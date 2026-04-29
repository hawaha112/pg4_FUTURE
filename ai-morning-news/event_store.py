#!/usr/bin/env python3
"""
event_store.py — 事件库 v2：canonical event + evidence chain + status lifecycle

设计目标：
- 引入 canonical_events 表：每个"事件"是一个独立实体，有生命周期
- 引入 evidence 表：每条原始文章是一条证据，关联到 canonical event
- 状态生命周期：rumor → reported → confirmed → official
- 保留证据链：谁先发、谁跟发、官方是否确认
- articles 表保留原有功能（存储原始文章），但增加 canonical_event_id 外键

表结构：
  articles          — 原始文章表（原 events 表，改名更清晰）
  canonical_events  — 事件主表：每个独立事件一条记录
  evidence          — 证据链：文章 ↔ 事件 的关联 + 角色标记
  collection_runs   — 采集运行记录（审计 + 监控）
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

from logger import get_logger
log = get_logger('event_store')


# ─── 状态生命周期 ────────────────────────────────────────────
# 状态按确认度排序：数值越大越确定
STATUS_LEVELS = {
    'rumor': 0,
    'reported': 1,
    'confirmed': 2,
    'official': 3,
}

# 状态升级规则
_STATUS_UPGRADE_RULES = {
    # (当前状态, 条件) → 新状态
    # 条件由 _evaluate_status_upgrade() 动态判断
}


def _url_hash(url: str) -> str:
    """URL 归一化后取 SHA256 前 32 位作为主键"""
    normalized = url.strip().rstrip('/').lower()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]


def _event_hash(title: str, source: str) -> str:
    """基于标题+来源生成事件候选 ID（聚类后可能合并）"""
    text = (title.strip().lower() + '|' + source.strip().lower())
    return 'evt_' + hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]


class EventStore:
    """事件库 v2：canonical event + evidence chain + status lifecycle"""

    def __init__(self, db_path: str, auto_recover: bool = True):
        self.db_path = db_path
        self.db = self._open_with_integrity_check(db_path, auto_recover=auto_recover)
        self._init_schema()
        # 启动时如库体积 > 阈值则做一次 VACUUM（每周不过一次），防止去重表无限膨胀
        self._maybe_vacuum()

    # ------------------------------------------------------------------
    # Integrity + recovery helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _open_with_integrity_check(db_path: str, *, auto_recover: bool) -> sqlite3.Connection:
        """打开 SQLite 连接，运行 integrity_check；失败时尝试从最近备份恢复。"""
        import os
        import shutil
        from pathlib import Path

        def _open(path):
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

        conn = _open(db_path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            ok = row and row[0] == 'ok'
        except sqlite3.DatabaseError:
            ok = False
        if ok:
            return conn

        log.error("🚨 SQLite 完整性校验失败: %s", db_path)
        try:
            conn.close()
        except Exception:
            pass

        if not auto_recover:
            raise sqlite3.DatabaseError(f"integrity_check failed on {db_path}")

        # 找最近的备份：{db_path}.bak.YYYYMMDD
        db_dir = Path(db_path).parent
        db_name = Path(db_path).name
        candidates = sorted(
            db_dir.glob(f"{db_name}.bak.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for cand in candidates:
            log.warning("🔁 尝试从备份恢复: %s", cand)
            try:
                corrupted = Path(db_path).with_suffix(Path(db_path).suffix + ".corrupt")
                try:
                    shutil.move(db_path, corrupted)
                except FileNotFoundError:
                    pass
                shutil.copy2(cand, db_path)
                conn = _open(db_path)
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if row and row[0] == 'ok':
                    log.warning("✅ 已从 %s 恢复，原损坏文件备份为 %s", cand.name, corrupted.name)
                    return conn
                conn.close()
            except Exception as e:
                log.warning("  恢复失败: %s", e)

        # 实在无救，用空库继续（跳过损坏数据），保存原文件以便事后分析
        log.error("💥 无可用备份，建立空库以继续。请事后检查 %s", db_path)
        corrupted = Path(db_path).with_suffix(Path(db_path).suffix + ".corrupt")
        try:
            if os.path.exists(db_path):
                shutil.move(db_path, corrupted)
        except Exception:
            pass
        return _open(db_path)

    def _maybe_vacuum(self, size_threshold_mb: float = 50.0, min_interval_days: int = 7):
        """库体积超过阈值且距上次 VACUUM > N 天时，执行 VACUUM。"""
        import os
        import time
        try:
            size_mb = os.path.getsize(self.db_path) / (1024 * 1024)
        except OSError:
            return
        if size_mb < size_threshold_mb:
            return
        # 用 meta 表记录最后 VACUUM 时间（建表使用 _internal 前缀避免冲突）
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS _internal_meta (k TEXT PRIMARY KEY, v TEXT)"
            )
            row = self.db.execute(
                "SELECT v FROM _internal_meta WHERE k='last_vacuum'"
            ).fetchone()
            last_ts = float(row[0]) if row else 0.0
            now = time.time()
            if now - last_ts < min_interval_days * 86400:
                return
            log.info("🧹 SQLite VACUUM: %s (%.1f MB)", self.db_path, size_mb)
            self.db.isolation_level = None
            try:
                self.db.execute("VACUUM")
            finally:
                self.db.isolation_level = ""
            self.db.execute(
                "INSERT OR REPLACE INTO _internal_meta(k, v) VALUES ('last_vacuum', ?)",
                (str(now),),
            )
            self.db.commit()
        except Exception as e:
            log.warning("⚠️ VACUUM 失败（不影响后续操作）: %s", e)

    def _init_schema(self):
        self.db.executescript("""
            -- 原始文章表（原 events 表的升级版）
            CREATE TABLE IF NOT EXISTS articles (
                id              TEXT PRIMARY KEY,
                url             TEXT NOT NULL,
                title           TEXT,
                summary         TEXT,
                full_text       TEXT,
                published_at    TEXT,
                collected_at    TEXT NOT NULL,
                source_name     TEXT,
                source_icon     TEXT,
                source_color    TEXT,
                source_category TEXT,
                source_tier     INTEGER DEFAULT 2,
                image_url       TEXT,
                analysis        TEXT,
                analyzed_at     TEXT,
                status          TEXT DEFAULT 'collected',
                importance      INTEGER DEFAULT 0,
                ai_relevant     INTEGER DEFAULT 1,
                rendered_at     TEXT,
                canonical_event_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_articles_status
                ON articles(status);
            CREATE INDEX IF NOT EXISTS idx_articles_collected
                ON articles(collected_at);
            CREATE INDEX IF NOT EXISTS idx_articles_source
                ON articles(source_name);
            CREATE INDEX IF NOT EXISTS idx_articles_tier
                ON articles(source_tier);
            CREATE INDEX IF NOT EXISTS idx_articles_canonical
                ON articles(canonical_event_id);

            -- 事件主表：每个独立事件一条记录
            CREATE TABLE IF NOT EXISTS canonical_events (
                event_id        TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                summary         TEXT,
                first_seen_at   TEXT NOT NULL,
                last_updated_at TEXT NOT NULL,
                published_at    TEXT,
                canonical_url   TEXT,
                canonical_source TEXT,
                canonical_article_id TEXT,
                entity_tags     TEXT DEFAULT '[]',
                event_type      TEXT DEFAULT 'news',
                status          TEXT DEFAULT 'reported',
                importance      INTEGER DEFAULT 0,
                cluster_size    INTEGER DEFAULT 1,
                analysis        TEXT,
                rendered_at     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_ce_status
                ON canonical_events(status);
            CREATE INDEX IF NOT EXISTS idx_ce_first_seen
                ON canonical_events(first_seen_at);
            CREATE INDEX IF NOT EXISTS idx_ce_importance
                ON canonical_events(importance);

            -- 证据链：文章 ↔ 事件 的关联 + 角色标记
            CREATE TABLE IF NOT EXISTS evidence (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        TEXT NOT NULL,
                article_id      TEXT NOT NULL,
                role            TEXT NOT NULL DEFAULT 'follower',
                source_name     TEXT,
                source_tier     INTEGER DEFAULT 2,
                reported_at     TEXT,
                url             TEXT,
                title           TEXT,
                UNIQUE(event_id, article_id)
            );

            CREATE INDEX IF NOT EXISTS idx_evidence_event
                ON evidence(event_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_article
                ON evidence(article_id);

            -- 采集运行记录
            CREATE TABLE IF NOT EXISTS collection_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                items_fetched   INTEGER DEFAULT 0,
                items_new       INTEGER DEFAULT 0,
                items_analyzed  INTEGER DEFAULT 0,
                events_created  INTEGER DEFAULT 0,
                events_updated  INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'running'
            );
        """)
        self.db.commit()
        self._migrate_if_needed()

    def _migrate_if_needed(self):
        """从旧 schema 迁移：如果 events 表存在但 articles 表不是从它创建的"""
        try:
            # 检查是否有旧的 events 表（无 canonical_event_id 列）
            self.db.execute("SELECT canonical_event_id FROM articles LIMIT 0")
        except sqlite3.OperationalError:
            # articles 表存在但缺少 canonical_event_id，添加之
            try:
                self.db.execute(
                    "ALTER TABLE articles ADD COLUMN canonical_event_id TEXT"
                )
                self.db.commit()
                log.info("📦 迁移: articles 表增加 canonical_event_id 列")
            except sqlite3.OperationalError:
                pass

        # 检查是否有旧名的 events 表需要迁移
        tables = [row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        if 'events' in tables and 'articles' not in tables:
            # 旧 schema：将 events 重命名为 articles
            self.db.execute("ALTER TABLE events RENAME TO articles")
            try:
                self.db.execute(
                    "ALTER TABLE articles ADD COLUMN canonical_event_id TEXT"
                )
            except sqlite3.OperationalError:
                pass
            self.db.commit()
            log.info("📦 迁移: events 表已重命名为 articles")
        elif 'events' in tables and 'articles' in tables:
            # 两表共存：旧 events 表是残留，数据已在 articles 里
            # 安全地删除旧表（避免 schema 冲突）
            try:
                self.db.execute("DROP TABLE IF EXISTS events")
                self.db.commit()
                log.info("📦 迁移: 清理残留的旧 events 表")
            except sqlite3.OperationalError:
                pass

    # ═══════════════════════════════════════════════════════════════
    # 文章写入（采集器用）— 兼容旧接口
    # ═══════════════════════════════════════════════════════════════

    def upsert_event(self, item: Dict[str, Any]) -> bool:
        """写入或更新一条文章。返回 True 表示新插入。

        保留旧接口名称以兼容 collector.py，实际写入 articles 表。
        """
        url = item.get('url', '') or item.get('link', '')
        if not url:
            return False

        # 统一字段名：RSS fetcher 用 'image'，DB 用 'image_url'
        if 'image' in item and 'image_url' not in item:
            item['image_url'] = item['image']

        article_id = _url_hash(url)

        existing = self.db.execute(
            "SELECT id, status FROM articles WHERE id = ?", (article_id,)
        ).fetchone()

        if existing:
            if existing[1] == 'collected':
                self.db.execute("""
                    UPDATE articles SET
                        title = COALESCE(?, title),
                        summary = COALESCE(?, summary),
                        full_text = COALESCE(?, full_text),
                        image_url = COALESCE(?, image_url)
                    WHERE id = ?
                """, (
                    item.get('title'),
                    item.get('summary'),
                    item.get('full_text'),
                    item.get('image_url'),
                    article_id,
                ))
            return False

        published = item.get('published')
        pub_str = published.isoformat() if isinstance(published, datetime) else (published or '')

        self.db.execute("""
            INSERT INTO articles (
                id, url, title, summary, full_text,
                published_at, collected_at,
                source_name, source_icon, source_color, source_category,
                source_tier, image_url, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'collected')
        """, (
            article_id, url,
            item.get('title', ''),
            item.get('summary', ''),
            item.get('full_text', ''),
            pub_str,
            datetime.now(timezone.utc).isoformat(),
            item.get('source_name', ''),
            item.get('source_icon', ''),
            item.get('source_color', ''),
            item.get('source_category', ''),
            item.get('source_tier', 2),
            item.get('image_url', ''),
        ))
        return True

    def save_analysis(self, url: str, analysis: Dict[str, Any]):
        """保存 LLM 分析结果到文章"""
        article_id = _url_hash(url)
        ai_relevant = 1 if analysis.get('ai_relevant', True) else 0
        importance = analysis.get('importance', 0)

        self.db.execute("""
            UPDATE articles SET
                analysis = ?,
                analyzed_at = ?,
                status = 'analyzed',
                importance = ?,
                ai_relevant = ?
            WHERE id = ?
        """, (
            json.dumps(analysis, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            importance, ai_relevant, article_id,
        ))

    # ═══════════════════════════════════════════════════════════════
    # Canonical Event 管理
    # ═══════════════════════════════════════════════════════════════

    def create_canonical_event(
        self,
        event_id: str,
        title: str,
        summary: str = '',
        canonical_url: str = '',
        canonical_source: str = '',
        canonical_article_id: str = '',
        entity_tags: List[str] = None,
        event_type: str = 'news',
        status: str = 'reported',
        importance: int = 0,
        published_at: str = '',
    ) -> bool:
        """创建 canonical event。返回 True=新建, False=已存在。"""
        now = datetime.now(timezone.utc).isoformat()

        existing = self.db.execute(
            "SELECT event_id FROM canonical_events WHERE event_id = ?",
            (event_id,)
        ).fetchone()

        if existing:
            return False

        self.db.execute("""
            INSERT INTO canonical_events (
                event_id, title, summary, first_seen_at, last_updated_at,
                published_at, canonical_url, canonical_source, canonical_article_id,
                entity_tags, event_type, status, importance, cluster_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            event_id, title, summary, now, now,
            published_at, canonical_url, canonical_source, canonical_article_id,
            json.dumps(entity_tags or [], ensure_ascii=False),
            event_type, status, importance,
        ))
        return True

    def update_canonical_event(
        self,
        event_id: str,
        cluster_size: int = None,
        importance: int = None,
        status: str = None,
        analysis: Dict[str, Any] = None,
        entity_tags: List[str] = None,
        canonical_url: str = None,
        canonical_source: str = None,
        canonical_article_id: str = None,
        title: str = None,
        summary: str = None,
    ):
        """更新 canonical event 的字段（只更新非 None 的参数）"""
        now = datetime.now(timezone.utc).isoformat()
        updates = ["last_updated_at = ?"]
        params = [now]

        if cluster_size is not None:
            updates.append("cluster_size = ?")
            params.append(cluster_size)
        if importance is not None:
            updates.append("importance = ?")
            params.append(importance)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if analysis is not None:
            updates.append("analysis = ?")
            params.append(json.dumps(analysis, ensure_ascii=False))
        if entity_tags is not None:
            updates.append("entity_tags = ?")
            params.append(json.dumps(entity_tags, ensure_ascii=False))
        if canonical_url is not None:
            updates.append("canonical_url = ?")
            params.append(canonical_url)
        if canonical_source is not None:
            updates.append("canonical_source = ?")
            params.append(canonical_source)
        if canonical_article_id is not None:
            updates.append("canonical_article_id = ?")
            params.append(canonical_article_id)
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if summary is not None:
            updates.append("summary = ?")
            params.append(summary)

        params.append(event_id)
        sql = f"UPDATE canonical_events SET {', '.join(updates)} WHERE event_id = ?"
        self.db.execute(sql, params)

    def add_evidence(
        self,
        event_id: str,
        article_id: str,
        role: str = 'follower',
        source_name: str = '',
        source_tier: int = 2,
        reported_at: str = '',
        url: str = '',
        title: str = '',
    ):
        """添加证据链记录：将文章关联到 canonical event"""
        try:
            self.db.execute("""
                INSERT OR IGNORE INTO evidence (
                    event_id, article_id, role,
                    source_name, source_tier, reported_at, url, title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id, article_id, role,
                source_name, source_tier, reported_at, url, title,
            ))
            # 同时更新文章的 canonical_event_id
            self.db.execute(
                "UPDATE articles SET canonical_event_id = ? WHERE id = ?",
                (event_id, article_id)
            )
        except sqlite3.IntegrityError:
            pass

    def upgrade_event_status(self, event_id: str, new_status: str,
                             reason: str = '') -> bool:
        """尝试升级事件状态。只允许向上升级，不允许降级。

        Returns:
            True 如果状态实际发生了升级
        """
        row = self.db.execute(
            "SELECT status FROM canonical_events WHERE event_id = ?",
            (event_id,)
        ).fetchone()

        if not row:
            return False

        current = row[0]
        current_level = STATUS_LEVELS.get(current, 0)
        new_level = STATUS_LEVELS.get(new_status, 0)

        if new_level <= current_level:
            return False

        self.update_canonical_event(event_id, status=new_status)
        log.info("📈 事件状态升级 [%s]: %s → %s%s",
                 event_id[:12], current, new_status,
                 f" ({reason})" if reason else "")
        return True

    def auto_upgrade_status(self, event_id: str) -> Optional[str]:
        """根据证据链自动判断是否应升级事件状态

        规则：
        - 有 Tier 0 官方源证据 → official
        - 有 3+ 不同来源报道（含 Tier 1）→ confirmed
        - 有 2+ 不同来源报道 → reported
        - 仅单一来源 → rumor（或保持 reported）

        Returns:
            升级后的状态，或 None 表示无变化
        """
        evidences = self.db.execute("""
            SELECT source_name, source_tier, role
            FROM evidence WHERE event_id = ?
        """, (event_id,)).fetchall()

        if not evidences:
            return None

        unique_sources = set(e[0] for e in evidences if e[0])
        has_tier0 = any(e[1] == 0 for e in evidences)
        has_official_role = any(e[2] == 'official' for e in evidences)
        n_sources = len(unique_sources)
        has_tier1 = any(e[1] <= 1 for e in evidences)

        # 判断目标状态
        if has_tier0 or has_official_role:
            target = 'official'
            reason = '官方源确认'
        elif n_sources >= 3 and has_tier1:
            target = 'confirmed'
            reason = f'{n_sources} 个来源交叉验证（含权威源）'
        elif n_sources >= 2:
            target = 'reported'
            reason = f'{n_sources} 个来源报道'
        else:
            return None

        if self.upgrade_event_status(event_id, target, reason):
            return target
        return None

    # ═══════════════════════════════════════════════════════════════
    # 读取接口
    # ═══════════════════════════════════════════════════════════════

    def get_events_for_briefing(
        self,
        hours: int = 24,
        min_importance: int = 0,
        include_unanalyzed_tier0: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取指定时间窗口内的已分析文章（兼容旧接口）"""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        query = """
            SELECT * FROM articles
            WHERE collected_at >= ?
              AND status = 'analyzed'
              AND ai_relevant = 1
              AND importance >= ?
            ORDER BY importance DESC, collected_at DESC
        """
        rows = self.db.execute(query, (cutoff, min_importance)).fetchall()
        events = [self._row_to_dict(r) for r in rows]

        if include_unanalyzed_tier0:
            tier0_query = """
                SELECT * FROM articles
                WHERE collected_at >= ?
                  AND source_tier = 0
                  AND status = 'collected'
                ORDER BY collected_at DESC
            """
            tier0_rows = self.db.execute(tier0_query, (cutoff,)).fetchall()
            events.extend(self._row_to_dict(r) for r in tier0_rows)

        return events

    def get_canonical_events_for_briefing(
        self,
        hours: int = 24,
        min_importance: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取 canonical events 用于生成早报（新接口）

        返回的每条事件包含：
        - 事件基本信息（title, summary, status, importance 等）
        - 证据链（evidence_chain: [{role, source_name, source_tier, url, title, reported_at}]）
        - 最佳文章的 analysis
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        # 用 last_updated_at 表示"事件近期是否仍在活跃/被跟进"：
        # 一个事件可能 04-27 首次建库 (first_seen_at)，04-29 仍有新源跟进
        # → last_updated_at 会被刷到 04-29，应进入今天的候选池供窗口筛选
        rows = self.db.execute("""
            SELECT * FROM canonical_events
            WHERE last_updated_at >= ?
              AND importance >= ?
            ORDER BY importance DESC, last_updated_at DESC
        """, (cutoff, min_importance)).fetchall()

        events = []
        for row in rows:
            event = self._ce_row_to_dict(row)

            # 加载证据链
            evidence_rows = self.db.execute("""
                SELECT role, source_name, source_tier, url, title, reported_at
                FROM evidence
                WHERE event_id = ?
                ORDER BY
                    CASE role
                        WHEN 'first_reporter' THEN 0
                        WHEN 'official' THEN 1
                        WHEN 'confirmer' THEN 2
                        WHEN 'follower' THEN 3
                    END,
                    reported_at ASC
            """, (event['event_id'],)).fetchall()

            event['evidence_chain'] = [
                {
                    'role': r[0], 'source_name': r[1], 'source_tier': r[2],
                    'url': r[3], 'title': r[4], 'reported_at': r[5],
                }
                for r in evidence_rows
            ]

            # 加载 canonical article 的完整 analysis
            if event.get('canonical_article_id'):
                art_row = self.db.execute(
                    "SELECT * FROM articles WHERE id = ?",
                    (event['canonical_article_id'],)
                ).fetchone()
                if art_row:
                    art = self._row_to_dict(art_row)
                    # 合并文章级别字段
                    event['article'] = art

                    # 智能合并 analysis：优先 canonical event 的，
                    # 但对空字段用 article 的 analysis 补充
                    ev_analysis = event.get('analysis') or {}
                    art_analysis = art.get('analysis') or {}
                    if not ev_analysis and art_analysis:
                        event['analysis'] = art_analysis
                    elif ev_analysis and art_analysis:
                        # 补充 canonical event analysis 中缺失的字段
                        merge_fields = [
                            'detailed_content', 'background', 'deep_analysis',
                            'chinese_title', 'why_it_matters', 'key_details',
                        ]
                        for field in merge_fields:
                            if not ev_analysis.get(field) and art_analysis.get(field):
                                ev_analysis[field] = art_analysis[field]
                        event['analysis'] = ev_analysis

                    # 携带渲染需要的来源展示信息
                    event['source_icon'] = art.get('source_icon', '')
                    event['source_color'] = art.get('source_color', '')
                    event['source_category'] = art.get('source_category', '')
                    event['image_url'] = art.get('image_url', '')

            events.append(event)

        return events

    def get_pending_analysis(self, limit: int = 200) -> List[Dict[str, Any]]:
        """获取等待 LLM 分析的文章"""
        rows = self.db.execute("""
            SELECT * FROM articles
            WHERE status = 'collected'
            ORDER BY source_tier ASC, collected_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_unlinked_articles(self, hours: int = 24) -> List[Dict[str, Any]]:
        """获取尚未关联到 canonical event 的已分析且 AI 相关的文章"""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.db.execute("""
            SELECT * FROM articles
            WHERE collected_at >= ?
              AND status = 'analyzed'
              AND ai_relevant = 1
              AND canonical_event_id IS NULL
            ORDER BY source_tier ASC, collected_at DESC
        """, (cutoff,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_evidence_for_event(self, event_id: str) -> List[Dict[str, Any]]:
        """获取某个 canonical event 的全部证据"""
        rows = self.db.execute("""
            SELECT e.*, a.analysis, a.full_text
            FROM evidence e
            LEFT JOIN articles a ON e.article_id = a.id
            WHERE e.event_id = ?
            ORDER BY e.reported_at ASC
        """, (event_id,)).fetchall()

        results = []
        for r in rows:
            d = {
                'id': r[0], 'event_id': r[1], 'article_id': r[2],
                'role': r[3], 'source_name': r[4], 'source_tier': r[5],
                'reported_at': r[6], 'url': r[7], 'title': r[8],
            }
            # article fields from JOIN
            if len(r) > 9:
                if r[9]:
                    try:
                        d['analysis'] = json.loads(r[9])
                    except (json.JSONDecodeError, TypeError):
                        d['analysis'] = {}
                d['full_text'] = r[10] if len(r) > 10 else ''
            results.append(d)
        return results

    def get_canonical_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """获取单个 canonical event"""
        row = self.db.execute(
            "SELECT * FROM canonical_events WHERE event_id = ?",
            (event_id,)
        ).fetchone()
        return self._ce_row_to_dict(row) if row else None

    def find_recent_canonical_events(self, hours: int = 48) -> List[Dict[str, Any]]:
        """获取最近的 canonical events（用于聚类时查找已有事件）"""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.db.execute("""
            SELECT * FROM canonical_events
            WHERE first_seen_at >= ?
            ORDER BY first_seen_at DESC
        """, (cutoff,)).fetchall()
        return [self._ce_row_to_dict(r) for r in rows]

    def mark_rendered(self, event_ids: List[str]):
        """标记事件已被渲染到早报（兼容旧接口 — 标记 articles）"""
        now = datetime.now(timezone.utc).isoformat()
        for eid in event_ids:
            self.db.execute(
                "UPDATE articles SET rendered_at = ? WHERE id = ?",
                (now, eid)
            )
        self.db.commit()

    def mark_canonical_rendered(self, event_ids: List[str]):
        """标记 canonical events 已被渲染"""
        now = datetime.now(timezone.utc).isoformat()
        for eid in event_ids:
            self.db.execute(
                "UPDATE canonical_events SET rendered_at = ? WHERE event_id = ?",
                (now, eid)
            )
        self.db.commit()

    # ═══════════════════════════════════════════════════════════════
    # 采集运行记录
    # ═══════════════════════════════════════════════════════════════

    def start_collection_run(self) -> int:
        cursor = self.db.execute(
            "INSERT INTO collection_runs (started_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
        self.db.commit()
        return cursor.lastrowid

    def finish_collection_run(self, run_id: int, fetched: int, new: int,
                              analyzed: int, events_created: int = 0,
                              events_updated: int = 0):
        self.db.execute("""
            UPDATE collection_runs SET
                finished_at = ?,
                items_fetched = ?,
                items_new = ?,
                items_analyzed = ?,
                events_created = ?,
                events_updated = ?,
                status = 'done'
            WHERE id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            fetched, new, analyzed, events_created, events_updated, run_id,
        ))
        self.db.commit()

    # ═══════════════════════════════════════════════════════════════
    # 维护
    # ═══════════════════════════════════════════════════════════════

    def cleanup(self, keep_days: int = 30):
        """清理过期数据"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()

        art_deleted = self.db.execute(
            "DELETE FROM articles WHERE collected_at < ?", (cutoff,)
        ).rowcount

        ev_deleted = self.db.execute(
            "DELETE FROM canonical_events WHERE first_seen_at < ?", (cutoff,)
        ).rowcount

        # 清理孤立的 evidence 记录
        self.db.execute("""
            DELETE FROM evidence WHERE event_id NOT IN (
                SELECT event_id FROM canonical_events
            )
        """)

        self.db.execute(
            "DELETE FROM collection_runs WHERE started_at < ?", (cutoff,)
        )
        self.db.commit()

        if art_deleted or ev_deleted:
            log.info("🧹 清理: %d 条文章, %d 个事件（超过 %d 天）",
                     art_deleted, ev_deleted, keep_days)

    def commit(self):
        self.db.commit()

    def stats(self) -> Dict[str, Any]:
        """返回事件库统计信息"""
        total_articles = self.db.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0]
        collected = self.db.execute(
            "SELECT COUNT(*) FROM articles WHERE status = 'collected'"
        ).fetchone()[0]
        analyzed = self.db.execute(
            "SELECT COUNT(*) FROM articles WHERE status = 'analyzed'"
        ).fetchone()[0]

        total_events = self.db.execute(
            "SELECT COUNT(*) FROM canonical_events"
        ).fetchone()[0]
        total_evidence = self.db.execute(
            "SELECT COUNT(*) FROM evidence"
        ).fetchone()[0]

        # 事件状态分布
        status_dist = {}
        for row in self.db.execute(
            "SELECT status, COUNT(*) FROM canonical_events GROUP BY status"
        ).fetchall():
            status_dist[row[0]] = row[1]

        by_tier = {}
        for row in self.db.execute(
            "SELECT source_tier, COUNT(*) FROM articles GROUP BY source_tier"
        ).fetchall():
            by_tier[f"tier_{row[0]}"] = row[1]

        return {
            "total_articles": total_articles,
            "collected": collected,
            "analyzed": analyzed,
            "total_events": total_events,
            "total_evidence": total_evidence,
            "event_status": status_dist,
            **by_tier,
        }

    def close(self):
        self.db.close()

    # ═══════════════════════════════════════════════════════════════
    # 内部工具
    # ═══════════════════════════════════════════════════════════════

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """将 articles 表的 row 转为 dict"""
        cols = [
            'id', 'url', 'title', 'summary', 'full_text',
            'published_at', 'collected_at',
            'source_name', 'source_icon', 'source_color', 'source_category',
            'source_tier', 'image_url',
            'analysis', 'analyzed_at', 'status',
            'importance', 'ai_relevant', 'rendered_at',
            'canonical_event_id',
        ]
        d = dict(zip(cols, row))

        if d.get('analysis'):
            try:
                d['analysis'] = json.loads(d['analysis'])
            except (json.JSONDecodeError, TypeError):
                d['analysis'] = {}
        else:
            d['analysis'] = {}

        if d.get('published_at'):
            try:
                d['published'] = datetime.fromisoformat(d['published_at'])
            except (ValueError, TypeError):
                d['published'] = None
        else:
            d['published'] = None

        return d

    def _ce_row_to_dict(self, row) -> Dict[str, Any]:
        """将 canonical_events 表的 row 转为 dict"""
        cols = [
            'event_id', 'title', 'summary',
            'first_seen_at', 'last_updated_at', 'published_at',
            'canonical_url', 'canonical_source', 'canonical_article_id',
            'entity_tags', 'event_type', 'status',
            'importance', 'cluster_size', 'analysis', 'rendered_at',
        ]
        d = dict(zip(cols, row))

        for json_field in ('entity_tags', 'analysis'):
            if d.get(json_field):
                try:
                    d[json_field] = json.loads(d[json_field])
                except (json.JSONDecodeError, TypeError):
                    d[json_field] = [] if json_field == 'entity_tags' else {}
            else:
                d[json_field] = [] if json_field == 'entity_tags' else {}

        if d.get('first_seen_at'):
            try:
                d['first_seen'] = datetime.fromisoformat(d['first_seen_at'])
            except (ValueError, TypeError):
                d['first_seen'] = None
        else:
            d['first_seen'] = None

        if d.get('published_at'):
            try:
                d['published'] = datetime.fromisoformat(d['published_at'])
            except (ValueError, TypeError):
                d['published'] = None
        else:
            d['published'] = None

        return d
