"""extractors/hf_papers.py — Hugging Face Daily Papers 抓取

HF 官方无 RSS feed 但有 JSON API：
    https://huggingface.co/api/daily_papers?date=YYYY-MM-DD

每天社区精选 20-40 篇论文，信噪比远高于 ArXiv 全量。协议注册为
`hf-papers://daily`，由 content_fetcher 分发到这里。

拉最近 N 天的 JSON 合并、按 upvotes 排序、输出为 article list。
"""

from datetime import datetime, timedelta, timezone
import json

from logger import get_logger
from tls_client import fetch_bytes

log = get_logger('hf_papers')

_API_URL = "https://huggingface.co/api/daily_papers?date={date}"


def fetch_hf_daily_papers(source: dict, max_items: int, max_age_hours: int,
                          health_tracker, days_lookback: int = 7) -> list:
    """拉最近 days_lookback 天的 HF Daily Papers，合并去重、按 upvotes 排。

    返回 list of article dict 兼容 content_fetcher 下游调用。
    """
    name = source.get('name', 'HF Daily Papers')
    health_tracker.start_timer(name)
    log.info("📡 抓取 %s（近 %d 天）...", name, days_lookback)

    now = datetime.now(timezone.utc)
    seen_ids = set()
    merged = []
    err_count = 0

    for i in range(days_lookback):
        day = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        url = _API_URL.format(date=day)
        try:
            data, status, _ = fetch_bytes(url, timeout=15)
            papers = json.loads(data.decode('utf-8'))
            if not isinstance(papers, list):
                continue
            for p in papers:
                paper = p.get('paper', {}) or {}
                pid = paper.get('id', '')
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                title = p.get('title') or paper.get('title', '')
                summary = (p.get('summary') or paper.get('summary', '') or '')[:600]
                upvotes = paper.get('upvotes', 0) or 0
                pub_raw = p.get('publishedAt') or paper.get('publishedAt', '')
                try:
                    pub = datetime.fromisoformat(pub_raw.replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    pub = now
                # HF Papers 是 tier 1，上游 collector 分层过滤会允许 168h。
                # 这里只做 days_lookback*24 的硬上限保底。
                age_h = (now - pub).total_seconds() / 3600
                if age_h > days_lookback * 24:
                    continue
                merged.append({
                    'title': title,
                    'link': f"https://huggingface.co/papers/{pid}",
                    'summary': summary,
                    'full_text': summary,
                    'published': pub,
                    'source_name': source['name'],
                    'source_icon': source.get('icon', '🤗'),
                    'source_color': source.get('color', '#FFD21E'),
                    'source_category': source.get('category', 'research'),
                    '_upvotes': upvotes,
                })
        except Exception as e:
            err_count += 1
            log.debug("HF papers %s: %s", day, e)

    # 按 upvotes 降序
    merged.sort(key=lambda a: -a.get('_upvotes', 0))
    merged = merged[:max_items]

    if merged:
        health_tracker.record_success(name, len(merged))
        log.info("✅ %s: 获取 %d 篇（去重后，top upvotes: %s）",
                 name, len(merged),
                 ', '.join(str(a.get('_upvotes', 0)) for a in merged[:3]))
    elif err_count >= days_lookback:
        health_tracker.record_failure(name, f"连续 {err_count} 天 API 失败")
        log.warning("⚠️ %s: 无数据", name)
    else:
        log.info("ℹ️ %s: 近期无新论文满足时间窗", name)

    return merged
