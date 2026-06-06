"""
state_store.py — 应用状态统一存储层 (Single Source of Truth)

架构定位:
- events.db 是项目唯一的"应用状态"权威来源
- 之前散落在多个 JSON 文件的状态 (source_health.json, pushed_breaking.json)
  统一迁入 events.db 的新表
- 所有状态读写必须通过 StateStore, 模块直接打开文件视为反模式

迁移现状 (2026-05-16 第一阶段):
✅ 已迁: source_health (信源健康)  → table: source_health
✅ 已迁: pushed_breaking (突发去重) → table: pushed_breaking
🟡 暂留 JSON: hot_signals.json (跨 workflow 临时缓存, 每小时重生)
🟡 暂留 JSON: .digest_cache.json (单次跑内缓存)
🚫 不动: dedup.db / llm_cache.db (独立 SQLite, schema 各自合理)
🚫 不动: briefing-state Git 分支 (后续 phase 改造)

兼容性设计:
- StateStore 写入只走 DB
- 读取 DB-first, 兼容旧 JSON 作为 fallback (避免历史数据丢失)
- 提供 migrate_from_json() 一次性导入旧 JSON 数据
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import get_logger

log = get_logger('state_store')

# ─────────────────────────────────────────────────────────────
# Schema 定义 (与 event_store.py 共用 events.db, 加 2 张新表)
# ─────────────────────────────────────────────────────────────
_SOURCE_HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_health (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_success TEXT,
    last_count INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    last_error TEXT,
    last_response_time REAL DEFAULT 0,
    avg_response_time REAL DEFAULT 0,
    total_successes INTEGER DEFAULT 0,
    total_runs INTEGER DEFAULT 0,
    success_rate REAL DEFAULT 100.0,
    updated_at TEXT NOT NULL
)
"""

_PUSHED_BREAKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pushed_breaking (
    sig_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    title_zh TEXT,
    summary_zh TEXT,
    signal TEXT,
    url TEXT,
    score INTEGER DEFAULT 0,
    pushed_at TEXT NOT NULL
)
"""

# 既有 DB(briefing-state 分支)早于 title_zh/summary_zh/signal 列, 需幂等补列。
# 否则突发卡片 概括/signal/中文标题 取不到 (DB 是 SOT, load_pushed_breaking 返回它)。
_PUSHED_BREAKING_ADD_COLUMNS = ['title_zh', 'summary_zh', 'signal']

_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_source_health_status ON source_health(status)",
    "CREATE INDEX IF NOT EXISTS idx_source_health_failures ON source_health(consecutive_failures DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pushed_breaking_pushed_at ON pushed_breaking(pushed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pushed_breaking_source ON pushed_breaking(source)",
]


class StateStore:
    """统一状态存储, 围绕 events.db 上的状态表."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(str(self.db_path)) as con:
            con.execute(_SOURCE_HEALTH_SCHEMA)
            con.execute(_PUSHED_BREAKING_SCHEMA)
            # 幂等补列: 老 DB 缺 title_zh/signal 时加上 (ADD COLUMN 不支持 IF NOT EXISTS)
            existing = {r[1] for r in con.execute(
                "PRAGMA table_info(pushed_breaking)").fetchall()}
            for col in _PUSHED_BREAKING_ADD_COLUMNS:
                if col not in existing:
                    con.execute(f"ALTER TABLE pushed_breaking ADD COLUMN {col} TEXT")
            for idx in _INDICES:
                con.execute(idx)
            con.commit()

    # ─────────────────────────────────────────────────────
    # source_health (信源健康)
    # ─────────────────────────────────────────────────────

    def load_source_health(self) -> Dict[str, dict]:
        """返回 {source_name: health_dict} 全量, 与原 source_health.json 同 shape."""
        with sqlite3.connect(str(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM source_health").fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            name = d.pop('name')
            d.pop('updated_at', None)
            result[name] = d
        return result

    def save_source_health(self, name: str, health: dict) -> None:
        """upsert 单个信源健康记录 (health 是 health_tracker 写入的 dict)."""
        now = datetime.now(timezone.utc).isoformat()
        cols = {
            'name': name,
            'status': health.get('status', 'unknown'),
            'last_success': health.get('last_success', '') or '',
            'last_count': int(health.get('last_count', 0) or 0),
            'consecutive_failures': int(health.get('consecutive_failures', 0) or 0),
            'last_error': str(health.get('last_error', '') or '')[:500],
            'last_response_time': float(health.get('last_response_time', 0) or 0),
            'avg_response_time': float(health.get('avg_response_time', 0) or 0),
            'total_successes': int(health.get('total_successes', 0) or 0),
            'total_runs': int(health.get('total_runs', 0) or 0),
            'success_rate': float(health.get('success_rate', 100.0) or 100.0),
            'updated_at': now,
        }
        placeholders = ','.join('?' * len(cols))
        keys = ','.join(cols.keys())
        values = list(cols.values())
        with sqlite3.connect(str(self.db_path)) as con:
            con.execute(
                f"INSERT OR REPLACE INTO source_health ({keys}) VALUES ({placeholders})",
                values,
            )
            con.commit()

    def save_source_health_bulk(self, health_data: Dict[str, dict]) -> None:
        """批量 upsert (优化: 一次事务, 用于 health_tracker.save())."""
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for name, h in health_data.items():
            rows.append((
                name,
                h.get('status', 'unknown'),
                h.get('last_success', '') or '',
                int(h.get('last_count', 0) or 0),
                int(h.get('consecutive_failures', 0) or 0),
                str(h.get('last_error', '') or '')[:500],
                float(h.get('last_response_time', 0) or 0),
                float(h.get('avg_response_time', 0) or 0),
                int(h.get('total_successes', 0) or 0),
                int(h.get('total_runs', 0) or 0),
                float(h.get('success_rate', 100.0) or 100.0),
                now,
            ))
        with sqlite3.connect(str(self.db_path)) as con:
            con.executemany("""
                INSERT OR REPLACE INTO source_health (
                    name, status, last_success, last_count,
                    consecutive_failures, last_error,
                    last_response_time, avg_response_time,
                    total_successes, total_runs, success_rate, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            con.commit()

    # ─────────────────────────────────────────────────────
    # pushed_breaking (突发去重 24h TTL)
    # ─────────────────────────────────────────────────────

    def load_pushed_breaking(self, ttl_hours: int = 24) -> Dict[str, dict]:
        """返回 24h 内的 {sig_id: {pushed_at, source, title, url, score}}."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
        with sqlite3.connect(str(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM pushed_breaking WHERE pushed_at >= ?",
                (cutoff,),
            ).fetchall()
        return {r['sig_id']: {k: r[k] for k in r.keys() if k != 'sig_id'} for r in rows}

    def mark_breaking_pushed(
        self, sig_id: str, source: str, title: str = '',
        url: str = '', score: int = 0, title_zh: str = '', signal: str = '',
        summary_zh: str = '',
    ) -> None:
        with sqlite3.connect(str(self.db_path)) as con:
            con.execute("""
                INSERT OR REPLACE INTO pushed_breaking
                (sig_id, source, title, title_zh, summary_zh, signal, url, score, pushed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sig_id, source, str(title)[:200],
                str(title_zh or '')[:200], str(summary_zh or '')[:200],
                str(signal or '')[:120],
                str(url)[:500], int(score or 0),
                datetime.now(timezone.utc).isoformat(),
            ))
            con.commit()

    def cleanup_pushed_breaking(self, ttl_hours: int = 24 * 7) -> int:
        """删除 ttl_hours 之外的旧记录 (默认保留 7 天用于审计)."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
        with sqlite3.connect(str(self.db_path)) as con:
            r = con.execute("DELETE FROM pushed_breaking WHERE pushed_at < ?", (cutoff,))
            con.commit()
            return r.rowcount

    # ─────────────────────────────────────────────────────
    # 健康检查 / 调试
    # ─────────────────────────────────────────────────────

    def health_summary(self) -> dict:
        """返回所有状态表的健康摘要 (用于 state_check.py / dashboard)."""
        with sqlite3.connect(str(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            sh_total = con.execute("SELECT COUNT(*) AS n FROM source_health").fetchone()['n']
            sh_failing = con.execute(
                "SELECT COUNT(*) AS n FROM source_health WHERE consecutive_failures >= 3"
            ).fetchone()['n']
            pb_total = con.execute("SELECT COUNT(*) AS n FROM pushed_breaking").fetchone()['n']
            pb_24h = con.execute(
                "SELECT COUNT(*) AS n FROM pushed_breaking WHERE pushed_at >= datetime('now', '-1 day')"
            ).fetchone()['n']
        return {
            'source_health': {'total': sh_total, 'failing': sh_failing},
            'pushed_breaking': {'total': pb_total, 'last_24h': pb_24h},
        }


# ─────────────────────────────────────────────────────────────
# 一次性迁移工具: 从 JSON 导入到 DB
# ─────────────────────────────────────────────────────────────

def migrate_from_json(
    db_path: str | Path,
    source_health_json: Optional[Path] = None,
    pushed_breaking_json: Optional[Path] = None,
) -> Dict[str, int]:
    """从旧 JSON 文件批量导入 → DB. 返回每张表导入数量.

    幂等: 重复跑只覆盖, 不复制. 不删 JSON 文件.
    """
    store = StateStore(db_path)
    counts = {}

    if source_health_json and Path(source_health_json).exists():
        try:
            data = json.loads(Path(source_health_json).read_text(encoding='utf-8'))
            if isinstance(data, dict):
                store.save_source_health_bulk(data)
                counts['source_health'] = len(data)
                log.info("✓ 迁移 source_health: %d 条 → DB", len(data))
            else:
                log.warning("⚠️ source_health.json 格式异常 (非 dict), 跳过")
        except (json.JSONDecodeError, OSError) as e:
            log.warning("⚠️ source_health.json 加载失败: %s", e)

    if pushed_breaking_json and Path(pushed_breaking_json).exists():
        try:
            data = json.loads(Path(pushed_breaking_json).read_text(encoding='utf-8'))
            if isinstance(data, dict):
                migrated = 0
                for sig_id, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    store.mark_breaking_pushed(
                        sig_id=sig_id,
                        source=info.get('source', 'unknown'),
                        title=info.get('title', ''),
                        url=info.get('url', ''),
                        score=info.get('score', 0),
                    )
                    migrated += 1
                counts['pushed_breaking'] = migrated
                log.info("✓ 迁移 pushed_breaking: %d 条 → DB", migrated)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("⚠️ pushed_breaking.json 加载失败: %s", e)

    return counts


if __name__ == '__main__':
    # 命令行: python -m state_store migrate
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'migrate':
        script_dir = Path(__file__).parent
        counts = migrate_from_json(
            db_path=script_dir / 'events.db',
            source_health_json=script_dir / 'source_health.json',
            pushed_breaking_json=script_dir / 'output' / 'pushed_breaking.json',
        )
        print(f"\n迁移完成: {counts}")
    else:
        # 默认: 显示状态摘要
        script_dir = Path(__file__).parent
        store = StateStore(script_dir / 'events.db')
        print(json.dumps(store.health_summary(), indent=2, ensure_ascii=False))
