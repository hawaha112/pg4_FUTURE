#!/usr/bin/env python3
"""backfill_polluted_analysis.py — 重跑 LLM 深度字段全空的 ai_relevant 文章

背景:
    OAuth token 失效期间, 每次 LLM 调用 401 → analyzer._fallback() 写出
    Level 1 但 background/deep_analysis/detailed_content 都为空的 analysis JSON。
    这些"污染"文章在 _has_substance 里全被过滤, 导致出报 kept=0。

本脚本:
    1. 找最近 N 小时 ai_relevant=1 但 LLM 深度字段全空的文章
    2. 调 LLMAnalyzer.analyze_article 重分析
    3. 写回 articles.analysis 并同步 canonical_events.analysis (该 article
       是事件主文时)

用法:
    python3 backfill_polluted_analysis.py [小时数, 默认 48]
"""

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from llm_analyzer import LLMAnalyzer
from logger import get_logger

log = get_logger('backfill')


def is_polluted(analysis_str):
    if not analysis_str:
        return False
    try:
        d = json.loads(analysis_str)
    except Exception:
        return False
    if d.get('_analysis_level', -1) != 1:
        return False
    bg = (d.get('background') or '').strip()
    da = (d.get('deep_analysis') or '').strip()
    dc = (d.get('detailed_content') or '').strip()
    return not (bg or da or len(dc) >= 100)


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 48

    cfg_path = Path(__file__).parent / 'config.json'
    cfg = json.load(open(cfg_path, encoding='utf-8'))['llm']
    if not cfg.get('enabled'):
        log.error('LLM disabled in config, abort')
        return 1

    analyzer = LLMAnalyzer(
        base_url=cfg['base_url'],
        api_key=cfg.get('api_key', ''),
        model=cfg.get('model', 'gpt-4o-mini'),
        provider=cfg.get('provider', 'openai'),
        max_retries=cfg.get('max_retries', 3),
        timeout=cfg.get('timeout', 60),
        max_workers=cfg.get('max_workers', 4),
        temperature=cfg.get('temperature', 0.3),
        max_tokens=cfg.get('max_tokens', 2000),
    )

    db_path = Path(__file__).parent / 'events.db'
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    rows = db.execute("""
        SELECT id, title, summary, full_text, source_name, analysis
        FROM articles
        WHERE ai_relevant=1
          AND collected_at >= datetime('now', ?)
    """, (f'-{hours} hours',)).fetchall()

    polluted = [dict(r) for r in rows if is_polluted(r['analysis'])]
    log.info("近 %dh ai_relevant=%d, 污染=%d", hours, len(rows), len(polluted))
    if not polluted:
        log.info("无需 backfill ✅")
        return 0

    success = 0
    fail = 0

    def work(r):
        try:
            new_a = analyzer.analyze_article(
                title=r['title'] or '',
                summary=r['summary'] or '',
                full_text=r['full_text'] or '',
                source_name=r['source_name'] or '',
            )
            if not new_a or not new_a.get('summary'):
                return r['id'], None, 'empty result'
            new_a['_analysis_level'] = 1
            return r['id'], json.dumps(new_a, ensure_ascii=False), None
        except Exception as e:
            return r['id'], None, str(e)[:120]

    workers = max(1, min(int(cfg.get('max_workers', 4)), 8))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, r) for r in polluted]
        for i, f in enumerate(as_completed(futures), 1):
            aid, new_json, err = f.result()
            if new_json:
                db.execute("UPDATE articles SET analysis=? WHERE id=?",
                           (new_json, aid))
                db.execute(
                    "UPDATE canonical_events SET analysis=? "
                    "WHERE canonical_article_id=?",
                    (new_json, aid),
                )
                success += 1
            else:
                fail += 1
                log.warning("  ❌ id=%s err=%s", (aid or '?')[:8], err)
            if i % 10 == 0:
                db.commit()
                log.info("进度 %d/%d  ✅%d  ❌%d", i, len(polluted), success, fail)

    db.commit()
    db.close()
    log.info("✅ backfill 完成: 成功 %d, 失败 %d / 共 %d",
             success, fail, len(polluted))
    return 0 if success > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
