#!/usr/bin/env python3
"""卡片配图清洗 — 剔除烂源图, 给无图卡片让位设计感占位图。

源站 og:image 常是与具体新闻无关的通用栏目封面 / 站点 logo / 噪声图。这里在
渲染前把这类"宁可没有也别放"的图剔掉(置空 item['image']); 若 extra_images 里
有真正的正文图就替补。置空后渲染端(html_generator)会落到设计感占位图。

2026-06-14 用户反馈"有些卡片图一点都不好" + "没有的能否补图"。AI 出图因免 key
服务(Pollinations)转付费限流暂不可靠, 先把"剔烂图 + 占位图"这条 100% 可靠的路做实。
"""
import re
from collections import Counter

# 噪声图特征(与 extractors/article.py 同源, 这里也作用于主图 og:image —— 原逻辑只过滤正文抓取图)
_NOISE_RE = re.compile(
    r'data:image|\.svg(?:[?#]|$)|sprite|placeholder|/logo|logo\.|favicon|avatar|gravatar|'
    r'/icon/|/icons/|[-_]icon\.|spacer|blank\.|transparent\.|[/_-]1x1[/_.]|pixel\.|tracking|'
    r'qrcode|/badges?/|/ads?[-/]|sponsor|default[-_]?(?:share|cover|avatar|image)|share[-_]?img',
    re.IGNORECASE,
)

# 已知"通用栏目封面"——同一张图被源站反复用于不同文章, 与新闻内容无关
_KNOWN_GENERIC = (
    'dao_li_cover',   # 爱范儿 早晚报通用封面
)


def is_bad_image(url) -> bool:
    """该图是否"宁可没有"(空/噪声/已知通用封面)。"""
    u = (url or '').strip()
    if not u or not u.startswith('http'):
        return True
    if _NOISE_RE.search(u):
        return True
    low = u.lower()
    return any(g in low for g in _KNOWN_GENERIC)


def clean_card_images(items, generic_threshold: int = 3) -> int:
    """剔除烂主图; 能替补就用 extra_images 里第一张干净图。返回被置空(无替补)的条数。

    generic_threshold: 同一 image_url 被 ≥N 个不同条目共用 → 判为通用封面, 一律剔除。
    """
    freq = Counter()
    for it in items:
        u = (it.get('image') or '').strip()
        if u:
            freq[u] += 1

    emptied = 0
    for it in items:
        u = (it.get('image') or '').strip()
        if not u:
            continue
        bad = is_bad_image(u) or freq.get(u, 0) >= generic_threshold
        if not bad:
            continue
        # 尝试从正文图里替补一张干净、非通用的
        repl = ''
        for cand in (it.get('extra_images') or []):
            c = (cand or '').strip()
            if c and c != u and not is_bad_image(c) and freq.get(c, 0) < generic_threshold:
                repl = c
                break
        it['image'] = repl
        if not repl:
            emptied += 1
    return emptied
