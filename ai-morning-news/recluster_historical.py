#!/usr/bin/env python3
"""recluster_historical.py — 一次性：把历史文章重新跑一次事件聚类

背景：
    之前 _cluster_text fallback bug 导致 189+ 个 article 全部建成独立 event
    （cluster_size=1）。bug 修复后新采集已能聚类，但**历史数据不会回溯合并**。

本脚本：
    1. 选取最近 N 小时的所有 AI 相关 articles
    2. 清理它们现有的 canonical_event / evidence 记录（从 articles.canonical_event_id 断开）
    3. 把它们当成新批次送进 EventClusterer.cluster_and_link
    4. 跑完后 canonical_events 表只保留有 evidence 的（无孤儿 event）

用法:
    python3 recluster_historical.py [小时数，默认 72]

前置：events.db 备份建议（run_daily.sh 每日会自动 rolling backup）
"""

import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, '.')

from event_store import EventStore
from event_cluster import EventClusterer
from logger import get_logger

log = get_logger('recluster')


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 72

    store = EventStore('events.db', auto_recover=False)
    db = store.db

    # ── 1. 选出近 N 小时 AI 相关文章 ──
    rows = db.execute("""
        SELECT id, url, title, summary, full_text, source_name, source_tier,
               source_icon, source_color, source_category, image_url,
               published_at, collected_at, analysis
        FROM articles
        WHERE ai_relevant = 1
          AND collected_at >= datetime('now', ?)
        ORDER BY collected_at ASC
    """, (f'-{hours} hours',)).fetchall()

    if not rows:
        log.warning("近 %dh 无 AI 相关文章", hours)
        return

    log.info("📦 载入 %d 篇文章（近 %dh）用于重聚类", len(rows), hours)

    articles = []
    article_ids = []
    for r in rows:
        a = json.loads(r[13] or '{}') if r[13] else {}
        articles.append({
            'url': r[1], 'link': r[1],
            'title': r[2] or '', 'summary': r[3] or '', 'full_text': r[4] or '',
            'source_name': r[5], 'source_tier': r[6],
            'source_icon': r[7], 'source_color': r[8], 'source_category': r[9],
            'image': r[10], 'image_url': r[10],
            'published': r[11], 'collected_at': r[12],
            'analysis': a,
            '_entities': a.get('_entities', []),
        })
        article_ids.append(r[0])

    # ── 2. 清理这些 articles 当前的聚类关联 ──
    placeholders = ','.join('?' * len(article_ids))

    # 2a. 找到将被重新关联的 event_ids
    old_event_ids = set()
    for r in db.execute(
        f"SELECT DISTINCT canonical_event_id FROM articles "
        f"WHERE id IN ({placeholders}) AND canonical_event_id IS NOT NULL AND canonical_event_id != ''",
        article_ids,
    ).fetchall():
        if r[0]:
            old_event_ids.add(r[0])
    log.info("📎 清理旧关联：%d 个 event、%d 篇 article", len(old_event_ids), len(article_ids))

    # 2b. 断开 articles → event
    db.execute(
        f"UPDATE articles SET canonical_event_id = NULL WHERE id IN ({placeholders})",
        article_ids,
    )

    # 2c. 删 evidence 行
    if old_event_ids:
        ev_ph = ','.join('?' * len(old_event_ids))
        db.execute(f"DELETE FROM evidence WHERE event_id IN ({ev_ph})", list(old_event_ids))

    # 2d. 删掉无剩余 evidence 的孤儿 canonical_event
    # 查哪些 old_event_id 在 evidence 表中还有记录（其他非本次 article 挂载）
    if old_event_ids:
        ev_ph = ','.join('?' * len(old_event_ids))
        orphans = db.execute(
            f"SELECT ce.event_id FROM canonical_events ce "
            f"WHERE ce.event_id IN ({ev_ph}) "
            f"  AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.event_id = ce.event_id)",
            list(old_event_ids),
        ).fetchall()
        orphan_ids = [o[0] for o in orphans]
        if orphan_ids:
            op_ph = ','.join('?' * len(orphan_ids))
            db.execute(f"DELETE FROM canonical_events WHERE event_id IN ({op_ph})", orphan_ids)
            log.info("🗑️  删除 %d 个孤儿 canonical_event", len(orphan_ids))

    store.commit()

    # ── 3. 重新聚类 ──
    clusterer = EventClusterer(
        store,
        similarity_threshold=0.40,
        similarity_threshold_cjk=0.35,
        merge_window_hours=hours,
    )
    stats = clusterer.cluster_and_link(articles)
    log.info("🔁 重聚类完成: %s", stats)

    # ── 4. 查看新的 cluster_size 分布 ──
    rows2 = db.execute("""
        SELECT cnt, COUNT(*) FROM (
            SELECT event_id, COUNT(*) AS cnt FROM evidence GROUP BY event_id
        ) GROUP BY cnt ORDER BY cnt
    """).fetchall()
    log.info("📊 cluster_size 分布:")
    for cnt, n in rows2:
        log.info("  %d 篇/event:  %d 个", cnt, n)

    top = db.execute("""
        SELECT ce.title, COUNT(e.id) AS n
        FROM canonical_events ce
        JOIN evidence e ON e.event_id = ce.event_id
        GROUP BY ce.event_id
        HAVING n >= 2
        ORDER BY n DESC LIMIT 10
    """).fetchall()
    log.info("🎯 多源事件 Top:")
    for title, n in top:
        log.info("  %dx  %s", n, title[:70])

    store.close()


if __name__ == '__main__':
    main()
