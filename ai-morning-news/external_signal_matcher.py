"""
external_signal_matcher.py — 把外部热度信号 (HN/Reddit/HF/GitHub) 关联到 canonical_event.

匹配策略 (按置信度从高到低):
1. URL 精确匹配: event.canonical_url == signal.url
2. URL hostname + path 匹配 (排除 query string 和锚点)
3. title 关键词重叠: 共有 >=3 个非停用词或共有专有名词
4. event.entity_tags 与 signal.title 关键词交叉

匹配上的事件:
- 加 external_signals dict (含每个源的最高分)
- 计算 effective_importance: max(LLM_importance, signal_boost_level)
  - HN points >= 1000 或 Reddit subreddit 上 hot → 加 1 级
  - HN points >= 500 / HF likes >= 1000 → 至少 4 级
  - 多个外部源同时命中 → 加 1 级 (跨平台共识)

UI 在卡片角标显示热度 (🔥 HN 1500 / ⭐ HF 2000)
"""

import json
import re
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from logger import get_logger

log = get_logger('external_signals')


# 中英文停用词 (匹配 title 时跳过)
_STOPWORDS = frozenset({
    # 英文
    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'about',
    'and', 'or', 'but', 'not', 'no', 'as', 'this', 'that', 'these', 'those',
    'it', 'its', 'they', 'them', 'their', 'we', 'you', 'i', 'he', 'she',
    'how', 'what', 'when', 'where', 'why', 'who', 'which', 'has', 'have',
    'will', 'would', 'can', 'could', 'should', 'may', 'might', 'do', 'does',
    'new', 'now', 'just', 'more', 'most', 'one', 'two', 'some', 'all', 'any',
    'show', 'ask', 'hn', 'reddit', 'd', 'p', 'r',
    # 中文 (简单)
    '的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都',
    '今', '日', '昨', '明', '年', '月', '天', '时',
})


def _norm_url(url: str) -> str:
    """规范化 URL: 去 trailing slash / query / fragment, lowercase host."""
    if not url:
        return ''
    try:
        p = urllib.parse.urlparse(url)
        host = (p.hostname or '').lower()
        path = (p.path or '').rstrip('/').lower()
        return f'{host}{path}'
    except Exception:
        return url.lower().rstrip('/')


def _extract_keywords(text: str) -> set:
    """从 title/summary 中提取非停用词关键词 (cased keep — 保留专有名词大小写)."""
    if not text:
        return set()
    # 中英分词: 英文按空格+标点切, 中文按字
    # 英文部分: 提取连续 ASCII 单词 (含连字符)
    en_words = re.findall(r'[A-Za-z][A-Za-z0-9-]*', text)
    en_set = {w.lower() for w in en_words if len(w) >= 2 and w.lower() not in _STOPWORDS}
    # 中文部分: 提取连续中文 2 字以上
    cn_words = re.findall(r'[一-鿿]{2,}', text)
    cn_set = set(cn_words) - _STOPWORDS
    return en_set | cn_set


def load_hot_signals(path: Path) -> Dict[str, List[Dict]]:
    """加载 hot_signals.json. 返回 {hn: [...], reddit: [...], hf: [...], github: [...]}."""
    if not path.exists():
        return {'hn': [], 'reddit': [], 'hf': [], 'github': []}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        signals = data.get('signals', {}) if isinstance(data, dict) else {}
        return {
            'hn': signals.get('hn', []),
            'reddit': signals.get('reddit', []),
            'hf': signals.get('hf', []),
            'github': signals.get('github', []),
        }
    except (json.JSONDecodeError, OSError) as e:
        log.warning("⚠️ 加载 hot_signals.json 失败: %s", e)
        return {'hn': [], 'reddit': [], 'hf': [], 'github': []}


def _match_signal_to_event(event: dict, signal: dict, source_type: str) -> Optional[Tuple[str, float]]:
    """判断一个信号是否能匹配到一个事件.

    返回 (匹配方式, 置信度 0-1) 或 None.
    """
    # 1. URL 精确/规范化匹配
    sig_url = _norm_url(signal.get('url', ''))
    ev_url = _norm_url(event.get('canonical_url', ''))
    if sig_url and ev_url and sig_url == ev_url:
        return ('url_exact', 1.0)

    # 2. evidence_urls (所有源报道的 URL 集合) 中匹配
    evidence_urls = event.get('evidence_urls') or []
    if isinstance(evidence_urls, list):
        for ev_u in evidence_urls:
            if sig_url and sig_url == _norm_url(str(ev_u)):
                return ('url_evidence', 0.95)

    # 3. title 关键词重叠
    ev_kw = _extract_keywords(event.get('title', ''))
    if not ev_kw:
        ev_kw = _extract_keywords(event.get('summary', ''))
    sig_kw = _extract_keywords(signal.get('title', ''))
    if ev_kw and sig_kw:
        overlap = ev_kw & sig_kw
        # 需要 >= 3 关键词重合 + 至少 1 个"有信息量"的词
        if len(overlap) >= 3:
            return ('title_keywords', 0.7)
        # 2 关键词但其中含已知品牌/产品 (高置信度)
        brand_words = {'openai', 'anthropic', 'claude', 'gpt', 'chatgpt', 'codex',
                       'gemini', 'llama', 'mistral', 'deepseek', 'qwen', 'nvidia',
                       'sora', 'dalle', 'gemma', 'whisper', 'midjourney',
                       'huggingface', 'cursor', 'perplexity', 'cerebras', 'tomoro',
                       'bytedance', 'kimi', 'doubao', 'grok', 'xai', 'meta',
                       'apple', 'google', 'microsoft', 'amazon', 'cohere',
                       'agent', 'vllm', 'transformer', 'diffusion', 'rag', 'mcp',
                       'fine-tune', 'fine-tuning'}
        if len(overlap) >= 2 and (overlap & brand_words):
            return ('title_brand', 0.6)

    # 4. event entity_tags vs signal title 关键词交叉
    entity_tags = event.get('entity_tags') or []
    if isinstance(entity_tags, list) and entity_tags:
        ent_kw = {str(t).lower() for t in entity_tags}
        # HF / GitHub 的 title 是 owner/repo 形式, 拆开
        sig_title_low = signal.get('title', '').lower()
        if ent_kw & sig_kw:
            return ('entity_match', 0.5)
        # GitHub 仓库名常含品牌: deepseek-ai/DeepSeek-V4
        for ent in ent_kw:
            if ent in sig_title_low:
                return ('entity_in_title', 0.5)

    return None


def _signal_boost_level(signals: Dict[str, dict]) -> int:
    """根据匹配到的所有外部信号计算 importance boost (返回应达到的最低 importance).

    规则 (经验值, 后续可校准):
    - HN points >= 1000 OR Reddit best signal_score >= 1000 → 至少 5
    - HN points >= 500 OR HF likes >= 2000 → 至少 4
    - HN >= 200 OR HF likes >= 500 OR github stars >= 5000 → 至少 3
    - 同时匹配 ≥ 2 个不同外部源 → 再 +1 (跨平台共识)
    """
    levels = []
    if 'hn' in signals:
        pts = int(signals['hn'].get('points', 0) or 0)
        if pts >= 1000: levels.append(5)
        elif pts >= 500: levels.append(4)
        elif pts >= 200: levels.append(3)
    if 'reddit' in signals:
        # Reddit RSS 无分数,出现即弱信号 (默认 100, 不直接加)
        # 但子版块本身已是过滤器,所以仍至少 3
        levels.append(3)
    if 'hf' in signals:
        likes = int(signals['hf'].get('likes', 0) or 0)
        if likes >= 2000: levels.append(4)
        elif likes >= 500: levels.append(3)
    if 'github' in signals:
        stars = int(signals['github'].get('stars', 0) or 0)
        if stars >= 50000: levels.append(4)
        elif stars >= 5000: levels.append(3)

    if not levels:
        return 0

    base = max(levels)
    # 跨平台共识: 至少 2 个不同源 → +1
    if len(signals) >= 2:
        base = min(5, base + 1)
    return base


def enrich_events_with_signals(
    events: List[dict],
    hot_signals: Dict[str, List[Dict]],
) -> Tuple[List[dict], Dict[str, int]]:
    """给每个 canonical_event 加上 external_signals 字段, 并计算 effective_importance.

    Returns:
      (enriched_events, stats) - stats 含 matched_events, total_matches, etc.
    """
    stats = {
        'matched_events': 0,
        'matches_by_source': {'hn': 0, 'reddit': 0, 'hf': 0, 'github': 0},
        'boosted_events': 0,
        'total_signal_candidates': sum(len(v) for v in hot_signals.values()),
    }

    if stats['total_signal_candidates'] == 0:
        log.info("📡 无外部热度信号 (hot_signals.json 为空), 跳过事件 enrich")
        return events, stats

    log.info("📡 关联热度信号到事件 (候选信号: HN=%d Reddit=%d HF=%d GitHub=%d)",
             len(hot_signals['hn']), len(hot_signals['reddit']),
             len(hot_signals['hf']), len(hot_signals['github']))

    for event in events:
        # 收集所有匹配的信号 (按源取最高分)
        matched: Dict[str, dict] = {}
        for src in ('hn', 'reddit', 'hf', 'github'):
            best_signal = None
            best_conf = 0.0
            for sig in hot_signals.get(src, []):
                match = _match_signal_to_event(event, sig, src)
                if match:
                    _, conf = match
                    sig_score = int(sig.get('signal_score', 0) or 0)
                    # 用 conf * sig_score 综合判断最优
                    weighted = conf * (sig_score + 100)
                    if not best_signal or weighted > best_conf * (best_signal.get('signal_score', 0) + 100):
                        best_signal = sig
                        best_conf = conf
            if best_signal:
                matched[src] = {
                    'title': best_signal.get('title', ''),
                    'url': best_signal.get('url', ''),
                    'points': best_signal.get('points'),   # HN
                    'upvotes': best_signal.get('upvotes'),  # Reddit
                    'likes': best_signal.get('likes'),     # HF
                    'stars': best_signal.get('stars'),     # GitHub
                    'subreddit': best_signal.get('subreddit'),
                    'signal_score': best_signal.get('signal_score', 0),
                    'match_conf': round(best_conf, 2),
                }
                stats['matches_by_source'][src] += 1

        if matched:
            event['external_signals'] = matched
            stats['matched_events'] += 1
            # boost importance (仅 boost, 不降级)
            boost_level = _signal_boost_level(matched)
            orig = int(event.get('importance') or 0)
            if boost_level > orig:
                event['_original_importance'] = orig
                event['effective_importance'] = boost_level
                stats['boosted_events'] += 1

    log.info("📡 关联完成: %d 事件被匹配 (HN=%d Reddit=%d HF=%d GitHub=%d), %d 个被 boost importance",
             stats['matched_events'],
             stats['matches_by_source']['hn'],
             stats['matches_by_source']['reddit'],
             stats['matches_by_source']['hf'],
             stats['matches_by_source']['github'],
             stats['boosted_events'])
    return events, stats
