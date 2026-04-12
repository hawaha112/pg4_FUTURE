"""
dedup_engine.py — 两层去重引擎（v2 — MinHash/LSH 优化版）
第一层：精确去重（URL 哈希 + 标题哈希）
第二层：语义去重（MinHash/LSH 候选过滤 → TF-IDF 精确确认）
存储层：SQLite 持久化（含 IDF 缓存 + MinHash 签名存储）

优化点（相比 v1）：
- MinHash + LSH banding 将候选查找从 O(n) 降为近似 O(1)
- IDF 统计缓存到 SQLite，跨运行复用，避免每次重算
- MinHash 签名持久化，历史文档无需重新计算

用法:
    engine = DedupEngine("dedup.db")
    unique = engine.deduplicate(items)   # items: list[dict]
    engine.close()
"""

import hashlib
import math
import random
import re
import sqlite3
import struct
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from logger import get_logger
log = get_logger('dedup_engine')


# ---------------------------------------------------------------------------
# 文本标准化
# ---------------------------------------------------------------------------

_SPLIT_RE = re.compile(r'[\w]+', re.UNICODE)
_CJK_RANGES = [
    (0x4E00, 0x9FFF),    # CJK Unified
    (0x3400, 0x4DBF),    # CJK Extension A
    (0xF900, 0xFAFF),    # CJK Compatibility
]


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> List[str]:
    """中英文混合分词：中文按字+双字 n-gram，英文按词"""
    text = text.lower().strip()
    text = unicodedata.normalize('NFKC', text)
    tokens = []
    for word in _SPLIT_RE.findall(text):
        has_cjk = any(_is_cjk(ch) for ch in word)
        if has_cjk:
            chars = [ch for ch in word if _is_cjk(ch)]
            tokens.extend(chars)
            for i in range(len(chars) - 1):
                tokens.append(chars[i] + chars[i + 1])
        else:
            if len(word) > 1:
                tokens.append(word)
    return tokens


def text_hash(text: str) -> str:
    """文本指纹：标准化后 SHA256"""
    normalized = re.sub(r'\s+', '', text.lower().strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]


def url_hash(url: str) -> str:
    """URL 指纹：去协议、去参数、去尾斜杠"""
    if not url:
        return ""
    url = url.strip().rstrip('/')
    url = re.sub(r'^https?://(www\.)?', '', url)
    url = re.sub(r'[?#].*$', '', url)
    return hashlib.sha256(url.lower().encode('utf-8')).hexdigest()[:32]


# ---------------------------------------------------------------------------
# MinHash + LSH（纯标准库实现）
# ---------------------------------------------------------------------------

# 固定随机种子，确保跨运行签名一致
_MINHASH_SEED = 42
_NUM_HASHES = 64          # MinHash 签名长度
_NUM_BANDS = 32           # LSH band 数量（宽松 banding，提高召回率）
_ROWS_PER_BAND = 2        # 每个 band 的行数（_NUM_HASHES / _NUM_BANDS）
# 32b×2r: Jaccard 0.3 → 召回 ~95%，Jaccard 0.5 → 召回 ~100%
# 误报由 TF-IDF 第二阶段过滤，不影响最终精度
_LARGE_PRIME = (1 << 61) - 1  # Mersenne prime

# 预计算哈希系数 (a, b)，每对定义一个哈希函数 h(x) = (a*x + b) % p
random.seed(_MINHASH_SEED)
_HASH_COEFFS = [(random.randint(1, _LARGE_PRIME - 1),
                  random.randint(0, _LARGE_PRIME - 1))
                 for _ in range(_NUM_HASHES)]


def _token_hash(token: str) -> int:
    """将 token 映射为正整数"""
    return int(hashlib.md5(token.encode('utf-8')).hexdigest()[:16], 16)


def compute_minhash(tokens: List[str]) -> bytes:
    """计算 MinHash 签名，返回 64 个 uint32 打包为 bytes"""
    if not tokens:
        return b'\xff' * (_NUM_HASHES * 4)

    token_hashes = [_token_hash(t) for t in set(tokens)]

    sig = []
    for a, b in _HASH_COEFFS:
        min_val = min((a * h + b) % _LARGE_PRIME for h in token_hashes)
        sig.append(min_val & 0xFFFFFFFF)  # 截断为 uint32

    return struct.pack(f'{_NUM_HASHES}I', *sig)


def minhash_to_bands(sig_bytes: bytes) -> List[str]:
    """将 MinHash 签名切分为 LSH bands，每个 band 返回一个哈希字符串"""
    values = struct.unpack(f'{_NUM_HASHES}I', sig_bytes)
    bands = []
    for b in range(_NUM_BANDS):
        start = b * _ROWS_PER_BAND
        band_values = values[start:start + _ROWS_PER_BAND]
        band_hash = hashlib.md5(struct.pack(f'{_ROWS_PER_BAND}I', *band_values)).hexdigest()[:16]
        bands.append(f"b{b}_{band_hash}")
    return bands


def minhash_jaccard(sig_a: bytes, sig_b: bytes) -> float:
    """从 MinHash 签名估算 Jaccard 相似度"""
    va = struct.unpack(f'{_NUM_HASHES}I', sig_a)
    vb = struct.unpack(f'{_NUM_HASHES}I', sig_b)
    agree = sum(1 for a, b in zip(va, vb) if a == b)
    return agree / _NUM_HASHES


class MinHashLSH:
    """MinHash + LSH 索引，O(1) 近似近邻查找"""

    def __init__(self):
        # band_key → set of doc indices
        self._buckets: Dict[str, set] = {}
        # doc_idx → minhash signature (bytes)
        self._signatures: Dict[int, bytes] = {}

    def add(self, idx: int, tokens: List[str], sig_bytes: bytes = None):
        """添加文档到 LSH 索引"""
        if sig_bytes is None:
            sig_bytes = compute_minhash(tokens)
        self._signatures[idx] = sig_bytes
        for band_key in minhash_to_bands(sig_bytes):
            self._buckets.setdefault(band_key, set()).add(idx)

    def query_candidates(self, idx: int) -> set:
        """查找与 idx 可能相似的候选文档（O(1) 均摊）"""
        sig = self._signatures.get(idx)
        if sig is None:
            return set()
        candidates = set()
        for band_key in minhash_to_bands(sig):
            bucket = self._buckets.get(band_key, set())
            candidates.update(bucket)
        candidates.discard(idx)
        return candidates

    def get_signature(self, idx: int) -> Optional[bytes]:
        return self._signatures.get(idx)


# ---------------------------------------------------------------------------
# TF-IDF（带 IDF 缓存）
# ---------------------------------------------------------------------------

class TFIDFMatcher:
    """TF-IDF 语义匹配器，IDF 可从缓存加载"""

    def __init__(self, idf_cache: Optional[Dict[str, float]] = None):
        self._docs: List[List[str]] = []
        self._vectors: List[Dict[str, float]] = []
        self._df: Counter = Counter()
        self._n_docs: int = 0
        # IDF 缓存（从 SQLite 加载的历史统计）
        self._idf_cache = idf_cache or {}

    def add(self, text: str) -> int:
        """添加文档，返回索引"""
        tokens = tokenize(text)
        idx = self._n_docs
        self._docs.append(tokens)
        self._n_docs += 1

        unique_tokens = set(tokens)
        self._df.update(unique_tokens)

        tf = Counter(tokens)
        total = len(tokens) or 1
        self._vectors.append({t: c / total for t, c in tf.items()})
        return idx

    def _idf(self, term: str) -> float:
        """获取 IDF，优先用当前 session 的 DF，缺失时回退到缓存"""
        df = self._df.get(term, 0)
        if df > 0:
            return math.log(self._n_docs / (1 + df))
        if term in self._idf_cache:
            return self._idf_cache[term]
        return math.log(self._n_docs or 1)  # 未见过的 term，给较高 IDF

    def similarity(self, idx_a: int, idx_b: int) -> float:
        """TF-IDF 余弦相似度"""
        va = self._vectors[idx_a]
        vb = self._vectors[idx_b]
        all_terms = set(va.keys()) | set(vb.keys())
        if not all_terms:
            return 0.0

        dot = norm_a = norm_b = 0.0
        for t in all_terms:
            idf = self._idf(t)
            a_val = va.get(t, 0) * idf
            b_val = vb.get(t, 0) * idf
            dot += a_val * b_val
            norm_a += a_val * a_val
            norm_b += b_val * b_val

        denom = math.sqrt(norm_a) * math.sqrt(norm_b)
        return dot / denom if denom > 0 else 0.0

    def jaccard(self, idx_a: int, idx_b: int) -> float:
        """词集合 Jaccard 相似度（短标题兜底）"""
        ka = set(self._vectors[idx_a].keys())
        kb = set(self._vectors[idx_b].keys())
        if not ka or not kb:
            return 0.0
        return len(ka & kb) / len(ka | kb)

    def find_similar_among(self, idx: int, candidates: set,
                           threshold: float = 0.6) -> List[Tuple[int, float]]:
        """在候选集中找相似文档（由 LSH 预过滤，规模远小于全集）"""
        results = []
        is_short = len(self._vectors[idx]) < 10
        for i in candidates:
            if i >= idx:
                continue  # 只跟之前的比
            sim = self.similarity(idx, i)
            if sim < threshold and is_short:
                jac = self.jaccard(idx, i)
                if jac > sim:
                    sim = jac
            if sim >= threshold:
                results.append((i, sim))
        return results

    def export_idf(self) -> Dict[str, float]:
        """导出当前 IDF 统计（用于缓存到 SQLite）"""
        n = self._n_docs or 1
        return {t: math.log(n / (1 + df)) for t, df in self._df.items() if df > 0}


# ---------------------------------------------------------------------------
# SQLite 存储层（扩展：IDF 缓存 + MinHash 签名）
# ---------------------------------------------------------------------------

class DedupDB:
    """SQLite 持久化去重数据库"""

    def __init__(self, db_path: str = "dedup.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash TEXT NOT NULL,
                title_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                source TEXT DEFAULT '',
                first_seen TEXT NOT NULL,
                event_cluster TEXT DEFAULT '',
                minhash_sig BLOB DEFAULT NULL,
                UNIQUE(url_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_url_hash ON articles(url_hash);
            CREATE INDEX IF NOT EXISTS idx_title_hash ON articles(title_hash);
            CREATE INDEX IF NOT EXISTS idx_first_seen ON articles(first_seen);
            CREATE INDEX IF NOT EXISTS idx_cluster ON articles(event_cluster);

            CREATE TABLE IF NOT EXISTS idf_cache (
                term TEXT PRIMARY KEY,
                idf_value REAL NOT NULL,
                doc_freq INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        # 安全地添加 minhash_sig 列（已有 DB 可能缺失此列）
        try:
            self.conn.execute("SELECT minhash_sig FROM articles LIMIT 0")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE articles ADD COLUMN minhash_sig BLOB DEFAULT NULL")
        self.conn.commit()

    def has_url(self, uhash: str, hours: int = 0) -> bool:
        if hours > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            row = self.conn.execute(
                "SELECT 1 FROM articles WHERE url_hash=? AND first_seen>=? LIMIT 1",
                (uhash, cutoff)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM articles WHERE url_hash=? LIMIT 1", (uhash,)
            ).fetchone()
        return row is not None

    def has_title(self, thash: str, hours: int = 0) -> bool:
        if hours > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            row = self.conn.execute(
                "SELECT 1 FROM articles WHERE title_hash=? AND first_seen>=? LIMIT 1",
                (thash, cutoff)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM articles WHERE title_hash=? LIMIT 1", (thash,)
            ).fetchone()
        return row is not None

    def insert(self, uhash: str, thash: str, title: str, url: str,
               source: str = "", cluster: str = "", minhash_sig: bytes = None):
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO articles
                   (url_hash, title_hash, title, url, source, first_seen, event_cluster, minhash_sig)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (uhash, thash, title, url, source,
                 datetime.now(timezone.utc).isoformat(), cluster, minhash_sig)
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def get_recent_titles(self, hours: int = 72) -> List[Tuple[str, str, str, Optional[bytes]]]:
        """返回最近 N 小时的 (title, source, event_cluster, minhash_sig)"""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT title, source, event_cluster, minhash_sig FROM articles WHERE first_seen >= ? ORDER BY first_seen DESC",
            (cutoff,)
        ).fetchall()
        return rows

    # ── IDF 缓存 ──

    def load_idf_cache(self) -> Dict[str, float]:
        """从 SQLite 加载 IDF 缓存"""
        rows = self.conn.execute("SELECT term, idf_value FROM idf_cache").fetchall()
        return {row[0]: row[1] for row in rows}

    def save_idf_cache(self, idf_data: Dict[str, float]):
        """批量保存 IDF 统计到 SQLite"""
        now = datetime.now(timezone.utc).isoformat()
        batch = [(term, idf_val, 0, now) for term, idf_val in idf_data.items()]
        self.conn.executemany(
            "INSERT OR REPLACE INTO idf_cache (term, idf_value, doc_freq, updated_at) VALUES (?, ?, ?, ?)",
            batch
        )
        self.conn.commit()

    def cleanup(self, keep_days: int = 30):
        """清理过期记录"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        self.conn.execute("DELETE FROM articles WHERE first_seen < ?", (cutoff,))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# 去重引擎主类
# ---------------------------------------------------------------------------

class DedupEngine:
    """两层去重引擎（v2 — MinHash/LSH 优化版）

    第一层：精确匹配（URL 哈希 + 标题哈希）— O(1)
    第二层：语义匹配（MinHash/LSH 候选 → TF-IDF 精确确认）— 近似 O(1)
    """

    def __init__(self, db_path: str = "dedup.db",
                 semantic_threshold: float = 0.60,
                 recent_hours: int = 72):
        self.db = DedupDB(db_path)
        self.threshold = semantic_threshold
        self.recent_hours = recent_hours

        # 加载 IDF 缓存
        idf_cache = self.db.load_idf_cache()
        self.matcher = TFIDFMatcher(idf_cache=idf_cache)
        self.lsh = MinHashLSH()

        # 索引映射：matcher_idx → item info
        self._idx_map: Dict[int, dict] = {}
        # 延迟写入：仅在 commit() 时才持久化到 SQLite
        self._pending_inserts: List[tuple] = []

        # 加载历史标题到 matcher + LSH
        recent = self.db.get_recent_titles(hours=recent_hours)
        for title, source, cluster, minhash_sig in recent:
            idx = self.matcher.add(title)
            tokens = tokenize(title)
            # 有缓存的 MinHash 签名就直接用，否则重算
            if minhash_sig and len(minhash_sig) == _NUM_HASHES * 4:
                self.lsh.add(idx, tokens, sig_bytes=minhash_sig)
            else:
                self.lsh.add(idx, tokens)
            self._idx_map[idx] = {
                'title': title, 'source': source,
                'cluster': cluster, '_from_db': True
            }

        self._db_count = len(recent)
        idf_info = f"，IDF 缓存 {len(idf_cache)} 词" if idf_cache else ""
        log.info("📦 去重引擎已加载 %d 条历史记录%s", self._db_count, idf_info)

    def deduplicate(self, items: List[dict],
                    source_authority: Optional[Dict[str, int]] = None
                    ) -> List[dict]:
        """对文章列表进行两层去重

        Args:
            items: 文章列表，每条需有 title, link, source_name
            source_authority: 来源权威度字典，用于同事件多源时选最优

        Returns:
            去重后的文章列表
        """
        if source_authority is None:
            source_authority = {}

        hash_dupes = 0
        semantic_dupes = 0
        result = []

        # 事件聚类：cluster_id → [items]
        clusters: Dict[str, List[dict]] = {}
        cluster_counter = 0

        for item in items:
            title = item.get('title', '')
            link = item.get('link', '')
            source = item.get('source_name', '')

            uh = url_hash(link)
            th = text_hash(title)

            # ── 第一层：精确去重（仅在 recent_hours 窗口内匹配） ──
            if uh and self.db.has_url(uh, hours=self.recent_hours):
                hash_dupes += 1
                continue

            if th and self.db.has_title(th, hours=self.recent_hours):
                hash_dupes += 1
                continue

            # ── 第二层：语义去重（MinHash/LSH 候选 → TF-IDF 精确确认） ──
            idx = self.matcher.add(title)
            tokens = tokenize(title)
            self.lsh.add(idx, tokens)
            sig = self.lsh.get_signature(idx)

            # LSH 快速候选查找（近似 O(1)，替代之前的 O(n) 全扫描）
            candidates = self.lsh.query_candidates(idx)
            similar = self.matcher.find_similar_among(idx, candidates, self.threshold)

            if similar:
                best_match_idx, best_sim = max(similar, key=lambda x: x[1])
                best_match = self._idx_map.get(best_match_idx, {})

                if best_match.get('_from_db'):
                    semantic_dupes += 1
                    self._pending_inserts.append(
                        (uh, th, title, link, source,
                         best_match.get('cluster', ''), sig))
                    self._idx_map[idx] = {
                        'title': title, 'source': source,
                        'cluster': best_match.get('cluster', ''),
                        '_from_db': False, '_item': item
                    }
                    continue

                # 本批次内聚类
                existing_cluster = best_match.get('cluster', '')
                if existing_cluster:
                    cluster_id = existing_cluster
                else:
                    cluster_counter += 1
                    cluster_id = f"evt_{cluster_counter}"
                    best_match['cluster'] = cluster_id
                    if '_item' in best_match:
                        prev_item = best_match['_item']
                        clusters.setdefault(cluster_id, []).append(prev_item)
                        if prev_item in result:
                            result.remove(prev_item)

                item['_cluster'] = cluster_id
                item['_sim_score'] = best_sim
                clusters.setdefault(cluster_id, []).append(item)

                self._idx_map[idx] = {
                    'title': title, 'source': source,
                    'cluster': cluster_id,
                    '_from_db': False, '_item': item
                }
                continue

            # 全新条目
            self._idx_map[idx] = {
                'title': title, 'source': source,
                'cluster': '', '_from_db': False, '_item': item
            }
            self._pending_inserts.append((uh, th, title, link, source, '', sig))
            result.append(item)

        # ── 聚类选优：同事件多源只保留最佳 ──
        for cluster_id, cluster_items in clusters.items():
            best = self._pick_best(cluster_items, source_authority)
            best['_cluster_size'] = len(cluster_items)
            best['_cluster_sources'] = [
                it.get('source_name', '') for it in cluster_items
                if it is not best
            ]
            result.append(best)
            semantic_dupes += len(cluster_items) - 1

            for it in cluster_items:
                i_uh = url_hash(it.get('link', ''))
                i_th = text_hash(it.get('title', ''))
                self._pending_inserts.append(
                    (i_uh, i_th, it.get('title', ''),
                     it.get('link', ''),
                     it.get('source_name', ''), cluster_id, None))

        # 汇报
        total_removed = hash_dupes + semantic_dupes
        if total_removed > 0:
            parts = []
            if hash_dupes:
                parts.append(f"{hash_dupes} 条精确重复")
            if semantic_dupes:
                parts.append(f"{semantic_dupes} 条语义重复")
            log.info("🔄 去重移除 %d 条（%s）", total_removed, '，'.join(parts))

        return result

    def commit(self):
        """将本次去重发现的新文章持久化到数据库。

        只有在流水线成功完成后才应调用此方法，
        避免未完成的运行"消耗"文章导致下次运行结果为空。
        """
        for args in self._pending_inserts:
            uh, th, title, link, source, cluster, sig = args
            self.db.insert(uh, th, title, link, source, cluster, sig)
        if self._pending_inserts:
            log.info("💾 去重引擎已持久化 %d 条新文章", len(self._pending_inserts))
        self._pending_inserts.clear()
        # 更新 IDF 缓存
        idf_data = self.matcher.export_idf()
        if idf_data:
            self.db.save_idf_cache(idf_data)

    @staticmethod
    def _pick_best(cluster_items: List[dict],
                   source_authority: Dict[str, int]) -> dict:
        """从同事件多源报道中选出最优的一条"""
        def score(item):
            authority = source_authority.get(item.get('source_name', ''), 2)
            has_body = 1 if item.get('full_text', '') else 0
            title_len = min(len(item.get('title', '')), 100) / 100
            return authority * 3 + has_body * 2 + title_len

        return max(cluster_items, key=score)

    def close(self):
        self.db.close()
