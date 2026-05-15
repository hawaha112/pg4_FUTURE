"""
html_generator.py - HTML page generation for AI Morning News

Generates the complete HTML page using external template files:
  templates/page.html  — HTML skeleton with $variable placeholders
  templates/style.css  — all CSS styles
  templates/script.js  — all JavaScript (modal, filter, search, keyboard)

Uses string.Template for substitution (zero external dependencies).
"""

import json
import re
from html import escape, unescape


def _safe_escape(text):
    """先 unescape 已有 HTML 实体（如 RSS 里的 &#x27;），再 escape 一次防 XSS。
    避免双重 escape 让 &#x27; 变成 &amp;#x27; 在浏览器里显示为字面量。"""
    return escape(unescape(str(text or '')))


def _render_editorial(raw: str) -> str:
    """把"今日速览"原文渲染成多段 HTML。

    新版 digest prompt 让 LLM 输出三层结构（主旋律 / 分类组 / 收束），用
    `\\n\\n` 分段、用 `**xxx**` 加粗主题。这里负责：
      1. 按空行切段
      2. 每段 escape 后把 `**...**` 还原成 `<strong>...</strong>`
      3. 包成 <p class="br-editorial">

    `**` 在 escape 之后仍是字面量（HTML 不转义星号），所以正则替换安全。
    """
    paragraphs = []
    for raw_para in (raw or '').split('\n\n'):
        para = raw_para.strip()
        if not para:
            continue
        body = _safe_escape(para)
        body = re.sub(r'\*\*([^*\n]+?)\*\*',
                      r'<strong class="br-bold">\1</strong>', body)
        paragraphs.append(f'<p class="br-editorial">{body}</p>')
    return ''.join(paragraphs) if paragraphs else ''
from datetime import datetime, timezone


def _format_local_time(pub_val) -> str:
    """把 published 字段统一格式化为本地时区的 'MM-DD HH:MM'。

    背景：源头的 published_at 通常是 UTC ISO 字符串（带 +00:00 或 Z），
    早期版本直接 strftime 输出 UTC 时间，导致"04-28 22:02 UTC"在北京时区
    应显示为"04-29 06:02"，但卡片上变成 04-28，让用户以为是昨天的内容。
    """
    if not pub_val:
        return ""
    try:
        if isinstance(pub_val, str):
            clean = pub_val.replace('Z', '+00:00')
            pub_val = datetime.fromisoformat(clean)
        # 若 naive，按 UTC 处理；统一 astimezone 到本地
        if pub_val.tzinfo is None:
            pub_val = pub_val.replace(tzinfo=timezone.utc)
        return pub_val.astimezone().strftime("%m-%d %H:%M")
    except (AttributeError, ValueError, TypeError):
        return ""
from pathlib import Path
from string import Template

from logger import get_logger

log = get_logger('html_generator')

# Template directory (relative to this file)
_TEMPLATE_DIR = Path(__file__).parent / 'templates'


def _load_template(name: str) -> str:
    """Load a template file from the templates/ directory."""
    path = _TEMPLATE_DIR / name
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# -- 来源类型中文映射 --
SOURCE_TYPE_LABELS = {
    "paper": "学术论文",
    "news": "新闻报道",
    "official": "官方发布",
    "opinion": "观点文章",
    "community": "社区讨论",
    "video": "视频",
}

# -- 源 tier / category → 徽章（图标 + 中文名） --
# tier 0 = 官方；tier 1 + category=academic/research → 学术；其余按 source_type
SOURCE_TIER_BADGES = {
    "official":   ("🏢", "官方"),
    "academic":   ("🎓", "学术"),
    "media":      ("📰", "媒体"),
    "community":  ("💬", "社区"),
    "video":      ("🎥", "视频"),
}

# -- 目标读者枚举 → 中文标签 + 图标 --
AUDIENCE_LABELS = {
    "researcher": ("研究员", "🔬"),
    "developer":  ("开发者", "💻"),
    "pm":         ("产品",   "📐"),
    "investor":   ("投资人", "📈"),
    "general":    ("大众",   "👥"),
}


def _source_badge(item):
    """返回 (icon, label) 作为源徽章；返回 None 时不渲染徽章。"""
    tier = item.get('source_tier', 2)
    if tier == 0:
        return SOURCE_TIER_BADGES["official"]
    src_type = (item.get('analysis', {}).get('source_type') or '').strip()
    if src_type == 'paper':
        return SOURCE_TIER_BADGES["academic"]
    if src_type == 'video':
        return SOURCE_TIER_BADGES["video"]
    if src_type == 'official':
        return SOURCE_TIER_BADGES["official"]
    if src_type == 'community':
        return SOURCE_TIER_BADGES["community"]
    if tier == 1:
        # tier 1 非 paper/video → 学术/研究源
        return SOURCE_TIER_BADGES["academic"]
    return SOURCE_TIER_BADGES["media"]


def _importance_dots(level):
    """生成重要性圆点 HTML — Tufte 风格，最小有效差异"""
    filled = min(max(level, 1), 5)
    colors = {1: '#555', 2: '#888', 3: '#c9a227', 4: '#e8913a', 5: '#e05252'}
    active_color = colors.get(filled, '#888')
    return ''.join(
        f'<span class="imp-dot" style="color:{active_color if i < filled else "rgba(255,255,255,0.12)"}">'
        f'{"●" if i < filled else "○"}</span>'
        for i in range(5)
    )


def _importance_label(level):
    """重要性文字标签"""
    labels = {1: "一般", 2: "关注", 3: "重要", 4: "很重要", 5: "重大事件"}
    return labels.get(level, "")


def generate_html(all_items, config, digest=None, meta=None):
    """生成重要性分层的 HTML 页面 — 必读区块 + 普通卡片网格

    meta: dict, optional — 可选的页面级元信息。支持字段:
        - llm_coverage (float, 0..1): LLM 深度分析覆盖率，< 0.5 时渲染页首 banner
        - llm_count (int): 有 LLM 分析的条目数
        - multi_source_count (int): 多源报道的事件数
    """
    now = datetime.now()
    date_str = now.strftime("%Y年%m月%d日")
    weekday_map = {0: '一', 1: '二', 2: '三', 3: '四', 4: '五', 5: '六', 6: '日'}
    weekday = weekday_map[now.weekday()]
    time_str = now.strftime("%H:%M")

    # 标题随班次切换：早班 → AI 早报；晚班 → AI 晚报；无 shift → AI 早报（兼容默认）
    import os as _os_briefing
    _shift = _os_briefing.environ.get('BRIEFING_SHIFT', '').lower()
    if _shift == 'pm':
        briefing_title = 'AI 晚报'
        briefing_emoji = '🌆'
    else:
        briefing_title = 'AI 早报'
        briefing_emoji = '📡'

    # 统计
    by_source = {}
    for item in all_items:
        src = item['source_name']
        by_source.setdefault(src, []).append(item)

    total = len(all_items)
    sources_count = len(by_source)

    avg_importance = 0
    imp_items = [i for i in all_items if i.get('analysis', {}).get('importance', 0) > 0]
    if imp_items:
        avg_importance = sum(i['analysis']['importance'] for i in imp_items) / len(imp_items)

    # 收集分类 + 读者
    all_categories = set()
    all_audiences = set()
    for item in all_items:
        for cat in item.get('analysis', {}).get('categories', []):
            all_categories.add(cat)
        for aud in item.get('analysis', {}).get('audience', []) or []:
            if aud in AUDIENCE_LABELS:
                all_audiences.add(aud)

    # ── 拆分为 featured（importance >= 4）和 regular ──
    featured_items = []
    regular_items = []
    modal_data = []

    # featured 门槛 ≥3（"重要"级别）— 避免 LLM 低估新版模型发布等条目（ChatGPT 5.4
    # 被打 ★3 等）。门槛降低后用排序把真正重大的顶到前面。
    today_str = now.strftime('%Y-%m-%d')
    for idx, item in enumerate(all_items):
        analysis = item.get('analysis', {})
        importance = analysis.get('importance', 1)

        if importance >= 3:
            featured_items.append((idx, item))
        else:
            regular_items.append((idx, item))

    def _is_today(item):
        """item.published 是否今天（按生成时的本地日期）"""
        pub = item.get('published')
        if not pub:
            return False
        try:
            if hasattr(pub, 'strftime'):
                return pub.strftime('%Y-%m-%d') == today_str
            if isinstance(pub, str) and len(pub) >= 10:
                return pub[:10] == today_str
        except (ValueError, TypeError, AttributeError):
            pass
        return False

    def _feat_key(pair):
        _idx, item = pair
        a = item.get('analysis', {}) or {}
        imp = a.get('importance', 1)
        # 多源 bonus（按 unique source 数）
        evidence_chain = item.get('_evidence_chain', []) or []
        unique_srcs = len({ev.get('source_name', '') for ev in evidence_chain if ev.get('source_name')})
        if unique_srcs == 0:
            unique_srcs = 1
        multi_bonus = 0.0
        if unique_srcs >= 4: multi_bonus = 1.0
        elif unique_srcs == 3: multi_bonus = 0.8
        elif unique_srcs == 2: multi_bonus = 0.5
        # 今日 bonus：新鲜度加权
        today_bonus = 0.7 if _is_today(item) else 0.0
        return -(imp + multi_bonus + today_bonus)
    featured_items.sort(key=_feat_key)

    # ── 今日三件大事：importance >= 4 且 cluster_size >= 2 的前 3 条 ──
    top3_items = []
    seen_idx = set()
    # 优先：importance=5 且多源
    for idx, item in featured_items:
        if len(top3_items) >= 3:
            break
        if item.get('analysis', {}).get('importance', 0) == 5 and item.get('_cluster_size', 1) >= 2:
            top3_items.append((idx, item))
            seen_idx.add(idx)
    # 补：importance=4 且多源
    for idx, item in featured_items:
        if len(top3_items) >= 3:
            break
        if idx in seen_idx:
            continue
        if item.get('analysis', {}).get('importance', 0) >= 4 and item.get('_cluster_size', 1) >= 2:
            top3_items.append((idx, item))
            seen_idx.add(idx)
    # 再补：importance >= 4（不要求多源）
    if len(top3_items) < 3:
        for idx, item in featured_items:
            if len(top3_items) >= 3:
                break
            if idx in seen_idx:
                continue
            top3_items.append((idx, item))
            seen_idx.add(idx)

    # ── 构建必读卡片（featured block）──
    featured_html = ""
    if featured_items:
        featured_html = '<div class="featured-section">\n<h2 class="featured-title">今日必读</h2>\n<div class="featured-grid">\n'

        for idx, item in featured_items:
            analysis = item.get('analysis', {})
            importance = analysis.get('importance', 1)

            # 中文标题处理
            chinese_title_raw = analysis.get('chinese_title', '') or ''
            raw_summary = analysis.get('summary', '') or ''
            if not raw_summary or raw_summary in ('无法提取摘要', '无法获取分析'):
                raw_summary = item.get('title', '')[:100]
            _is_entity_label = (
                chinese_title_raw
                and '|' in chinese_title_raw
                and len(chinese_title_raw) < 40
                and not any('\u4e00' <= c <= '\u9fff' for c in chinese_title_raw)
            )
            if not chinese_title_raw or _is_entity_label:
                fallback_src = raw_summary or item.get('title', '') or ''
                if len(fallback_src) <= 50:
                    chinese_title_raw = fallback_src
                else:
                    truncated = fallback_src[:50]
                    for sep in ['。', '，', '；', '. ', ', ', '; ', ' ']:
                        last_sep = truncated.rfind(sep)
                        if last_sep > 15:
                            chinese_title_raw = truncated[:last_sep + len(sep)].rstrip()
                            break
                    else:
                        chinese_title_raw = truncated.rstrip()

            chinese_title = _safe_escape(chinese_title_raw)
            # 卡片显示客观事实陈述(summary),不显示意义解读(why_it_matters)。
            # 后者跟 modal 里的 deep_analysis 内容重叠,留给 modal 展开看。
            card_summary = _safe_escape(analysis.get('summary', '') or raw_summary)
            categories = analysis.get('categories', ['其他'])

            pub_str = _format_local_time(item.get('published'))

            icon = item.get('source_icon', '📰')
            source_name = _safe_escape(item.get("source_name", ""))
            image_url = _safe_escape(item.get("image", ""))
            cat_data = '|'.join(categories)

            # 左侧边条颜色
            border_color = '#e05252' if importance == 5 else '#e8913a'

            # 图片区域
            img_html = ""
            if image_url:
                img_html = f'<div class="featured-img" style="background-image:url(\'{image_url}\')"></div>'

            # 标题
            title_html = f'<div class="featured-title-text">{chinese_title}</div>'

            # 卡片简介:用 summary(事实)而不是 why_it_matters(意义),后者留给 modal
            why_html = f'<div class="featured-why">{card_summary}</div>' if card_summary else ""

            # 来源徽章 + 时间
            tb = _source_badge(item)
            tier_badge_html = (
                f'<span class="tier-badge tier-{tb[1]}" title="{tb[1]}源">{tb[0]} {tb[1]}</span>'
                if tb else ''
            )
            time_part = f' · {pub_str}' if pub_str else ''

            src_html = (
                f'<div class="featured-source">'
                f'{tier_badge_html} {icon} {source_name}{time_part}'
                f'</div>'
            )

            # 多源详细列表：列出所有 evidence 的"其他媒体"报道（去重，同源多篇合并）
            # 注意：cluster_size 数的是 evidence 条数，同一家媒体发多篇也会 >=2，
            # 但"多源"业务语义应是 unique source > 1，所以这里按 source_name 去重。
            other_sources_html = ''
            evidence_chain = item.get('_evidence_chain', []) or []
            canonical_source = item.get('source_name', '') or ''
            others_map = {}  # source_name → earliest reported_at
            for ev in evidence_chain:
                sn = ev.get('source_name', '') or ''
                if not sn or sn == canonical_source:
                    continue
                ra = ev.get('reported_at', '') or ''
                # 同源保留最早时间
                if sn not in others_map or (ra and ra < others_map[sn]):
                    others_map[sn] = ra
            if others_map:
                def _fmt(ra):
                    try:
                        if ra:
                            return datetime.fromisoformat(ra.replace('Z', '+00:00')).strftime('%m-%d %H:%M')
                    except (ValueError, TypeError):
                        pass
                    return (ra[:16].replace('T', ' ')) if ra else ''
                rows = ''.join(
                    f'<div class="fos-item">· {_safe_escape(sn)}'
                    f'{" · " + _safe_escape(_fmt(ra)) if _fmt(ra) else ""}</div>'
                    for sn, ra in others_map.items()
                )
                other_sources_html = (
                    f'<div class="featured-other-sources">'
                    f'<div class="fos-label">📡 另有 {len(others_map)} 源报道</div>'
                    f'{rows}'
                    f'</div>'
                )
            multi_pill = ''  # 兼容下方模板字符串引用

            # importance=5 的 "行业级" 徽章（置于卡片顶部，左侧）
            hero_badge = ''
            if importance == 5:
                hero_badge = '<span class="hero-badge" title="行业格局级">🚀 行业级</span>'

            # data-audience 属性，供前端 tab 过滤
            aud_list = analysis.get('audience', []) or ['general']
            aud_data = '|'.join(a for a in aud_list if a in AUDIENCE_LABELS) or 'general'

            _iid = item.get('_event_id') or item.get('link') or f'i{idx}'
            featured_html += f'''    <div class="featured-card" data-cat="{_safe_escape(cat_data)}" data-aud="{_safe_escape(aud_data)}" data-idx="{idx}" data-iid="{_safe_escape(_iid)}" style="border-left-color: {border_color}">
        {img_html}
        <div class="featured-body">
            {hero_badge}
            {multi_pill}
            {title_html}
            {why_html}
            {src_html}
            {other_sources_html}
        </div>
    </div>
'''

        featured_html += '</div>\n</div>\n'

    # ── 构建普通卡片（regular grid）──
    cards_html = ""
    for idx, item in regular_items:
        analysis = item.get('analysis', {})

        # 中文标题处理
        chinese_title_raw = analysis.get('chinese_title', '') or ''
        raw_summary = analysis.get('summary', '') or ''
        if not raw_summary or raw_summary in ('无法提取摘要', '无法获取分析'):
            raw_summary = item.get('title', '')[:100]
        _is_entity_label = (
            chinese_title_raw
            and '|' in chinese_title_raw
            and len(chinese_title_raw) < 40
            and not any('\u4e00' <= c <= '\u9fff' for c in chinese_title_raw)
        )
        if not chinese_title_raw or _is_entity_label:
            fallback_src = raw_summary or item.get('title', '') or ''
            if len(fallback_src) <= 50:
                chinese_title_raw = fallback_src
            else:
                truncated = fallback_src[:50]
                for sep in ['。', '，', '；', '. ', ', ', '; ', ' ']:
                    last_sep = truncated.rfind(sep)
                    if last_sep > 15:
                        chinese_title_raw = truncated[:last_sep + len(sep)].rstrip()
                        break
                else:
                    chinese_title_raw = truncated.rstrip()

        chinese_title = _safe_escape(chinese_title_raw)
        # 卡片用 summary(事实陈述)替代 why_it_matters(意义),
        # 避免和 modal 的 deep_analysis 重复。
        card_summary = _safe_escape(analysis.get('summary', '') or raw_summary)
        categories = analysis.get('categories', ['其他'])
        source_type = analysis.get('source_type', 'news')
        reading_minutes = analysis.get('reading_minutes', 1)

        pub_str = _format_local_time(item.get('published'))

        icon = item.get('source_icon', '📰')
        source_name = _safe_escape(item.get("source_name", ""))
        cat_data = '|'.join(categories)
        image_url = _safe_escape(item.get("image", ""))

        # Z1: 分类 + 阅读时间
        cat_text = ' · '.join(_safe_escape(c) for c in categories[:2])
        z1_html = f'''<div class="z1">
            <span class="z1-left">{cat_text}</span>
            <span class="z1-meta">{reading_minutes} min</span>
        </div>'''

        # 图片区域
        img_html = ""
        if image_url:
            img_html = f'<div class="card-img" style="background-image:url(\'{image_url}\')"></div>'

        # 标题
        title_html = f'<div class="card-title">{chinese_title}</div>'

        # 卡片简介用 summary(事实陈述),不重复 modal 里的 deep_analysis(意义解读)
        z3_html = ""
        if card_summary:
            z3_html = f'<div class="z3"><div class="z3-why">{card_summary}</div></div>'

        # Z5: 来源徽章 + 来源 + 时间 + 事件状态（多源指示提升为独立 pill，见下方）
        tb = _source_badge(item)
        tier_badge_html = (
            f'<span class="tier-badge tier-{tb[1]}" title="{tb[1]}源">{tb[0]}</span>'
            if tb else ''
        )
        time_part = f' · {pub_str}' if pub_str else ''
        status_badge = ''
        if item.get('_event_status_display'):
            st = item['_event_status_display']
            status_badge = f' <span style="color:{st.get("color","#888")};font-size:9px;font-weight:600">{st.get("icon","")} {st.get("label","")}</span>'
        z5_html = f'<div class="z5">{tier_badge_html} {icon} {source_name}{time_part}{status_badge}</div>'

        # 多源报道 pill（普通卡片右上角）
        cluster_size = item.get('_cluster_size', 1) or 1
        report_count = item.get('_report_count', cluster_size) or cluster_size
        multi_pill = ''
        if cluster_size >= 2 or report_count >= 2:
            n = max(cluster_size, report_count)
            multi_pill = f'<span class="src-pill" title="共 {n} 个来源报道同一事件">📡 {n} 源</span>'

        # 外部热度信号 pill (P2): HN/Reddit/HF/GitHub 关联结果
        ext_signals = item.get('_external_signals') or {}
        hot_pills_html = ''
        if ext_signals:
            pill_parts = []
            if 'hn' in ext_signals:
                pts = int(ext_signals['hn'].get('points') or 0)
                hn_url = _safe_escape(ext_signals['hn'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-hn" target="_blank" rel="noopener" '
                    f'href="{hn_url}" title="HN {pts} 分">🔥 HN {pts}</a>'
                )
            if 'reddit' in ext_signals:
                sub = _safe_escape(ext_signals['reddit'].get('subreddit') or 'reddit')
                r_url = _safe_escape(ext_signals['reddit'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-reddit" target="_blank" rel="noopener" '
                    f'href="{r_url}" title="r/{sub} hot">💬 r/{sub}</a>'
                )
            if 'hf' in ext_signals:
                likes = int(ext_signals['hf'].get('likes') or 0)
                hf_url = _safe_escape(ext_signals['hf'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-hf" target="_blank" rel="noopener" '
                    f'href="{hf_url}" title="HuggingFace {likes} 个 likes">⭐ HF {likes}</a>'
                )
            if 'github' in ext_signals:
                stars = int(ext_signals['github'].get('stars') or 0)
                stars_disp = f'{stars // 1000}K' if stars >= 1000 else str(stars)
                gh_url = _safe_escape(ext_signals['github'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-gh" target="_blank" rel="noopener" '
                    f'href="{gh_url}" title="GitHub {stars} stars">🐙 {stars_disp}</a>'
                )
            if pill_parts:
                hot_pills_html = '<div class="hot-pills">' + ''.join(pill_parts) + '</div>'

        # data-audience 属性
        aud_list = analysis.get('audience', []) or ['general']
        aud_data = '|'.join(a for a in aud_list if a in AUDIENCE_LABELS) or 'general'

        _riid = item.get('_event_id') or item.get('link') or f'r{idx}'
        cards_html += f'''
        <div class="card" data-cat="{_safe_escape(cat_data)}" data-aud="{_safe_escape(aud_data)}" data-idx="{idx}" data-iid="{_safe_escape(_riid)}"
             style="animation-delay:{min(idx * 25, 500)}ms">
            {img_html}
            <div class="card-body">
                {multi_pill}
                {z1_html}
                {title_html}
                {z3_html}
                {hot_pills_html}
                {z5_html}
            </div>
        </div>'''

    # ── 为所有卡片构建 modal_data ──
    # 必须按 all_items 原顺序遍历，因为卡片的 data-idx 用的是 enumerate(all_items) 的 idx。
    # 如果按 featured+regular 顺序 append 会导致 modal_data[idx] 与 data-idx 错位（featured 卡点开显示错条目）。
    for idx, item in enumerate(all_items):
        analysis = item.get('analysis', {})

        chinese_title_raw = analysis.get('chinese_title', '') or ''
        raw_summary = analysis.get('summary', '') or ''
        if not raw_summary or raw_summary in ('无法提取摘要', '无法获取分析'):
            raw_summary = item.get('title', '')[:100]
        _is_entity_label = (
            chinese_title_raw
            and '|' in chinese_title_raw
            and len(chinese_title_raw) < 40
            and not any('\u4e00' <= c <= '\u9fff' for c in chinese_title_raw)
        )
        if not chinese_title_raw or _is_entity_label:
            fallback_src = raw_summary or item.get('title', '') or ''
            if len(fallback_src) <= 50:
                chinese_title_raw = fallback_src
            else:
                truncated = fallback_src[:50]
                for sep in ['。', '，', '；', '. ', ', ', '; ', ' ']:
                    last_sep = truncated.rfind(sep)
                    if last_sep > 15:
                        chinese_title_raw = truncated[:last_sep + len(sep)].rstrip()
                        break
                else:
                    chinese_title_raw = truncated.rstrip()

        categories = analysis.get('categories', ['其他'])
        source_type = analysis.get('source_type', 'news')
        reading_minutes = analysis.get('reading_minutes', 1)

        pub_str = _format_local_time(item.get('published'))

        icon = item.get('source_icon', '📰')
        source_name = _safe_escape(item.get("source_name", ""))

        orig_title = item.get('title', '')
        if not analysis.get('detailed_content') and orig_title and len(orig_title) > 60:
            analysis_copy = dict(analysis)
            analysis_copy['detailed_content'] = orig_title
            analysis = analysis_copy

        # 源 tier 徽章
        tb_m = _source_badge(item)
        modal_entry = {
            "title": orig_title,
            "chinese_title": chinese_title_raw,
            "summary": raw_summary,
            "why_it_matters": analysis.get('why_it_matters', ''),
            "key_details": analysis.get('key_details', []),
            "detailed_content": analysis.get('detailed_content', ''),
            "background": analysis.get('background', ''),
            "deep_analysis": analysis.get('deep_analysis', ''),
            "importance": analysis.get('importance', 1),
            "categories": categories,
            "source_type": source_type,
            "source_name": source_name,
            "source_icon": icon,
            "source_badge_icon": tb_m[0] if tb_m else '',
            "source_badge_label": tb_m[1] if tb_m else '',
            "link": item.get('link', '#'),
            "pub_date": pub_str,
            "image": item.get('image', ''),
            "extra_images": item.get('extra_images', []),
            "reading_minutes": reading_minutes,
            "audience": analysis.get('audience', ['general']) or ['general'],
            "item_id": item.get('_event_id', '') or item.get('link', '') or f'i{idx}',
        }
        if analysis.get('causal_matches'):
            modal_entry['causal_matches'] = analysis['causal_matches']
            modal_entry['impact_summary'] = analysis.get('impact_summary', '')
        if item.get('_event_status_display'):
            modal_entry['event_status'] = item['_event_status']
            modal_entry['event_status_label'] = item['_event_status_display'].get('label', '')
            modal_entry['event_status_icon'] = item['_event_status_display'].get('icon', '')
        if item.get('_also_reported_by'):
            modal_entry['also_reported_by'] = item['_also_reported_by']
            modal_entry['report_count'] = item.get('_report_count', 0)
        modal_data.append(modal_entry)

    modal_json = json.dumps(modal_data, ensure_ascii=False)
    modal_js_content = f"const __data = {modal_json};"

    # 筛选按钮（分类）
    filter_html = '<button class="f-btn active" data-filter="all">全部</button>\n'
    ordered_cats = ['大模型发布', '开源生态', 'AI 政策监管', '芯片与算力', '产品与应用',
                    '安全与对齐', '融资与商业', '学术研究', 'AI 工具', '具身智能',
                    '自动驾驶', 'AI 医疗', 'AI 编程', '行业观点', '其他']
    for cat in ordered_cats:
        if cat in all_categories:
            filter_html += f'<button class="f-btn" data-filter="{_safe_escape(cat)}">{_safe_escape(cat)}</button>\n'

    # 筛选按钮（读者画像）
    audience_filter_html = ''
    if all_audiences:
        audience_filter_html = '<button class="a-btn active" data-audience="all">全部读者</button>\n'
        aud_order = ['researcher', 'developer', 'pm', 'investor', 'general']
        for aud in aud_order:
            if aud in all_audiences:
                label, icon = AUDIENCE_LABELS[aud]
                audience_filter_html += (
                    f'<button class="a-btn" data-audience="{aud}" '
                    f'title="只看对{label}有用的资讯">{icon} {label}</button>\n'
                )

    # 今日三件大事（第一屏）
    top3_html = ''
    if top3_items:
        top3_cards = ''
        for idx, item in top3_items:
            analysis = item.get('analysis', {})
            ct = _safe_escape(analysis.get('chinese_title', '') or item.get('title', '')[:40])
            why = _safe_escape(analysis.get('why_it_matters', '') or analysis.get('summary', '')[:100])
            src = _safe_escape(item.get("source_name", ""))
            icon = item.get('source_icon', '📰')
            cluster = item.get('_cluster_size', 1) or 1
            multi = (f' · <b>📡 {cluster} 源确认</b>' if cluster >= 2 else '')
            imp = analysis.get('importance', 4)
            imp_marker = '🚀' if imp == 5 else '⭐'
            top3_cards += (
                f'<div class="top3-card" data-idx="{idx}">'
                f'<span class="top3-badge">{imp_marker}</span>'
                f'<div class="top3-body">'
                f'<div class="top3-title">{ct}</div>'
                f'<div class="top3-why">{why}</div>'
                f'<div class="top3-meta">{icon} {src}{multi}</div>'
                f'</div>'
                f'</div>'
            )
        top3_html = (
            '<section class="top3-section" aria-label="今日三件大事">'
            '<h2 class="top3-title-h">🗞️ 今日三件大事</h2>'
            f'<div class="top3-grid">{top3_cards}</div>'
            '</section>'
        )

    # 今日速览 — v2 优先渲染 3 个判断卡片, 没有则回退到旧版 editorial
    briefing_html = ""
    judgments = (digest or {}).get('judgments') or []
    if judgments:
        # v2: 3 个判断卡 (产品定位升级: 从"罗列"到"判断")
        headline = _safe_escape((digest.get('headline') or '').strip())
        outro = _safe_escape((digest.get('outro') or '').strip())

        # 注意: 用 j_cards_html 而非 cards_html, 避免与外层"文章卡片列表"的同名变量冲突
        j_cards_html = ''
        for j in judgments[:3]:
            j_emoji = _safe_escape(str(j.get('emoji', '🔹')))
            j_title = _safe_escape(str(j.get('title', '')).strip())
            j_body = _safe_escape(str(j.get('body', '')).strip())
            # 允许 body 内嵌 ** 加粗
            j_body = re.sub(r'\*\*([^*\n]+?)\*\*',
                            r'<strong class="jc-bold">\1</strong>', j_body)
            j_cards_html += (
                '<article class="judgment-card">'
                f'<div class="jc-emoji">{j_emoji}</div>'
                f'<h3 class="jc-title">{j_title}</h3>'
                f'<p class="jc-body">{j_body}</p>'
                '</article>'
            )

        headline_html = f'<p class="br-headline">{headline}</p>' if headline else ''
        outro_html = f'<p class="br-outro">{outro}</p>' if outro else ''
        n_judgments = len(judgments[:3])
        briefing_html = f'''
    <section class="briefing">
        <h2 class="br-title">📌 今日 {n_judgments} 个判断</h2>
        {headline_html}
        <div class="judgments-grid jcg-{n_judgments}">{j_cards_html}</div>
        {outro_html}
    </section>'''
    elif digest and digest.get('editorial'):
        # 兼容兜底: judgments 缺失时仍渲染旧版 editorial 文本
        editorial_html = _render_editorial(digest.get('editorial', ''))
        if editorial_html:
            briefing_html = f'''
    <section class="briefing">
        <h2 class="br-title">今日速览</h2>
        {editorial_html}
    </section>'''

    # LLM 覆盖率 banner：覆盖率 < 50% 时在页首提示读者"部分内容为规则兜底"
    llm_banner_html = ""
    if meta:
        coverage = meta.get('llm_coverage')
        if coverage is not None and coverage < 0.5 and total > 0:
            pct = int(round(coverage * 100))
            llm_cnt = meta.get('llm_count', 0)
            llm_banner_html = (
                '<div class="llm-banner" role="status">'
                '<span class="llm-banner-icon">⚠️</span>'
                f'<span class="llm-banner-text">今日 LLM 深度分析覆盖率 <b>{pct}%</b>'
                f'（{llm_cnt} / {total} 条），其余为规则兜底内容，质量与标题生成可能简化。</span>'
                '</div>'
            )

    # ── 大V 动态区块：X-*/YouTube 等社交媒体大V，importance 通常<4 达不到 featured
    # 但仍值得独立展示（观点/访谈/短视频解读）。排除已进 featured 的，避免重复 ──
    # VIP 名单 = config.json 里 "vip": true 的源 + 所有 X-* 推特源 + source_type==video
    _vip_names = set()
    for _lang in ('english', 'chinese'):
        for _s in (config or {}).get('sources', {}).get(_lang, []) or []:
            if _s.get('vip'):
                _vip_names.add(_s.get('name', ''))
    def _is_vip(item):
        name = item.get('source_name', '') or ''
        if name.startswith('X-') or name in _vip_names:
            return True
        return item.get('analysis', {}).get('source_type') == 'video'

    featured_idx_set = {idx for idx, _ in featured_items}
    vip_candidates = []
    for idx, item in enumerate(all_items):
        if idx in featured_idx_set or not _is_vip(item):
            continue
        a = item.get('analysis', {}) or {}
        if a.get('ai_relevant') is False:
            continue
        imp = a.get('importance', 0)
        if imp < 2:
            continue
        vip_candidates.append((idx, item, imp))
    vip_candidates.sort(key=lambda x: -x[2])
    vip_candidates = vip_candidates[:8]

    vip_html = ''
    if vip_candidates:
        rows = []
        for idx, item, _imp in vip_candidates:
            a = item.get('analysis', {}) or {}
            ct = _safe_escape(a.get('chinese_title', '') or item.get('title', '')[:70])
            why = _safe_escape(a.get('why_it_matters', '') or (a.get('summary', '') or '')[:110])
            src = _safe_escape(item.get("source_name", ""))
            icon = item.get('source_icon', '🎙️')
            why_html = f'<div class="vip-why">{why}</div>' if why else ''
            _viid = item.get('_event_id') or item.get('link') or f'vip{idx}'
            rows.append(
                f'<div class="vip-item" data-idx="{idx}" data-iid="{_safe_escape(_viid)}">'
                f'<div class="vip-meta">{icon} {src}</div>'
                f'<div class="vip-title-txt">{ct}</div>'
                f'{why_html}'
                f'</div>'
            )
        vip_html = (
            '<section class="vip-section" aria-label="大V 动态">'
            '<h2 class="vip-heading">🎙️ 大V 动态</h2>'
            '<div class="vip-list">' + ''.join(rows) + '</div>'
            '</section>'
        )

    # ── SEO / 分享：description 优先取 digest.editorial，其次拼 top3 标题 ──
    meta_description = ''
    if digest and digest.get('editorial'):
        meta_description = digest['editorial'].strip().replace('\n', ' ')
    elif top3_items:
        titles = [
            (i.get('analysis', {}).get('chinese_title') or i.get('title', ''))[:40]
            for _, i in top3_items
        ]
        meta_description = f"今日共 {total} 条 AI 资讯 · 重点：" + '；'.join(t for t in titles if t)
    else:
        meta_description = f"每日 AI 行业早报 · 共 {total} 条资讯，覆盖 {sources_count} 个信息源"
    # 裁剪到 160 字符（search engine / og 常规上限）+ 双重 escape（meta content 属性）
    meta_description = _safe_escape(meta_description[:157] + ('…' if len(meta_description) > 160 else ''))

    # ══════════════════════════════════════════════════════════
    # 从模板文件组装 HTML
    # ══════════════════════════════════════════════════════════
    css_content = _load_template('style.css')
    js_content = _load_template('script.js')
    page_template = Template(_load_template('page.html'))

    html = page_template.safe_substitute(
        vip_html=vip_html,
        date_str=date_str,
        weekday=weekday,
        time_str=time_str,
        briefing_title=briefing_title,
        briefing_emoji=briefing_emoji,
        total=total,
        sources_count=sources_count,
        filter_html=filter_html,
        audience_filter_html=audience_filter_html,
        top3_html=top3_html,
        llm_banner_html=llm_banner_html,
        briefing_html=briefing_html,
        featured_html=featured_html,
        cards_html=cards_html,
        css_content=css_content,
        js_content=js_content,
        meta_description=meta_description,
    )

    return html, modal_js_content
