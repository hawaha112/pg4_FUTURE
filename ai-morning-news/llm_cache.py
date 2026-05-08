"""LLM 分析结果的跨运行缓存（SQLite，URL hash 为 key）。

以 URL hash 为 key，缓存完整的 LLM 分析结果 JSON。TTL 默认 7 天，过期自动清理。

用法：
    cache = LLMCache("llm_cache.db")
    result = cache.get(url)
    if result is None:
        result = analyzer.analyze_article(...)
        cache.set(url, result)
    cache.close()
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional


class LLMCache:
    """LLM 分析结果的跨运行缓存。"""

    def __init__(self, db_path: str, ttl_days: int = 7):
        self.ttl_days = ttl_days
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                url_hash TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.db.commit()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _hash(url: str) -> str:
        normalized = url.strip().rstrip('/').lower()
        normalized = re.sub(r'^https?://(www\.)?', '', normalized)
        # 去 fragment + 常见追踪参数，保留其他查询参数（如 YouTube 的 ?v=XXX）
        normalized = re.sub(r'#.*$', '', normalized)
        normalized = re.sub(r'[?&](utm_\w+|ref|fbclid|gclid|source|mc_\w+)=[^&]*', '', normalized)
        normalized = re.sub(r'\?&', '?', normalized)
        normalized = re.sub(r'\?$', '', normalized)
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]

    def get(self, url: str) -> Optional[dict]:
        """查找缓存。返回 None 表示 miss。"""
        if not url:
            self._misses += 1
            return None
        h = self._hash(url)
        row = self.db.execute(
            "SELECT result_json, created_at FROM llm_cache WHERE url_hash = ?", (h,)
        ).fetchone()
        if row is None:
            self._misses += 1
            return None
        try:
            created = datetime.fromisoformat(row[1])
            age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
            if age_days > self.ttl_days:
                self.db.execute("DELETE FROM llm_cache WHERE url_hash = ?", (h,))
                self.db.commit()
                self._misses += 1
                return None
        except (ValueError, TypeError):
            pass
        self._hits += 1
        return json.loads(row[0])

    def set(self, url: str, result: dict):
        """写入缓存。"""
        if not url or not result:
            return
        h = self._hash(url)
        self.db.execute(
            "INSERT OR REPLACE INTO llm_cache (url_hash, result_json, created_at) VALUES (?, ?, ?)",
            (h, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat())
        )
        self.db.commit()

    def delete(self, url: str):
        """删除指定 URL 的缓存条目。"""
        if not url:
            return
        h = self._hash(url)
        self.db.execute("DELETE FROM llm_cache WHERE url_hash = ?", (h,))
        self.db.commit()

    def cleanup(self):
        """清理过期条目。"""
        cutoff = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "DELETE FROM llm_cache WHERE created_at < datetime(?, ?)",
            (cutoff, f'-{self.ttl_days} days')
        )
        self.db.commit()

    @property
    def stats(self) -> str:
        return f"hits={self._hits}, misses={self._misses}"

    def close(self):
        self.db.close()
