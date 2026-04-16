"""
html_generator.py - HTML page generation for AI Morning News

Generates the complete HTML page using external template files:
  templates/page.html  — HTML skeleton with $variable placeholders
  templates/style.css  — all CSS styles
  templates/script.js  — all JavaScript (modal, filter, search, keyboard)

Uses string.Template for substitution (zero external dependencies).
"""

import json
from html import escape
from datetime import datetime
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

    for idx, item in enumerate(all_items):
        analysis = item.get('analysis', {})
        importance = analysis.get('importance', 1)

        if importance >= 4:
            featured_items.append((idx, item))
        else:
            regular_items.append((idx, item))

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

            chinese_title = escape(chinese_title_raw)
            why_it_matters = escape(analysis.get('why_it_matters', ''))
            categories = analysis.get('categories', ['其他'])

            pub_str = ""
            pub_val = item.get('published')
            if pub_val:
                try:
                    if isinstance(pub_val, str):
                        clean = pub_val.replace('Z', '+00:00')
                        pub_val = datetime.fromisoformat(clean)
                    pub_str = pub_val.strftime("%m-%d %H:%M")
                except (AttributeError, ValueError, TypeError):
                    if isinstance(pub_val, str) and len(pub_val) >= 10:
                        try:
                            pub_str = pub_val[5:10]
                        except Exception:
                            pass

            icon = item.get('source_icon', '📰')
            source_name = escape(item.get('source_name', ''))
            image_url = escape(item.get('image', ''))
            cat_data = '|'.join(categories)

            # 左侧边条颜色
            border_color = '#e05252' if importance == 5 else '#e8913a'

            # 图片区域
            img_html = ""
            if image_url:
                img_html = f'<div class="featured-img" style="background-image:url(\'{image_url}\')"></div>'

            # 标题
            title_html = f'<div class="featured-title-text">{chinese_title}</div>'

            # why_it_matters
            why_html = f'<div class="featured-why">{why_it_matters}</div>' if why_it_matters else ""

            # 来源徽章 + 时间
            tb = _source_badge(item)
            tier_badge_html = (
                f'<span class="tier-badge tier-{tb[1]}" title="{tb[1]}源">{tb[0]} {tb[1]}</span>'
                if tb else ''
            )
            time_part = f' · {pub_str}' if pub_str else ''
            src_html = f'<div class="featured-source">{tier_badge_html} {icon} {source_name}{time_part}</div>'

            # 多源报道 pill（featured 卡片右上角）
            cluster_size = item.get('_cluster_size', 1) or 1
            report_count = item.get('_report_count', cluster_size) or cluster_size
            multi_pill = ''
            if cluster_size >= 2 or report_count >= 2:
                n = max(cluster_size, report_count)
                multi_pill = f'<span class="src-pill src-pill-featured" title="共 {n} 个来源报道同一事件">📡 {n} 源</span>'

            # importance=5 的 "行业级" 徽章（置于卡片顶部，左侧）
            hero_badge = ''
            if importance == 5:
                hero_badge = '<span class="hero-badge" title="行业格局级">🚀 行业级</span>'

            # data-audience 属性，供前端 tab 过滤
            aud_list = analysis.get('audience', []) or ['general']
            aud_data = '|'.join(a for a in aud_list if a in AUDIENCE_LABELS) or 'general'

            featured_html += f'''    <div class="featured-card" data-cat="{escape(cat_data)}" data-aud="{escape(aud_data)}" data-idx="{idx}" style="border-left-color: {border_color}">
        {img_html}
        <div class="featured-body">
            {hero_badge}
            {multi_pill}
            {title_html}
            {why_html}
            {src_html}
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

        chinese_title = escape(chinese_title_raw)
        why_it_matters = escape(analysis.get('why_it_matters', ''))
        categories = analysis.get('categories', ['其他'])
        source_type = analysis.get('source_type', 'news')
        reading_minutes = analysis.get('reading_minutes', 1)

        pub_str = ""
        pub_val = item.get('published')
        if pub_val:
            try:
                if isinstance(pub_val, str):
                    clean = pub_val.replace('Z', '+00:00')
                    pub_val = datetime.fromisoformat(clean)
                pub_str = pub_val.strftime("%m-%d %H:%M")
            except (AttributeError, ValueError, TypeError):
                if isinstance(pub_val, str) and len(pub_val) >= 10:
                    try:
                        pub_str = pub_val[5:10]
                    except Exception:
                        pass

        icon = item.get('source_icon', '📰')
        source_name = escape(item.get('source_name', ''))
        cat_data = '|'.join(categories)
        image_url = escape(item.get('image', ''))

        # Z1: 分类 + 阅读时间
        cat_text = ' · '.join(escape(c) for c in categories[:2])
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

        # 精简的 why_it_matters（只显示，不显示 key_details/summary）
        z3_html = ""
        if why_it_matters:
            z3_html = f'<div class="z3"><div class="z3-why">{why_it_matters}</div></div>'

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

        # data-audience 属性
        aud_list = analysis.get('audience', []) or ['general']
        aud_data = '|'.join(a for a in aud_list if a in AUDIENCE_LABELS) or 'general'

        cards_html += f'''
        <div class="card" data-cat="{escape(cat_data)}" data-aud="{escape(aud_data)}" data-idx="{idx}"
             style="animation-delay:{min(idx * 25, 500)}ms">
            {img_html}
            <div class="card-body">
                {multi_pill}
                {z1_html}
                {title_html}
                {z3_html}
                {z5_html}
            </div>
        </div>'''

    # ── 为所有卡片构建 modal_data ──
    for idx, item in featured_items + regular_items:
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

        pub_str = ""
        pub_val = item.get('published')
        if pub_val:
            try:
                if isinstance(pub_val, str):
                    clean = pub_val.replace('Z', '+00:00')
                    pub_val = datetime.fromisoformat(clean)
                pub_str = pub_val.strftime("%m-%d %H:%M")
            except (AttributeError, ValueError, TypeError):
                if isinstance(pub_val, str) and len(pub_val) >= 10:
                    try:
                        pub_str = pub_val[5:10]
                    except Exception:
                        pass

        icon = item.get('source_icon', '📰')
        source_name = escape(item.get('source_name', ''))

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
            filter_html += f'<button class="f-btn" data-filter="{escape(cat)}">{escape(cat)}</button>\n'

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
            ct = escape(analysis.get('chinese_title', '') or item.get('title', '')[:40])
            why = escape(analysis.get('why_it_matters', '') or analysis.get('summary', '')[:100])
            src = escape(item.get('source_name', ''))
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

    # 今日速览
    briefing_html = ""
    if digest and digest.get('editorial'):
        editorial = escape(digest.get('editorial', ''))
        briefing_html = f'''
    <section class="briefing">
        <h2 class="br-title">今日速览</h2>
        <p class="br-editorial">{editorial}</p>
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

    # ══════════════════════════════════════════════════════════
    # 从模板文件组装 HTML
    # ══════════════════════════════════════════════════════════
    css_content = _load_template('style.css')
    js_content = _load_template('script.js')
    page_template = Template(_load_template('page.html'))

    html = page_template.safe_substitute(
        date_str=date_str,
        weekday=weekday,
        time_str=time_str,
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
    )

    return html, modal_js_content
