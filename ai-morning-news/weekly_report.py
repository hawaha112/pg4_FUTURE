#!/usr/bin/env python3
"""
weekly_report.py — 每周日生成 AI 行业周报

输出:
  output/weekly/YYYY-Wnn.html  (本周快照)
  output/weekly/latest.html    (始终指向最新一期)
  output/weekly/index.json     (周报元数据列表,供其他页面消费)

数据流:
  events.db (canonical_events imp>=4 over past 7 days)
       ↓
  articles 表(过去 7 天)统计实体 + 分类分布
       ↓
  LLM 一次调用生成: 3 个关键判断 + 下周值得关注
       ↓
  渲染 inline HTML 模板
       ↓
  保存到 output/weekly/

CLI:
  python3 weekly_report.py            # 跑过去 7 天
  python3 weekly_report.py --days 14  # 跑过去 14 天(测试用)
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import sys
from html import escape
from pathlib import Path
from string import Template
from typing import Optional

from logger import get_logger
from llm_analyzer import LLMAnalyzer, create_analyzer_from_config

log = get_logger('weekly_report')

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))


# ──────────────────────────────────────────────────────────
# 数据查询
# ──────────────────────────────────────────────────────────

def fetch_important_events(db_path: Path, start: datetime.datetime, end: datetime.datetime):
    """取窗口内 importance >= 4 的事件,按 published_at 倒序。"""
    con = sqlite3.connect(str(db_path))
    rows = con.execute("""
        SELECT
            e.event_id,
            e.published_at,
            json_extract(e.analysis, '$.chinese_title') AS title,
            json_extract(e.analysis, '$.summary') AS summary,
            json_extract(e.analysis, '$.deep_analysis') AS deep,
            e.importance,
            (SELECT a.source_name FROM articles a
              WHERE a.canonical_event_id = e.event_id LIMIT 1) AS src,
            (SELECT a.url FROM articles a
              WHERE a.canonical_event_id = e.event_id LIMIT 1) AS url
        FROM canonical_events e
        WHERE e.published_at >= ?
          AND e.published_at < ?
          AND e.importance >= 4
          AND e.analysis IS NOT NULL
        ORDER BY datetime(e.published_at) DESC
    """, (start.isoformat(), end.isoformat())).fetchall()
    con.close()
    return rows


def fetch_entity_distribution(db_path: Path, registry_path: Path,
                              start: datetime.datetime, end: datetime.datetime):
    """统计窗口内每个预定义实体出现次数。"""
    if not registry_path.exists():
        return []
    try:
        registry = json.loads(registry_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return []

    patterns = []
    for e in registry.get('entities', []):
        kws = e.get('keywords') or []
        if not kws:
            continue
        pat = re.compile('|'.join(re.escape(k) for k in kws), re.IGNORECASE)
        patterns.append((e.get('name') or e.get('id'), pat))

    if not patterns:
        return []

    con = sqlite3.connect(str(db_path))
    rows = con.execute("""
        SELECT title,
               json_extract(analysis,'$.chinese_title'),
               json_extract(analysis,'$.summary')
        FROM articles
        WHERE collected_at >= ? AND collected_at < ?
          AND ai_relevant = 1
    """, (start.isoformat(), end.isoformat())).fetchall()
    con.close()

    counts: dict[str, int] = {}
    for title, ct, sm in rows:
        text = ' '.join(filter(None, [title, ct, sm]))
        if not text:
            continue
        for name, pat in patterns:
            if pat.search(text):
                counts[name] = counts.get(name, 0) + 1

    return sorted(counts.items(), key=lambda x: -x[1])


def fetch_category_distribution(db_path: Path,
                                start: datetime.datetime, end: datetime.datetime):
    """统计 categories 分布。"""
    con = sqlite3.connect(str(db_path))
    rows = con.execute("""
        SELECT json_extract(analysis, '$.categories') AS cats
        FROM articles
        WHERE collected_at >= ? AND collected_at < ?
          AND ai_relevant = 1
          AND json_extract(analysis, '$.categories') IS NOT NULL
    """, (start.isoformat(), end.isoformat())).fetchall()
    con.close()

    counts: dict[str, int] = {}
    for (cats_json,) in rows:
        try:
            cats = json.loads(cats_json) if cats_json else []
            for c in cats[:2]:  # 每条文章只取前 2 个分类
                if c:
                    counts[c] = counts.get(c, 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(counts.items(), key=lambda x: -x[1])


# ──────────────────────────────────────────────────────────
# LLM 生成 insight
# ──────────────────────────────────────────────────────────

WEEKLY_INSIGHT_PROMPT = """你是 AI 行业资深分析师。基于本周 {n_events} 条 importance ≥ 4 的核心事件,撰写本周分析报告。

本周事件(按时间倒序):
{events_text}

请用 JSON 格式返回(直接输出,第一个字符必须是 {{,最后一个必须是 }}):
{{
  "judgement_1": {{"title": "判断标题,20 字内", "body": "120 字详述,引用具体事件"}},
  "judgement_2": {{"title": "...", "body": "..."}},
  "judgement_3": {{"title": "...", "body": "..."}},
  "next_week_focus": "150 字以内,基于本周已发生事件给出'下周值得关注'的提示"
}}

要求:
- 3 个判断要覆盖不同维度(技术格局变化 / 商业动作 / 政策法规 / 资本流向 / 人才流动 等)
- 标题简洁有力,像头条;body 给出具体证据,**引用本周事件标题**
- 不要泛泛而谈;不要重复事件本身,要做"判断"和"解读"
- 下周展望基于本周埋下的种子(融资刚发的、合作刚签的、政策刚出的下一步可能怎么走),不要凭空预测
- 全部使用简体中文"""


def build_events_text(events) -> str:
    """把事件列表拼成给 LLM 的 prompt 部分。"""
    parts = []
    for i, ev in enumerate(events[:30], 1):
        title = (ev[2] or '').strip()
        summary = (ev[3] or '').strip()
        deep = (ev[4] or '').strip()[:200]
        src = ev[6] or ''
        parts.append(
            f"{i}. {title}  [{src}]\n"
            f"   摘要: {summary}\n"
            f"   分析: {deep}"
        )
    return '\n\n'.join(parts)


def generate_insights(events, llm: LLMAnalyzer):
    """让 LLM 生成 3 个关键判断 + 下周展望。"""
    if not events:
        return None

    events_text = build_events_text(events)
    prompt = WEEKLY_INSIGHT_PROMPT.format(
        n_events=len(events), events_text=events_text
    )

    try:
        response = llm._call_api([{"role": "user", "content": prompt}])
    except Exception as e:
        log.error("LLM 调用失败: %s", e)
        return None

    insights = llm._extract_json(response)
    if not insights or 'judgement_1' not in insights:
        log.error("LLM 周报 insight 解析失败,响应前 200 字: %s", response[:200])
        return None
    return insights


# ──────────────────────────────────────────────────────────
# HTML 渲染
# ──────────────────────────────────────────────────────────

WEEKLY_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="$meta_description">
<title>$title</title>
<style>
  :root {
    --bg-page: #0c1220;
    --bg-card: rgba(255,255,255,0.04);
    --bg-card-hi: rgba(255,255,255,0.08);
    --border: rgba(255,255,255,0.10);
    --accent: #66c0ff;
    --accent-hi: #5eead4;
    --accent-glow: rgba(102,192,255,0.25);
    --red: #e05252;
    --amber: #e8913a;
    --text-100: #ecf0f5;
    --text-70: #b6bdcb;
    --text-50: #8a93a6;
    --text-35: #5a6275;
    --serif: 'Noto Serif SC', 'Songti SC', serif;
    --mono: 'JetBrains Mono', Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px;
    background: var(--bg-page); color: var(--text-100);
    font-family: -apple-system, 'PingFang SC', 'Noto Sans SC', sans-serif;
    line-height: 1.6;
  }
  .wrap { max-width: 800px; margin: 0 auto; }
  .header {
    border-bottom: 1px solid var(--border);
    padding-bottom: 20px; margin-bottom: 28px;
  }
  .h-back { color: var(--accent); text-decoration: none; font-size: 13px; }
  .h-back:hover { text-decoration: underline; }
  .h-title {
    font-family: var(--serif); font-size: 28px; font-weight: 700;
    margin: 12px 0 6px; letter-spacing: -0.5px;
  }
  .h-meta { color: var(--text-50); font-size: 12px; letter-spacing: 0.5px; }

  .section-title {
    font-family: var(--serif); font-size: 18px; font-weight: 700;
    margin: 36px 0 14px; padding-left: 10px;
    border-left: 3px solid var(--accent); line-height: 1.3;
  }

  /* 三个关键判断 */
  .judgement {
    background: var(--bg-card); border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 6px; padding: 18px 20px; margin-bottom: 14px;
  }
  .judgement-num {
    display: inline-block; font-family: var(--mono); font-size: 11px;
    color: var(--accent); letter-spacing: 1px; margin-bottom: 6px;
  }
  .judgement-title {
    font-family: var(--serif); font-size: 17px; font-weight: 700;
    color: var(--text-100); margin: 0 0 10px; line-height: 1.4;
  }
  .judgement-body { color: var(--text-70); font-size: 14px; }

  /* 下周展望 */
  .next-week {
    background: linear-gradient(180deg, rgba(102,192,255,0.06), rgba(94,234,212,0.04));
    border: 1px solid rgba(102,192,255,0.18);
    border-radius: 6px; padding: 18px 20px; margin-bottom: 14px;
  }
  .next-week-label {
    display: inline-block; font-size: 11px; color: var(--accent);
    letter-spacing: 1px; margin-bottom: 8px;
  }
  .next-week-body { color: var(--text-100); font-size: 14px; line-height: 1.6; }

  /* 大事时间线 */
  .events-list { margin: 0; padding: 0; list-style: none; }
  .event-item {
    background: var(--bg-card); border: 1px solid var(--border);
    border-left: 2px solid var(--amber);
    border-radius: 5px; margin-bottom: 8px; padding: 12px 16px;
    transition: background 0.15s;
  }
  .event-item:hover { background: var(--bg-card-hi); }
  .event-meta {
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    margin-bottom: 6px; font-size: 11px;
  }
  .event-imp { color: var(--red); font-weight: 700; }
  .event-date { color: var(--accent); font-family: var(--mono); }
  .event-src { color: var(--text-50); }
  .event-title {
    font-size: 14px; font-weight: 500; color: var(--text-100);
    line-height: 1.45; margin: 0 0 4px;
  }
  .event-title a { color: inherit; text-decoration: none; }
  .event-title a:hover { color: var(--accent); }
  .event-summary { font-size: 12px; color: var(--text-70); line-height: 1.5; margin: 0; }

  /* chip 云(实体 + 分类) */
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 28px; }
  .chip {
    padding: 4px 10px; border-radius: 14px;
    background: rgba(102,192,255,0.08);
    border: 1px solid rgba(102,192,255,0.18);
    color: var(--text-100); font-weight: 500; font-size: 13px;
  }
  .chip-count {
    margin-left: 5px; font-size: 0.8em;
    color: var(--text-50); font-weight: 400;
  }

  .footer {
    margin-top: 48px; padding-top: 20px;
    border-top: 1px solid var(--border);
    color: var(--text-35); font-size: 11px; text-align: center;
  }
  .footer a { color: var(--accent); text-decoration: none; }

  @media (max-width: 640px) {
    body { padding: 16px 12px; }
    .h-title { font-size: 22px; }
    .judgement { padding: 14px 16px; }
    .judgement-title { font-size: 15px; }
  }
</style>
</head>
<body>

<div class="wrap">

<header class="header">
  <a class="h-back" href="../../">← 返回早报</a>
  <h1 class="h-title">📅 $week_label · AI 行业周报</h1>
  <div class="h-meta">$date_range · 基于 $n_events 条 importance ≥ 4 的核心事件</div>
</header>

<h2 class="section-title">📌 本周三个关键判断</h2>
$judgements_html

<h2 class="section-title">🔭 下周值得关注</h2>
<div class="next-week">
  <div class="next-week-label">FORWARD-LOOKING · 信号埋点</div>
  <div class="next-week-body">$next_week_html</div>
</div>

<h2 class="section-title">⭐ 本周大事时间线($n_events 条)</h2>
<ul class="events-list">
$events_html
</ul>

<h2 class="section-title">🏷️ 本周焦点实体</h2>
<div class="chips">$entity_chips_html</div>

<h2 class="section-title">📊 本周话题分布</h2>
<div class="chips">$category_chips_html</div>

<footer class="footer">
  AI 早报 · 本周回顾 · 生成于 $generated_at<br>
  <a href="../../">← 返回最新早报</a> · <a href="../dashboard.html">📊 跑步仪表盘</a>
</footer>

</div>
</body>
</html>
"""


def render_weekly_html(insights, events, entities, cats,
                       start, end, week_label) -> str:
    """渲染周报 HTML。"""
    # 三个判断
    judg_blocks = []
    for i in (1, 2, 3):
        j = insights.get(f'judgement_{i}', {})
        title = escape((j.get('title') or '').strip())
        body = escape((j.get('body') or '').strip())
        judg_blocks.append(
            f'<div class="judgement">'
            f'<div class="judgement-num">JUDGEMENT 0{i}</div>'
            f'<h3 class="judgement-title">{title}</h3>'
            f'<div class="judgement-body">{body}</div>'
            f'</div>'
        )
    judgements_html = '\n'.join(judg_blocks)

    # 下周展望
    next_week_html = escape((insights.get('next_week_focus') or '').strip())

    # 事件时间线
    event_blocks = []
    for ev in events:
        eid, pub_iso, title, summary, deep, imp, src, url = ev
        try:
            pub_dt = datetime.datetime.fromisoformat(
                (pub_iso or '').replace('Z', '+00:00')
            ).astimezone(CN_TZ)
            date_str = pub_dt.strftime('%m-%d %H:%M')
        except (ValueError, TypeError, AttributeError):
            date_str = '?'
        title_e = escape((title or '').strip())
        summary_e = escape((summary or '').strip())
        src_e = escape((src or '').strip())
        url_e = escape((url or '').strip())
        title_link = (
            f'<a href="{url_e}" target="_blank" rel="noopener">{title_e}</a>'
            if url_e else title_e
        )
        event_blocks.append(
            f'<li class="event-item">'
            f'<div class="event-meta">'
            f'<span class="event-imp">★{imp}</span>'
            f'<span class="event-date">{date_str}</span>'
            f'<span class="event-src">{src_e}</span>'
            f'</div>'
            f'<div class="event-title">{title_link}</div>'
            f'<p class="event-summary">{summary_e}</p>'
            f'</li>'
        )
    events_html = '\n'.join(event_blocks)

    # 实体 chip 云(top 12)
    if entities:
        max_n = entities[0][1] or 1
        entity_chips = []
        for name, n in entities[:12]:
            size = 13 + int(round((n / max_n) * 6))
            entity_chips.append(
                f'<span class="chip" style="font-size:{size}px">'
                f'{escape(name)}<span class="chip-count">×{n}</span>'
                f'</span>'
            )
        entity_chips_html = ' '.join(entity_chips)
    else:
        entity_chips_html = '<span class="chip">本周暂无显著实体</span>'

    # 分类 chip 云(top 8)
    if cats:
        max_c = cats[0][1] or 1
        cat_chips = []
        for name, n in cats[:8]:
            cat_chips.append(
                f'<span class="chip">{escape(name)}<span class="chip-count">×{n}</span></span>'
            )
        category_chips_html = ' '.join(cat_chips)
    else:
        category_chips_html = '<span class="chip">本周暂无显著分类</span>'

    date_range = (
        f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}"
    )
    generated_at = datetime.datetime.now(CN_TZ).strftime('%Y-%m-%d %H:%M')

    return Template(WEEKLY_HTML_TEMPLATE).safe_substitute(
        title=f'{week_label} · AI 行业周报',
        meta_description=f'AI 行业周报 {date_range},基于 {len(events)} 条 importance ≥ 4 的核心事件。',
        week_label=escape(week_label),
        date_range=escape(date_range),
        n_events=len(events),
        judgements_html=judgements_html,
        next_week_html=next_week_html,
        events_html=events_html,
        entity_chips_html=entity_chips_html,
        category_chips_html=category_chips_html,
        generated_at=escape(generated_at),
    )


# ──────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────

def parse_days_arg() -> int:
    for i, a in enumerate(sys.argv):
        if a == '--days' and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return 7


def main():
    script_dir = Path(__file__).parent
    config_path = script_dir / 'config.json'
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)

    days = parse_days_arg()
    end = datetime.datetime.now(CN_TZ)
    start = end - datetime.timedelta(days=days)
    iso_year, iso_week, _ = end.isocalendar()
    week_label = f'{iso_year}-W{iso_week:02d}'

    log.info("生成 %s 周报: %s ~ %s (last %d days)",
             week_label, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'), days)

    db_path = script_dir / 'events.db'
    if not db_path.exists():
        log.error("events.db 不存在: %s", db_path)
        sys.exit(1)

    events = fetch_important_events(db_path, start, end)
    log.info("取到 %d 条 imp>=4 事件", len(events))
    if not events:
        log.warning("本周无 imp>=4 事件,跳过周报生成")
        sys.exit(0)

    entities = fetch_entity_distribution(
        db_path, script_dir / 'entity_registry.json', start, end)
    cats = fetch_category_distribution(db_path, start, end)
    log.info("Top 5 实体: %s", [f'{n}×{c}' for n, c in entities[:5]])
    log.info("Top 5 分类: %s", [f'{n}×{c}' for n, c in cats[:5]])

    log.info("调用 LLM 生成本周三个判断 + 下周展望...")
    llm = create_analyzer_from_config(config)
    insights = generate_insights(events, llm)
    if not insights:
        log.error("LLM 生成失败,跳过")
        sys.exit(1)

    log.info("✓ Insights:")
    for i in (1, 2, 3):
        j = insights.get(f'judgement_{i}', {})
        log.info("  J%d: %s", i, j.get('title', ''))
    log.info("  下周: %s", (insights.get('next_week_focus') or '')[:50] + '...')

    out_dir = script_dir / 'output' / 'weekly'
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_weekly_html(insights, events, entities, cats, start, end, week_label)

    out_path = out_dir / f'{week_label}.html'
    out_path.write_text(html, encoding='utf-8')
    latest_path = out_dir / 'latest.html'
    latest_path.write_text(html, encoding='utf-8')
    log.info("✓ 周报已生成: %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)

    # 元数据 (供其他页面读)
    meta = {
        'week_label': week_label,
        'date_range': f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}",
        'n_events': len(events),
        'judgement_titles': [
            insights.get(f'judgement_{i}', {}).get('title', '') for i in (1, 2, 3)
        ],
        'next_week_focus': insights.get('next_week_focus', ''),
        'generated_at': datetime.datetime.now(CN_TZ).isoformat(),
    }
    meta_path = out_dir / f'{week_label}.json'
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    log.info("✓ 元数据: %s", meta_path)


if __name__ == '__main__':
    main()
