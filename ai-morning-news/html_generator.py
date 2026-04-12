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


def generate_html(all_items, config, digest=None):
    """生成六区卡片模型的 HTML 页面 — 基于 Tufte 信噪比原则重新设计"""
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

    # 收集分类
    all_categories = set()
    for item in all_items:
        for cat in item.get('analysis', {}).get('categories', []):
            all_categories.add(cat)

    # ── 构建卡片 ──
    cards_html = ""
    modal_data = []

    for idx, item in enumerate(all_items):
        analysis = item.get('analysis', {})
        # 中文标题：优先 LLM 生成的 chinese_title，否则回退到 summary 截取
        chinese_title_raw = analysis.get('chinese_title', '') or ''
        raw_summary = analysis.get('summary', '') or ''
        if not raw_summary or raw_summary in ('无法提取摘要', '无法获取分析'):
            raw_summary = item.get('title', '')[:100]
        if not chinese_title_raw:
            # fallback：LLM 没给中文标题时，取 summary/原标题前 40 字，
            # 避免和 summary[:30] 那种半词截断撞车
            fallback_src = raw_summary or item.get('title', '') or ''
            chinese_title_raw = fallback_src[:40]
        chinese_title = escape(chinese_title_raw)
        summary = escape(raw_summary)
        why_it_matters = escape(analysis.get('why_it_matters', ''))
        key_details = analysis.get('key_details', [])
        importance = analysis.get('importance', 1)
        categories = analysis.get('categories', ['其他'])
        source_type = analysis.get('source_type', 'news')
        reading_minutes = analysis.get('reading_minutes', 1)

        pub_str = ""
        pub_val = item.get('published')
        if pub_val:
            try:
                if isinstance(pub_val, str):
                    pub_val = datetime.fromisoformat(pub_val)
                pub_str = pub_val.strftime("%m-%d %H:%M")
            except (AttributeError, ValueError, TypeError):
                pass

        link = escape(item.get('link', '#'))
        icon = item.get('source_icon', '📰')
        source_name = escape(item.get('source_name', ''))
        cat_data = '|'.join(categories)
        image_url = escape(item.get('image', ''))

        # 是否是高重要性卡片（4-5）
        is_featured = importance >= 4

        # Z1: bare minimum — category + reading time
        cat_text = ' · '.join(escape(c) for c in categories[:2])
        z1_html = f'''<div class="z1">
            <span class="z1-left">{cat_text}</span>
            <span class="z1-meta">{reading_minutes} min</span>
        </div>'''

        # 图片区域
        img_html = ""
        if image_url:
            img_html = f'<div class="card-img" style="background-image:url(\'{image_url}\')"></div>'

        # 标题：中文标题作为卡片主标题
        title_html = f'<div class="card-title">{chinese_title}</div>'

        # Z2: 要点摘要 — key_details 优先，没有就用 summary
        z2_inner = ""
        if key_details:
            for d in key_details[:3]:
                d_text = escape(d) if isinstance(d, str) else escape(str(d))
                z2_inner += f'<div class="z2-point">• {d_text}</div>'
        elif summary:
            z2_inner = f'<div class="z2-summary">{summary}</div>'
        z2_html = f'<div class="z2">{z2_inner}</div>' if z2_inner else ""

        # Z3: why_it_matters（不再重复 key_details）
        z3_html = ""
        if why_it_matters:
            z3_html = f'<div class="z3"><div class="z3-why">{why_it_matters}</div></div>'

        # Z5: source + time
        time_part = f' · {pub_str}' if pub_str else ''
        z5_html = f'<div class="z5">{icon} {source_name}{time_part}</div>'

        # 组装卡片
        featured_cls = " card--featured" if is_featured else ""
        cards_html += f'''
        <div class="card{featured_cls}" data-cat="{escape(cat_data)}" data-idx="{idx}"
             style="animation-delay:{min(idx * 25, 500)}ms">
            {img_html}
            <div class="card-body">
                {z1_html}
                {title_html}
                {z2_html}
                {z3_html}
                {z5_html}
            </div>
        </div>'''

        # 弹窗数据 — 包含卡片上没有的深度字段
        modal_entry = {
            "title": item.get('title', ''),
            "chinese_title": chinese_title_raw,
            "summary": raw_summary,
            "why_it_matters": analysis.get('why_it_matters', ''),
            "key_details": key_details,
            "detailed_content": analysis.get('detailed_content', ''),
            "background": analysis.get('background', ''),
            "deep_analysis": analysis.get('deep_analysis', ''),
            "importance": importance,
            "categories": categories,
            "source_type": source_type,
            "source_name": source_name,
            "source_icon": icon,
            "link": item.get('link', '#'),
            "pub_date": pub_str,
            "image": item.get('image', ''),
            "extra_images": item.get('extra_images', []),
            "reading_minutes": reading_minutes,
        }
        # 因果分析数据（如有）
        if analysis.get('causal_matches'):
            modal_entry['causal_matches'] = analysis['causal_matches']
            modal_entry['impact_summary'] = analysis.get('impact_summary', '')
        modal_data.append(modal_entry)

    modal_json = json.dumps(modal_data, ensure_ascii=False)
    modal_js_content = f"const __data = {modal_json};"

    # 筛选按钮 — 只显示实际存在的分类
    filter_html = '<button class="f-btn active" data-filter="all">全部</button>\n'
    ordered_cats = ['大模型发布', '开源生态', 'AI 政策监管', '芯片与算力', '产品与应用',
                    '安全与对齐', '融资与商业', '学术研究', 'AI 工具', '具身智能',
                    '自动驾驶', 'AI 医疗', 'AI 编程', '行业观点', '其他']
    for cat in ordered_cats:
        if cat in all_categories:
            filter_html += f'<button class="f-btn" data-filter="{escape(cat)}">{escape(cat)}</button>\n'

    # 今日速览 — 仅编辑导语，不列 top stories
    briefing_html = ""
    if digest and digest.get('editorial'):
        editorial = escape(digest.get('editorial', ''))
        briefing_html = f'''
    <section class="briefing">
        <h2 class="br-title">今日速览</h2>
        <p class="br-editorial">{editorial}</p>
    </section>'''

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
        briefing_html=briefing_html,
        cards_html=cards_html,
        css_content=css_content,
        js_content=js_content,
    )

    return html, modal_js_content
