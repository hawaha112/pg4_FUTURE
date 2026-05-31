"""
entity_timeline_generator.py — 实体时间线生成器 (P1)

为每个核心实体 (OpenAI / Anthropic / Google DeepMind / NVIDIA / ...) 生成
"过去 30 天动态时间线" 独立 HTML 页, 让用户能查"某公司 30 天大事记".

核心价值: 把 18 条孤立新闻 → 串成实体演进叙事. 这是从"新闻聚合"到
"行业分析师"产品定位升级的关键组件 (与 P0 "今日 3 个判断"互补).

策略 (纯渲染层, 不改 DB schema):
- 加载最近 30 天的 canonical_events (events.db)
- 用 EntityCoverageMatrix 即时从 title + summary 提取实体
- 按实体聚合 → 生成 timeline HTML
- 输出到 output/entities/{entity_id}-30d.html
- 主早报顶部加 "实体追踪" 入口链接到这些页面
"""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from entity_coverage import EntityCoverageMatrix
from logger import get_logger

log = get_logger('entity_timeline')


def _format_event_time(iso_str: str) -> Tuple[str, str]:
    """ISO 时间 → (绝对日期 MM-DD, 相对 X 天前) for UI."""
    if not iso_str:
        return ('?', '')
    try:
        dt = datetime.fromisoformat(str(iso_str).replace('Z', '+00:00'))
        dt = dt.astimezone(timezone(timedelta(hours=8)))
        days_ago = (datetime.now(timezone(timedelta(hours=8))).date() - dt.date()).days
        rel = '今天' if days_ago == 0 else f'{days_ago} 天前' if days_ago < 30 else dt.strftime('%Y-%m')
        return (dt.strftime('%m-%d %H:%M'), rel)
    except Exception:
        return (str(iso_str)[:16], '')


def _load_recent_events(db_path: Path, days: int = 30) -> List[Dict]:
    """从 events.db 加载最近 N 天的 canonical_events + 关联的 canonical article."""
    if not db_path.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        sql = """
        SELECT
            e.event_id, e.title, e.summary, e.canonical_url,
            e.published_at, e.first_seen_at, e.importance, e.status,
            e.event_type, e.cluster_size, e.analysis,
            a.title AS article_title, a.summary AS article_summary,
            a.source_name AS article_source, a.url AS article_url
        FROM canonical_events e
        LEFT JOIN articles a ON e.canonical_article_id = a.id
        WHERE COALESCE(e.published_at, e.first_seen_at) >= ?
        ORDER BY COALESCE(e.published_at, e.first_seen_at) DESC
        """
        for r in conn.execute(sql, (cutoff,)).fetchall():
            d = dict(r)
            # 把 analysis JSON 解开
            try:
                d['analysis'] = json.loads(d.get('analysis') or '{}')
            except (json.JSONDecodeError, TypeError):
                d['analysis'] = {}
            rows.append(d)
    return rows


def _tag_events_with_entities(events: List[Dict]) -> Dict[str, List[Dict]]:
    """给每个 event 即时提取实体, 然后按实体聚合.

    Returns: {entity_id: [events sorted by time desc]}
    """
    matrix = EntityCoverageMatrix()
    # tag_items 接受 dict 列表, 看 title + summary; 我们把 event 当 item
    items = []
    for ev in events:
        # 拼"看得最全"的文本: chinese_title + article title + summary
        chinese_title = (ev.get('analysis', {}).get('chinese_title') or '').strip()
        ev_title = (ev.get('title') or '').strip()
        art_title = (ev.get('article_title') or '').strip()
        summary = (ev.get('summary') or ev.get('article_summary') or '').strip()
        item = {
            'title': f'{chinese_title} {ev_title} {art_title}'.strip(),
            'summary': summary[:300],
            '_orig_event': ev,
        }
        items.append(item)

    tagged = matrix.tag_items(items)

    by_entity = defaultdict(list)
    for t in tagged:
        for eid in t.get('_entities', []) or []:
            by_entity[eid].append(t['_orig_event'])

    return by_entity


def _get_entity_meta(matrix: EntityCoverageMatrix, entity_id: str) -> Dict:
    """从 registry 拿实体的展示元数据 (name, icon, aliases)."""
    for e in matrix.entities:
        if e.get('id') == entity_id:
            return {
                'id': entity_id,
                'name': e.get('name', entity_id),
                'icon': e.get('icon', '🏢'),
                'tier': e.get('tier', 2),
                'aliases': e.get('aliases', []),
            }
    return {'id': entity_id, 'name': entity_id, 'icon': '🏢', 'tier': 2, 'aliases': []}


def _render_timeline_html(entity_meta: Dict, events: List[Dict],
                          briefing_url: str = '') -> str:
    """渲染单个实体的时间线 HTML 页."""
    name = entity_meta['name']
    icon = entity_meta['icon']
    n_events = len(events)

    # 按重要性 + 时间分类: 头条事件 (importance >= 4) vs 一般动态
    important = [e for e in events if (e.get('importance') or 0) >= 4]
    others = [e for e in events if (e.get('importance') or 0) < 4]

    # 渲染单个事件行
    def render_event_row(e: Dict, idx: int) -> str:
        chinese_title = (e.get('analysis', {}).get('chinese_title') or '').strip()
        title_display = chinese_title or (e.get('title') or '').strip() or '(无标题)'
        time_str, rel_str = _format_event_time(
            e.get('published_at') or e.get('first_seen_at')
        )
        imp = e.get('importance') or 0
        imp_stars = '⭐' * min(imp, 5) if imp >= 4 else ''
        source = e.get('article_source') or ''
        url = e.get('canonical_url') or e.get('article_url') or '#'
        summary = (e.get('summary') or '').strip()[:160]
        # event 的 deep_analysis (如果有)
        analysis = e.get('analysis', {})
        deep = (analysis.get('deep_analysis') or '').strip()[:240]
        cluster_pill = ''
        if (e.get('cluster_size') or 1) >= 2:
            cluster_pill = f'<span class="tl-multi">📡 {e.get("cluster_size")} 源</span>'

        return f'''
        <article class="tl-event">
            <div class="tl-event-time">
                <span class="tl-date">{time_str}</span>
                <span class="tl-rel">{rel_str}</span>
            </div>
            <div class="tl-event-body">
                <h3 class="tl-event-title">
                    {imp_stars}
                    <a href="{url}" target="_blank" rel="noopener">{title_display}</a>
                </h3>
                <div class="tl-event-meta">{source} {cluster_pill}</div>
                <p class="tl-event-summary">{summary}</p>
                {f'<p class="tl-event-deep">{deep}</p>' if deep else ''}
            </div>
        </article>'''

    important_html = ''.join(render_event_row(e, i) for i, e in enumerate(important))
    others_html = ''.join(render_event_row(e, i) for i, e in enumerate(others))

    # 智能返回：实体页同时被主早报(根 index.html)和归档页(archive/*.html)链接，
    # 单一静态链接无法知道来路。优先 history.back() 回到真实来路(主页或归档页都对)，
    # 无 referrer / 直接打开 时回退到根 index.html。JS 关掉也能用(href 兜底)。
    back_link = (
        '<a href="../index.html" class="tl-back" '
        'onclick="if(document.referrer&&history.length>1){history.back();return false;}">'
        '← 返回早报</a>'
    )

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} · 30 天动态 · AI 早报</title>
<style>
:root {{
    --bg: #0c1220;
    --bg-card: rgba(255,255,255,0.04);
    --border: rgba(255,255,255,0.10);
    --accent: #66c0ff;
    --accent-hi: #5eead4;
    --text-100: #ecf0f5;
    --text-70: #b6bdcb;
    --text-50: #8a93a6;
    --serif: 'Noto Serif SC', 'Songti SC', serif;
}}
* {{ box-sizing: border-box; }}
body {{
    margin: 0; padding: 32px 16px;
    background: var(--bg); color: var(--text-100);
    font-family: -apple-system, 'PingFang SC', 'Noto Sans SC', sans-serif;
    line-height: 1.6;
}}
.wrap {{ max-width: 880px; margin: 0 auto; }}
.tl-back {{
    color: var(--text-50); font-size: 12px; text-decoration: none;
    display: inline-block; margin-bottom: 18px;
}}
.tl-back:hover {{ color: var(--accent-hi); }}
h1.tl-title {{
    font-family: var(--serif); font-size: 28px; font-weight: 800;
    margin: 0 0 4px; letter-spacing: 0.3px;
}}
.tl-icon {{ font-size: 30px; margin-right: 8px; }}
.tl-subtitle {{
    color: var(--text-50); font-size: 13px;
    margin: 0 0 28px;
}}
h2.tl-section {{
    font-size: 13px; font-weight: 700; letter-spacing: 1px;
    color: var(--text-50); text-transform: uppercase;
    margin: 28px 0 12px; padding-left: 10px;
    border-left: 3px solid var(--accent);
}}
.tl-event {{
    display: grid; grid-template-columns: 110px 1fr;
    gap: 16px; padding: 14px 0;
    border-bottom: 1px solid var(--border);
}}
.tl-event:last-child {{ border-bottom: none; }}
.tl-event-time {{
    display: flex; flex-direction: column; align-items: flex-start;
    font-family: monospace; line-height: 1.4; padding-top: 2px;
}}
.tl-date {{ font-size: 12px; color: var(--accent); font-weight: 600; }}
.tl-rel {{ font-size: 10px; color: var(--text-50); margin-top: 2px; }}
.tl-event-body {{ min-width: 0; }}
.tl-event-title {{
    font-family: var(--serif); font-size: 16px; font-weight: 600;
    line-height: 1.5; margin: 0 0 6px;
}}
.tl-event-title a {{ color: var(--text-100); text-decoration: none; }}
.tl-event-title a:hover {{ color: var(--accent-hi); }}
.tl-event-meta {{
    font-size: 11px; color: var(--text-50); margin-bottom: 6px;
}}
.tl-multi {{
    display: inline-block; margin-left: 8px;
    padding: 1px 6px; border-radius: 4px;
    background: rgba(102,192,255,0.08);
    color: var(--accent); font-weight: 500;
}}
.tl-event-summary {{
    font-size: 13px; color: var(--text-70); margin: 0 0 6px;
    line-height: 1.6;
}}
.tl-event-deep {{
    font-size: 12.5px; color: var(--text-70); margin: 6px 0 0;
    padding: 8px 12px; border-left: 2px solid var(--accent-hi);
    background: rgba(94,234,212,0.04); border-radius: 0 6px 6px 0;
    font-style: italic;
}}
.tl-empty {{
    color: var(--text-50); font-size: 13px; padding: 14px 0;
}}
@media (max-width: 640px) {{
    body {{ padding: 20px 12px; }}
    .tl-event {{ grid-template-columns: 1fr; gap: 6px; }}
    .tl-event-time {{ flex-direction: row; gap: 8px; align-items: baseline; }}
    h1.tl-title {{ font-size: 22px; }}
}}
</style>
</head>
<body>
<div class="wrap">
    {back_link}
    <h1 class="tl-title"><span class="tl-icon">{icon}</span>{name}</h1>
    <p class="tl-subtitle">过去 30 天动态时间线 · 共 <b>{n_events}</b> 个事件</p>

    <h2 class="tl-section">⭐ 重要事件 ({len(important)})</h2>
    {important_html if important else '<div class="tl-empty">本月暂无重要事件 (importance ≥ 4)</div>'}

    <h2 class="tl-section">📰 一般动态 ({len(others)})</h2>
    {others_html if others else '<div class="tl-empty">本月暂无一般动态</div>'}
</div>
</body>
</html>
'''


def generate_entity_timelines(
    db_path: Path,
    output_dir: Path,
    days: int = 30,
    min_events: int = 3,
    top_n_entities: int = 12,
) -> List[Dict]:
    """主入口: 生成所有有足够事件量的实体时间线页面.

    Args:
        db_path: events.db 路径
        output_dir: 输出目录 (会创建 entities/ 子目录)
        days: 取最近 N 天事件
        min_events: 实体至少有 N 个事件才生成页 (避免冷门实体)
        top_n_entities: 最多生成 N 个实体页

    Returns:
        [{'id', 'name', 'icon', 'count', 'html_path'}] 用于主早报渲染入口链接
    """
    log.info("📋 生成实体时间线 (最近 %d 天)...", days)
    events = _load_recent_events(db_path, days)
    if not events:
        log.warning("⚠️ 没有最近 %d 天的事件, 跳过实体时间线", days)
        return []

    log.info("📋 加载到 %d 个事件, 开始按实体聚合...", len(events))
    by_entity = _tag_events_with_entities(events)

    matrix = EntityCoverageMatrix()
    # 按事件数排序, 取 top N 且 >= min_events
    eligible = [(eid, evs) for eid, evs in by_entity.items() if len(evs) >= min_events]
    eligible.sort(key=lambda x: -len(x[1]))
    selected = eligible[:top_n_entities]

    log.info("📋 选出 %d 个实体 (>= %d events): %s",
             len(selected), min_events,
             ', '.join(f'{eid}={len(evs)}' for eid, evs in selected[:6]) + ('...' if len(selected) > 6 else ''))

    output_dir = Path(output_dir)
    entities_dir = output_dir / 'entities'
    entities_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    for entity_id, ev_list in selected:
        meta = _get_entity_meta(matrix, entity_id)
        html = _render_timeline_html(meta, ev_list)
        out_path = entities_dir / f'{entity_id}-30d.html'
        out_path.write_text(html, encoding='utf-8')
        summary.append({
            'id': entity_id,
            'name': meta['name'],
            'icon': meta['icon'],
            'count': len(ev_list),
            'html_path': f'entities/{entity_id}-30d.html',
        })
        log.info("  ✓ %s %s: %d 个事件 → %s", meta['icon'], meta['name'], len(ev_list), out_path.name)

    return summary


if __name__ == '__main__':
    # 命令行调试
    import sys
    db = Path(sys.argv[1] if len(sys.argv) > 1 else 'events.db')
    out = Path(sys.argv[2] if len(sys.argv) > 2 else 'output')
    result = generate_entity_timelines(db, out)
    print(f'\n生成 {len(result)} 个实体时间线:')
    for r in result:
        print(f'  {r["icon"]} {r["name"]:20s} {r["count"]:3d} 事件  → {r["html_path"]}')
