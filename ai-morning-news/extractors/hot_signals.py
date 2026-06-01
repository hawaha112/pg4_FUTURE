"""
hot_signals.py — 外部热度信号抓取 (HN / Reddit / HF Trending / GitHub Trending)

不是新闻源,而是"流量信号":告诉系统今天大家在哪里讨论什么/star/up vote.
信号用途:
1. 关联到 canonical_event (URL 模糊匹配 + title 关键词) → boost importance
2. 在 UI 显示为热度标签 (🔥 HN 1500 / 💬 Reddit 850 / ⭐ HF 200)
3. 反向发现盲区: 外部很火但我们没收录 → 加入候选信源

并发抓取, 单个源失败不影响其他, 任何源出错都不会阻塞 collector 流水线.
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import re
import time
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from logger import get_logger

log = get_logger('hot_signals')

# 共用 SSL 上下文 (项目其他地方一样的处理)
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

_DEFAULT_UA = (
    'pg4-future-hot-signals/1.0 '
    '(+https://github.com/hawaha112/pg4_FUTURE)'
)
_DEFAULT_TIMEOUT = 15


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: int = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """带 UA + SSL 的 GET, 返回 JSON; 失败返回 None."""
    req_headers = {'User-Agent': _DEFAULT_UA, 'Accept': 'application/json'}
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, json.JSONDecodeError) as e:
        log.warning("  ⚠️ HTTP/JSON 抓取失败 %s: %s", url[:60], str(e)[:80])
        return None


def _http_get_text(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: int = _DEFAULT_TIMEOUT) -> Optional[str]:
    """带 UA + SSL 的 GET, 返回 text; 失败返回 None."""
    req_headers = {'User-Agent': _DEFAULT_UA}
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.warning("  ⚠️ HTTP 抓取失败 %s: %s", url[:60], str(e)[:80])
        return None


# ─────────────────────────────────────────────────────────
# Hacker News
# ─────────────────────────────────────────────────────────

def fetch_hn_top(limit: int = 30, hours: int = 36) -> List[Dict]:
    """抓 Hacker News 最近 N 小时内 AI 相关 top stories.

    用 Algolia 公共 API: https://hn.algolia.com/api/
    无需 API key.
    """
    since_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    # 用按点数排序的 API (search API)
    params = {
        'tags': 'story',
        'numericFilters': f'created_at_i>={since_ts},points>=50',
        'hitsPerPage': str(limit * 2),  # 多取一点, 然后我们筛 AI 相关
    }
    url = f'https://hn.algolia.com/api/v1/search?{urllib.parse.urlencode(params)}'
    data = _http_get_json(url)
    if not data:
        return []

    # AI 关键词过滤 (HN search 不支持 OR, 自己筛)。用词边界正则而非朴素子串 ——
    # 否则 'ai' 会命中 "br(ai)n"/"ch(ai)r"/"(ai)r travel" 等, 把非 AI 帖(实测
    # "Creatine raises brain energy" 518 分)误判成 AI 突发。'ai'/'rag' 要求整词,
    # 其余允许前缀(agent→agents、fine-tun→fine-tuning…)。
    ai_kw_re = re.compile(
        r'(?:\bai\b|\brag\b|'
        r'\b(?:llm|gpt|claude|gemini|llama|mistral|openai|anthropic|deepmind|'
        r'huggingface|transformer|diffusion|neural|machine\s+learning|agent|'
        r'inference|embedding|fine[-\s]?tun|open[-\s]?source|mlx))',
        re.IGNORECASE,
    )
    hits = data.get('hits', [])
    results = []
    for h in hits:
        title = (h.get('title') or '').strip()
        if not title:
            continue
        title_lc = title.lower()
        if not ai_kw_re.search(title_lc):
            continue
        url_field = h.get('url') or f'https://news.ycombinator.com/item?id={h.get("objectID")}'
        results.append({
            'source': 'hn',
            'title': title,
            'url': url_field,
            'points': int(h.get('points') or 0),
            'comments': int(h.get('num_comments') or 0),
            'author': h.get('author', ''),
            'created_at': h.get('created_at', ''),
            'hn_id': str(h.get('objectID', '')),
            # 综合分: points + 0.5 * comments
            'signal_score': int(h.get('points') or 0) + int(h.get('num_comments') or 0) // 2,
        })
        if len(results) >= limit:
            break

    results.sort(key=lambda x: -x['signal_score'])
    log.info("  ✅ HN: 抓到 %d 条 AI 相关 top story (>=50 points, <%dh)", len(results), hours)
    return results


# ─────────────────────────────────────────────────────────
# Reddit (r/MachineLearning, r/LocalLLaMA, r/singularity, r/OpenAI)
# ─────────────────────────────────────────────────────────

_REDDIT_SUBS = ('MachineLearning', 'LocalLLaMA', 'singularity', 'OpenAI', 'artificial')

def fetch_reddit_hot(subreddits: Optional[List[str]] = None, limit_per_sub: int = 15) -> List[Dict]:
    """抓 Reddit AI 子版块 hot stories.

    背景: Reddit 已对 /r/X/hot.json 匿名请求返回 403 (需要 OAuth).
    退而求次用 RSS endpoint /r/X/hot.rss, 仍可用且无需鉴权.
    缺点: 没有 score/comments 数 (RSS 不携带). 我们用"出现在 hot RSS"本身
    作为弱信号 (score 默认设 100, 让它至少不被零分过滤掉).
    """
    subs = subreddits or _REDDIT_SUBS
    results = []
    for sub in subs:
        url = f'https://www.reddit.com/r/{sub}/hot.rss?limit={limit_per_sub}'
        xml_text = _http_get_text(url, headers={'User-Agent': _DEFAULT_UA})
        if not xml_text:
            continue
        # 解析 Atom RSS — <entry> 块, 取 title / link
        for entry_match in re.finditer(
            r'<entry>(.*?)</entry>', xml_text, re.S
        ):
            block = entry_match.group(1)
            t_match = re.search(r'<title[^>]*>(.*?)</title>', block, re.S)
            l_match = re.search(r'<link[^>]*href="([^"]+)"', block)
            if not (t_match and l_match):
                continue
            title = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', t_match.group(1)).strip()
            link = l_match.group(1).strip()
            if not title or not link:
                continue
            results.append({
                'source': 'reddit',
                'subreddit': sub,
                'title': title,
                'url': link,
                'permalink': link,
                'upvotes': 100,        # RSS 不含具体数,用"出现在 hot"作弱信号
                'comments': 0,
                'created_utc': None,
                'signal_score': 100,   # 默认 100 (低于 HN 高分但高于零)
            })

    # 去重: 同 url 只留一条
    seen = set()
    deduped = []
    for r in results:
        if r['url'] in seen:
            continue
        seen.add(r['url'])
        deduped.append(r)

    log.info("  ✅ Reddit: 抓到 %d 条 hot.rss (跨 %d sub)", len(deduped), len(subs))
    return deduped[:50]


# ─────────────────────────────────────────────────────────
# HuggingFace Trending (models / datasets / spaces)
# ─────────────────────────────────────────────────────────

def fetch_hf_trending(types: Optional[List[str]] = None, limit_per_type: int = 15) -> List[Dict]:
    """抓 HuggingFace 各类目的 trending 排行.

    /api/models?sort=likes7d 之类的端点目前最稳.
    """
    types_list = types or ['models', 'datasets', 'spaces']
    results = []
    for type_key in types_list:
        url = f'https://huggingface.co/api/{type_key}?sort=likes7d&direction=-1&limit={limit_per_type}'
        data = _http_get_json(url)
        if not data:
            continue
        items = data if isinstance(data, list) else data.get('items', [])
        for it in items:
            mid = it.get('id') or it.get('modelId') or ''
            if not mid:
                continue
            likes = int(it.get('likes') or 0)
            if likes < 5:
                continue
            results.append({
                'source': 'hf',
                'type': type_key.rstrip('s'),  # model / dataset / space
                'title': mid,
                'url': f'https://huggingface.co/{mid}' if type_key != 'spaces' else f'https://huggingface.co/spaces/{mid}',
                'likes': likes,
                'downloads': int(it.get('downloads') or 0) if type_key == 'models' else 0,
                # 综合分: HF 的 likes 数量级比 HN/Reddit 小, 放大处理
                'signal_score': likes * 10 + (int(it.get('downloads') or 0) // 1000 if type_key == 'models' else 0),
            })

    results.sort(key=lambda x: -x['signal_score'])
    log.info("  ✅ HF Trending: 抓到 %d 条 (跨 %s)", len(results), ', '.join(types_list))
    return results[:50]


# ─────────────────────────────────────────────────────────
# GitHub Trending
# ─────────────────────────────────────────────────────────

def fetch_github_trending(limit: int = 20, days_back: int = 7) -> List[Dict]:
    """抓 GitHub 上 AI 相关的 trending repos.

    用 GitHub Search API (无需 token, 公开 rate limit 10 req/min):
      q=topic:llm OR topic:ai OR topic:agent OR ...
      created:>YYYY-MM-DD (最近 N 天创建的)
      sort=stars

    比解析 /trending HTML 稳定得多 (HTML 结构经常改).
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')
    # 用 stars:>20 + AI 相关 topic, 按 stars 降序
    # 注意: GitHub search query 用空格做 AND, 但我们要 OR topic, 用 q= 的 topic: 字段
    queries = [
        f'topic:llm pushed:>={since} stars:>50',
        f'topic:agent pushed:>={since} stars:>50',
        f'topic:ai pushed:>={since} stars:>100',
    ]
    seen = set()
    results = []
    for q in queries:
        params = urllib.parse.urlencode({'q': q, 'sort': 'stars', 'order': 'desc', 'per_page': 20})
        url = f'https://api.github.com/search/repositories?{params}'
        data = _http_get_json(url, headers={'Accept': 'application/vnd.github+json'})
        if not data:
            continue
        items = data.get('items', [])
        for it in items:
            full_name = it.get('full_name', '')
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)
            stars = int(it.get('stargazers_count') or 0)
            forks = int(it.get('forks_count') or 0)
            description = (it.get('description') or '')[:140]
            results.append({
                'source': 'github',
                'title': full_name,
                'url': it.get('html_url', f'https://github.com/{full_name}'),
                'description': description,
                'stars': stars,
                'forks': forks,
                'language': it.get('language', ''),
                'topics': it.get('topics', []),
                # 综合分: stars/100 + forks/50 (forks 反映被开发者引用的程度)
                'signal_score': stars // 100 * 10 + forks // 50,
            })

    results.sort(key=lambda x: -x['signal_score'])
    log.info("  ✅ GitHub Trending: 抓到 %d 条 AI 相关 repo (pushed >= %s)", len(results), since)
    return results[:limit]


# ─────────────────────────────────────────────────────────
# 聚合 + 持久化
# ─────────────────────────────────────────────────────────

def fetch_all_hot_signals(save_path: Optional[Path] = None,
                          max_workers: int = 4) -> Dict[str, List[Dict]]:
    """并发抓取 4 个源, 返回 {hn, reddit, hf, github} 字典.

    单源失败不影响其他源. 任何源都不会抛异常 — 失败时返回空列表.
    可选 save_path: 持久化到 JSON 文件供 renderer 加载.
    """
    log.info("📡 抓取外部热度信号 (HN / Reddit / HF / GitHub)...")
    t0 = time.time()

    fetchers: Dict[str, Callable[[], List[Dict]]] = {
        'hn': fetch_hn_top,
        'reddit': fetch_reddit_hot,
        'hf': fetch_hf_trending,
        'github': fetch_github_trending,
    }
    results: Dict[str, List[Dict]] = {k: [] for k in fetchers}

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fn): name for name, fn in fetchers.items()}
        for fut in as_completed(futures, timeout=60):
            name = futures[fut]
            try:
                results[name] = fut.result(timeout=30)
            except Exception as e:
                log.warning("  ❌ %s 抓取异常: %s", name, str(e)[:80])
                results[name] = []

    total = sum(len(v) for v in results.values())
    elapsed = time.time() - t0
    log.info("📡 热度信号抓取完成: HN=%d Reddit=%d HF=%d GitHub=%d (共 %d, %.1fs)",
             len(results['hn']), len(results['reddit']),
             len(results['hf']), len(results['github']),
             total, elapsed)

    if save_path:
        try:
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'signals': results,
            }
            save_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
            log.info("📡 持久化到 %s", save_path)
        except OSError as e:
            log.warning("  ⚠️ 热度信号持久化失败: %s", e)

    return results


if __name__ == '__main__':
    # 命令行调试: python -m extractors.hot_signals
    import sys
    out = fetch_all_hot_signals(save_path=Path('output/hot_signals.json'))
    print(f"\n=== Top HN AI ===")
    for s in out['hn'][:5]:
        print(f"  [{s['points']:>4}p {s['comments']:>3}c] {s['title'][:70]}")
    print(f"\n=== Top Reddit AI ===")
    for s in out['reddit'][:5]:
        print(f"  [r/{s['subreddit']:14s} {s['upvotes']:>5}↑] {s['title'][:60]}")
    print(f"\n=== Top HF Trending ===")
    for s in out['hf'][:5]:
        print(f"  [{s['type']:>7s} {s['likes']:>4}♥] {s['title'][:60]}")
    print(f"\n=== Top GitHub AI repos ===")
    for s in out['github'][:5]:
        print(f"  [{s['stars']:>6}★ {s.get('forks', 0):>4}fork] {s['title'][:50]} — {s.get('description', '')[:40]}")
