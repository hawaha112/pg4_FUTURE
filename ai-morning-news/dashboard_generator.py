#!/usr/bin/env python3
"""dashboard_generator.py — 读 output/run_health.jsonl 生成跑步健康仪表盘 HTML

展示内容：
- 顶部 KPI 卡片（最近一次 kept / LLM 覆盖率 / 多源事件 / 用时 / 源健康）
- 4 个时序图（近 60 次跑）：条目数、LLM 覆盖率、多源事件、用时
- 全部跑次表格（时间倒序）

自包含：jsonl 数据内联到 HTML，Chart.js 走 CDN。
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from html import escape
from pathlib import Path

MAX_RUNS = 60


def _kb_stats(events_db_path: Path, days: int = 7) -> dict:
    """查询 events.db，返回 Tier 2 知识库累积指标。

    包括：
    - total_events / new_events_Nd
    - multi_total / multi_pct
    - entity_total / new_entities_Nd
    - first_t01_total / first_t01_Nd
    - articles_total
    """
    out = {
        'total_events': 0, 'new_events': 0,
        'multi_total': 0, 'multi_pct': 0.0,
        'important_total': 0, 'important_recent': 0,
        'first_t01_total': 0, 'first_t01_recent': 0,
        'articles_total': 0, 'days': days,
    }
    if not events_db_path.exists():
        return out
    try:
        db = sqlite3.connect(f'file:{events_db_path}?mode=ro', uri=True)
        cur = db.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # 累计 / 近 N 天事件
        out['total_events'] = cur.execute(
            "SELECT COUNT(*) FROM canonical_events"
        ).fetchone()[0]
        out['new_events'] = cur.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE first_seen_at >= ?",
            (cutoff,)
        ).fetchone()[0]

        # 跨源事件（distinct source >= 2）
        out['multi_total'] = cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT event_id FROM evidence
                GROUP BY event_id HAVING COUNT(DISTINCT source_name) >= 2
            )
        """).fetchone()[0]
        if out['total_events']:
            out['multi_pct'] = round(out['multi_total'] / out['total_events'] * 100, 1)

        # 重要事件累计 / 近 N 天（importance >= 4）
        # 注：events.db 里 entity_tags 字段长期为空（聚类阶段未填），
        # 这里改用「重要事件累计」作为更可靠的"知识资产"指标
        out['important_total'] = cur.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE importance >= 4"
        ).fetchone()[0]
        out['important_recent'] = cur.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE importance >= 4 AND first_seen_at >= ?",
            (cutoff,)
        ).fetchone()[0]

        # 首发独家：first_reporter 来自 T0/T1 源
        out['first_t01_total'] = cur.execute("""
            SELECT COUNT(DISTINCT event_id) FROM evidence
            WHERE role = 'first_reporter' AND source_tier <= 1
        """).fetchone()[0]
        out['first_t01_recent'] = cur.execute("""
            SELECT COUNT(DISTINCT event_id) FROM evidence
            WHERE role = 'first_reporter' AND source_tier <= 1
              AND reported_at >= ?
        """, (cutoff,)).fetchone()[0]

        out['articles_total'] = cur.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0]

        db.close()
    except sqlite3.Error:
        pass
    return out


def _render_kb_section(script_dir: Path) -> str:
    """渲染 Tier 2「📚 知识库累积」section：4 张快照卡。"""
    db_path = script_dir / 'events.db'
    s = _kb_stats(db_path, days=7)

    def card(label, val, sub):
        return (
            f'<div class="kpi">'
            f'<div class="kpi-label">{label}</div>'
            f'<div class="kpi-val">{val}</div>'
            f'<div class="kpi-sub">{sub}</div>'
            f'</div>'
        )

    cards = (
        card('累计事件',
             f'{s["total_events"]}',
             f'近 {s["days"]}d +{s["new_events"]} · {s["articles_total"]} 篇文章')
        + card('跨源累计',
               f'{s["multi_total"]}<span style="font-size:14px;color:var(--text-50)"> / {s["total_events"]}</span>',
               f'多源交叉确认 · {s["multi_pct"]}%')
        + card('重要事件',
               f'{s["important_total"]}',
               f'importance ≥ 4 · 近 {s["days"]}d +{s["important_recent"]}')
        + card('首发独家',
               f'{s["first_t01_total"]}',
               f'T0/T1 抢先报道 · 近 {s["days"]}d +{s["first_t01_recent"]}')
    )

    return (
        f'<div class="section-title kb-title">📚 知识库累积</div>'
        f'<div class="kb-meta">events.db 全库快照 · 跨班次跨日期累计的「项目资产」</div>'
        f'<div class="kpis">{cards}</div>'
    )


def _collect_recent_errors(script_dir: Path, max_errors: int = 30) -> list:
    """从 daily_run.log 抓最近 N 条 [W]/[E] 行做错误聚合。

    返回 list of {ts, level, module, message, count}（按出现频次降序）。
    """
    log_path = script_dir / 'daily_run.log'
    if not log_path.exists():
        return []
    import re
    from collections import Counter
    pattern = re.compile(r'^(\d{2}:\d{2}:\d{2}) \[([WE])\] ([^:]+): (.{0,200})')
    seen_keys = Counter()
    samples = {}  # key → last (ts, msg)
    try:
        # 只读最后 ~500KB 避免大日志慢
        with open(log_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 500_000))
            tail = f.read().decode('utf-8', errors='ignore')
        for line in tail.split('\n'):
            m = pattern.match(line)
            if not m:
                continue
            ts, lvl, mod, msg = m.groups()
            # 去重 key：模块 + 错误消息前 60 字
            key = (lvl, mod.strip(), msg.strip()[:60])
            seen_keys[key] += 1
            samples[key] = (ts, msg.strip())
    except OSError:
        return []
    # 按频次降序，返回前 max_errors 个
    out = []
    for (lvl, mod, _), cnt in seen_keys.most_common(max_errors):
        ts, msg = samples[(lvl, mod, _)]
        out.append({'level': lvl, 'module': mod, 'count': cnt,
                    'last_ts': ts, 'message': msg})
    return out


def _collect_source_stats(script_dir: Path) -> list:
    """汇总每个源的贡献与健康数据。返回 list of dict，按近7d贡献数降序。"""
    config_path = script_dir / 'config.json'
    db_path = script_dir / 'events.db'
    health_path = script_dir / 'source_health.json'

    sources = []
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding='utf-8'))
            for lang in ('english', 'chinese'):
                for s in cfg.get('sources', {}).get(lang, []):
                    sources.append({
                        'name': s['name'],
                        'url': s.get('url', ''),
                        'tier': s.get('tier', 2),
                        'category': s.get('category', ''),
                        'lang': lang,
                        'enabled': s.get('enabled', True) and not s.get('disabled', False),
                    })
        except (json.JSONDecodeError, KeyError, OSError):
            return []

    # articles 统计
    stats_by_src = {}
    if db_path.exists():
        try:
            db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
            # 总条数、近7天条数、近7天 importance>=3 条数、均 importance、最近采集时间
            rows = db.execute("""
                SELECT source_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN collected_at >= datetime('now','-7 days') THEN 1 ELSE 0 END) AS w7,
                       SUM(CASE WHEN collected_at >= datetime('now','-7 days')
                                 AND importance >= 3 THEN 1 ELSE 0 END) AS w7_hi,
                       ROUND(AVG(CASE WHEN collected_at >= datetime('now','-7 days')
                                      THEN importance ELSE NULL END), 2) AS avg_imp_w7,
                       MAX(collected_at) AS last_collected
                FROM articles
                WHERE ai_relevant = 1
                GROUP BY source_name
            """).fetchall()
            for name, total, w7, w7_hi, avg_imp, last in rows:
                stats_by_src[name] = {
                    'total': total or 0,
                    'w7': w7 or 0,
                    'w7_hi': w7_hi or 0,
                    'avg_imp_w7': avg_imp or 0.0,
                    'last_collected': last or '',
                }
            db.close()
        except sqlite3.Error:
            pass

    health = {}
    if health_path.exists():
        try:
            health = json.loads(health_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass

    merged = []
    for s in sources:
        st = stats_by_src.get(s['name'], {})
        h = health.get(s['name'], {})
        fails = h.get('consecutive_failures', 0)
        if not s['enabled']:
            health_tag = 'disabled'
        elif fails >= 10:
            health_tag = 'dead'
        elif fails >= 3:
            health_tag = 'failing'
        else:
            health_tag = 'ok'
        merged.append({
            **s,
            'total': st.get('total', 0),
            'w7': st.get('w7', 0),
            'w7_hi': st.get('w7_hi', 0),
            'avg_imp_w7': st.get('avg_imp_w7', 0.0),
            'last_collected': st.get('last_collected', ''),
            'consec_fails': fails,
            'health_tag': health_tag,
        })
    # 排序：enabled 先，按 w7 desc，再按 w7_hi desc
    merged.sort(key=lambda x: (
        0 if x['enabled'] else 1,
        -x['w7'],
        -x['w7_hi'],
        -x['total'],
    ))
    return merged


def _render_week_important(script_dir: Path) -> str:
    """渲染过去 7 天 importance ≥ 4 的事件，按事件发布日期(published_at)分组。
    每条显示具体时间 + 来源 + 跳归档链接(按 rendered_at 推断 am/pm 班次)。
    """
    import sqlite3
    from collections import OrderedDict
    db_path = script_dir / 'events.db'
    if not db_path.exists():
        return ''
    try:
        con = sqlite3.connect(str(db_path))
        rows = con.execute("""
            SELECT
                published_at,
                json_extract(analysis, '$.chinese_title') AS title,
                event_id,
                rendered_at,
                (SELECT a.source_name FROM articles a
                  WHERE a.canonical_event_id = e.event_id LIMIT 1) AS src
            FROM canonical_events e
            WHERE rendered_at >= datetime('now','-7 days')
              AND importance >= 4
              AND analysis IS NOT NULL
              AND published_at IS NOT NULL
            ORDER BY datetime(published_at) DESC
        """).fetchall()
        con.close()
    except sqlite3.Error:
        return ''

    if not rows:
        return ''

    def _parse_dt(s):
        try:
            return datetime.fromisoformat((s or '').replace('Z', '+00:00')).astimezone()
        except (ValueError, TypeError, AttributeError):
            return None

    # 按事件发布的本地日期分组（DESC 顺序保持）
    by_date = OrderedDict()
    earliest_pub = None
    latest_pub = None
    for pub_iso, title, eid, rendered_at, src in rows:
        pub_dt = _parse_dt(pub_iso)
        rendered_dt = _parse_dt(rendered_at)
        if pub_dt is None:
            continue
        if earliest_pub is None or pub_dt < earliest_pub:
            earliest_pub = pub_dt
        if latest_pub is None or pub_dt > latest_pub:
            latest_pub = pub_dt
        d_key = pub_dt.strftime('%Y-%m-%d')
        # 用 rendered_at 推断这条事件落到哪个班次的归档页
        shift = 'pm' if (rendered_dt and rendered_dt.hour >= 12) else 'am'
        rendered_date_key = (
            rendered_dt.strftime('%Y-%m-%d') if rendered_dt else d_key
        )
        by_date.setdefault(d_key, []).append({
            'title': title or '',
            'event_id': eid,
            'src': src or '',
            'time_str': pub_dt.strftime('%H:%M'),
            # dashboard.html 自己就在 archive/ 下，归档页同目录，不带前缀。
            # #evt- hash 让归档页里 script.js 滚到对应卡片并自动展开 modal。
            'archive_url': f'{rendered_date_key}-{shift}.html#evt-{eid}',
        })

    # section title 的日期范围
    if earliest_pub and latest_pub:
        e_str = earliest_pub.strftime('%m-%d')
        l_str = latest_pub.strftime('%m-%d')
        date_range = e_str if e_str == l_str else f'{e_str} ~ {l_str}'
    else:
        date_range = '近 7 天'

    # 中文星期映射，方便快速识别
    weekday_zh = ['一', '二', '三', '四', '五', '六', '日']

    lines = []
    for d, items in by_date.items():
        # 给日期加上"周X"和"今天/昨天"提示
        try:
            d_dt = datetime.strptime(d, '%Y-%m-%d').astimezone()
            wd = weekday_zh[d_dt.weekday()]
            today_local = datetime.now().astimezone().date()
            if d_dt.date() == today_local:
                hint = '今天'
            elif (today_local - d_dt.date()).days == 1:
                hint = '昨天'
            else:
                hint = f'周{wd}'
            label = f'{d} · {hint}'
        except ValueError:
            label = d
        lines.append(f'<div class="wi-date">{escape(label)}</div>')
        for it in items:
            t = (it['title'] or '')[:64]
            lines.append(
                f'<div class="wi-row">'
                f'<a href="{escape(it["archive_url"])}" target="_blank" rel="noopener">'
                f'<span class="wi-star">⭐</span>'
                f'<span class="wi-time">{escape(it["time_str"])}</span>'
                f'<span class="wi-title">{escape(t)}</span>'
                f'<span class="wi-src">{escape(it["src"])}</span>'
                f'</a></div>'
            )

    return (
        f'<div class="section-title">📅 本周重要事件 · {date_range} · 共 {len(rows)} 条</div>'
        f'<div class="wi-block">{"".join(lines)}</div>'
    )


def _render_week_entities(script_dir: Path) -> str:
    """扫描过去 7 天 ai_relevant 文章的标题/摘要，统计预定义实体出现次数。
    渲染成 chip 云，字号按频次加权。
    """
    import sqlite3
    import re as _re
    db_path = script_dir / 'events.db'
    reg_path = script_dir / 'entity_registry.json'
    if not db_path.exists() or not reg_path.exists():
        return ''

    try:
        registry = json.loads(reg_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return ''

    entities = registry.get('entities', [])
    patterns = []  # [(name, compiled_pattern)]
    for e in entities:
        kws = e.get('keywords') or []
        if not kws:
            continue
        pat = _re.compile('|'.join(_re.escape(k) for k in kws), _re.IGNORECASE)
        patterns.append((e.get('name') or e.get('id'), pat))

    if not patterns:
        return ''

    try:
        con = sqlite3.connect(str(db_path))
        rows = con.execute("""
            SELECT title,
                   json_extract(analysis,'$.chinese_title'),
                   json_extract(analysis,'$.summary')
            FROM articles
            WHERE collected_at >= datetime('now','-7 days')
              AND ai_relevant = 1
        """).fetchall()
        con.close()
    except sqlite3.Error:
        return ''

    if not rows:
        return ''

    counts = {}
    for title, ct, sm in rows:
        text = ' '.join(filter(None, [title, ct, sm]))
        if not text:
            continue
        for name, pat in patterns:
            if pat.search(text):
                counts[name] = counts.get(name, 0) + 1

    if not counts:
        return ''

    sorted_e = sorted(counts.items(), key=lambda x: -x[1])[:12]
    max_n = sorted_e[0][1]

    chips = []
    for name, n in sorted_e:
        # 字号 13–22 px，亮度 0.55–1.0
        size = 13 + int(round((n / max_n) * 9))
        intensity = 0.55 + (n / max_n) * 0.45
        chips.append(
            f'<span class="ent-chip" style="font-size:{size}px;opacity:{intensity:.2f}">'
            f'{escape(name)}<span class="ent-count">×{n}</span>'
            f'</span>'
        )

    total_articles = len(rows)
    return (
        f'<div class="section-title">🏷️ 本周焦点实体 · {total_articles} 篇文章累积</div>'
        f'<div class="ent-chips">{" ".join(chips)}</div>'
    )


def main():
    script_dir = Path(__file__).parent
    jsonl_path = script_dir / 'output' / 'run_health.jsonl'
    out_path = script_dir / 'output' / 'dashboard.html'

    if not jsonl_path.exists():
        print(f'⚠️ no run_health.jsonl at {jsonl_path}, skip')
        return

    runs = []
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not runs:
        print('⚠️ run_health.jsonl empty')
        return

    runs = runs[-MAX_RUNS:]
    latest = runs[-1]

    # 时间标签：UTC→本地（假设 CST +8；UTC isoformat 以 Z 结尾）
    def _fmt_label(run_id: str) -> str:
        try:
            dt = datetime.fromisoformat(run_id.replace('Z', '+00:00'))
            return dt.astimezone().strftime('%m-%d %H:%M')
        except (ValueError, TypeError):
            return run_id[:16].replace('T', ' ')

    # KPI 区按班次分组（早班一组 / 晚班一组），数据从下方计算的 today_am/today_pm 取

    # 最近运行表（时间倒序，时间列点击跳到该次归档 HTML）
    # 注：dashboard 自身在 archive/ 下，所以归档链接用相对路径 `../archive/YYYY-MM-DD-{shift}.html`
    # 但相对 dashboard 所在的 archive/dashboard.html 来说，归档在同级目录
    def _archive_url(run_id: str, shift: str) -> str:
        """从 run_id (UTC ISO) 推断本地日期，拼归档 URL。"""
        try:
            dt = datetime.fromisoformat(run_id.replace('Z', '+00:00')).astimezone()
            date_str = dt.strftime('%Y-%m-%d')
            if shift in ('am', 'pm'):
                return f'./{date_str}-{shift}.html'
            # 老格式（无 shift）— 旧归档命名只有日期
            return f'./{date_str}.html'
        except (ValueError, TypeError):
            return ''

    table_rows = []
    for r in reversed(runs[-14:]):  # 一周 ~14 次（早晚两班 × 7 天）
        run_id = r.get('run_id', '')
        shift = r.get('shift', '') or ''
        label = _fmt_label(run_id)
        sh = {'am': '🌅 早', 'pm': '🌆 晚'}.get(shift, '—')
        kept = r.get('kept', 0)
        llm = round((r.get('llm_coverage') or 0) * 100, 1)
        multi = r.get('multi_source_count', 0)
        dur = round((r.get('duration_sec') or 0) / 60.0, 1)
        deploy = '✓' if r.get('deploy_ok') else '✗'
        ok = r.get('sources_healthy', 0)
        dead = len(r.get('dead_sources') or [])
        archive_href = _archive_url(run_id, shift)
        if archive_href:
            time_cell = f'<a href="{escape(archive_href)}" target="_blank" title="打开本次归档">{escape(label)} ↗</a>'
        else:
            time_cell = escape(label)
        # 高亮异常：kept=0 或 deploy 失败
        row_style = ''
        if kept == 0 or not r.get('deploy_ok'):
            row_style = ' style="background:rgba(192,80,80,0.08)"'
        table_rows.append(
            f'<tr{row_style}><td>{time_cell}</td><td>{sh}</td>'
            f'<td>{kept}</td><td>{llm}%</td><td>{multi}</td>'
            f'<td>{dur}</td><td>{ok}/{dead}</td><td>{deploy}</td></tr>'
        )
    table_body = '\n'.join(table_rows)

    # ── 今日双班快捷卡（早班 / 晚班，可点击直达对应归档）──
    today_local = datetime.now().astimezone().strftime('%Y-%m-%d')

    def _local_date(rid: str) -> str:
        try:
            return datetime.fromisoformat(rid.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d')
        except (ValueError, TypeError):
            return ''

    today_am, today_pm = None, None
    for r in runs:
        if _local_date(r.get('run_id', '')) != today_local:
            continue
        sh = r.get('shift', '')
        if sh == 'am' and (today_am is None or r.get('run_id', '') > today_am.get('run_id', '')):
            today_am = r
        elif sh == 'pm' and (today_pm is None or r.get('run_id', '') > today_pm.get('run_id', '')):
            today_pm = r

    def _today_card(run, shift: str, label: str, emoji: str) -> str:
        if not run:
            return (
                f'<div class="today-card today-empty">'
                f'<div class="tc-row"><span class="tc-emoji">{emoji}</span>'
                f'<span class="tc-shift">{label}</span></div>'
                f'<div class="tc-status">⏳ 尚未发布</div>'
                f'</div>'
            )
        href = _archive_url(run.get('run_id', ''), shift)
        kept = run.get('kept', 0)
        llm = int(round((run.get('llm_coverage') or 0) * 100))
        time_str = _fmt_label(run.get('run_id', ''))
        deploy_ok = run.get('deploy_ok')
        flag = '' if deploy_ok else ' <span class="tc-warn">部署失败</span>'
        return (
            f'<a href="{escape(href)}" target="_blank" class="today-card today-active" '
            f'title="打开 {label} 归档">'
            f'<div class="tc-row"><span class="tc-emoji">{emoji}</span>'
            f'<span class="tc-shift">{label}</span></div>'
            f'<div class="tc-meta">{escape(time_str)} · <b>{kept}</b> 条 · LLM {llm}%{flag}</div>'
            f'<div class="tc-cta">打开归档 →</div>'
            f'</a>'
        )

    today_html = (
        f'<div class="today-grid">'
        f'<div class="today-title">📅 今日发布 · {today_local}</div>'
        f'<div class="today-cards">'
        f'{_today_card(today_am, "am", "早班 · 早报", "🌅")}'
        f'{_today_card(today_pm, "pm", "晚班 · 晚报", "🌆")}'
        f'</div></div>'
    )

    # ── 班次分组 KPI（早班一组 / 晚班一组，每组 5 张经典大数字卡）──
    def _kpi_card(label: str, val_html: str, sub: str, extra_cls: str = '',
                  expandable_items: list = None) -> str:
        """普通 KPI 卡。若传入 expandable_items，则卡片可点击展开为列表。"""
        if expandable_items:
            from html import escape as _esc

            def _norm_archive_href(u: str) -> str:
                """dashboard.html 在 archive/ 下，stats.json 里 important_events.archive_url
                带 'archive/' 前缀（设计给主页用），从这里出发会变成 /archive/archive/X.html
                而 404。剥掉前缀即可。"""
                if u and u.startswith('archive/'):
                    return u[len('archive/'):]
                return u or '#'

            def _add_evt_hash(href: str, eid: str) -> str:
                """追加 #evt-<event_id>，归档页 script.js 会据此滚到对应卡 + 开 modal"""
                if not eid or '#' in (href or ''):
                    return href
                return f'{href}#evt-{eid}'

            items_html = ''.join(
                f'<li><a href="{_esc(_add_evt_hash(_norm_archive_href(ev.get("archive_url")), ev.get("event_id") or ""))}" '
                f'target="_blank" rel="noopener" '
                f'title="{_esc(ev.get("source_name") or "")}">'
                f'<span class="kpi-list-star">⭐</span>'
                f'<span class="kpi-list-title">'
                f'{_esc((ev.get("title") or "")[:80])}</span>'
                f'<span class="kpi-list-src">{_esc(ev.get("source_name") or "")}</span>'
                f'</a></li>'
                for ev in expandable_items
            )
            return (
                f'<details class="kpi kpi-expandable" open>'
                f'<summary>'
                f'<div class="kpi-label">{label} <span class="kpi-toggle">▾</span></div>'
                f'<div class="kpi-val {extra_cls}">{val_html}</div>'
                f'<div class="kpi-sub">{sub} · 点击折叠</div>'
                f'</summary>'
                f'<ul class="kpi-list">{items_html}</ul>'
                f'</details>'
            )
        return (
            f'<div class="kpi">'
            f'<div class="kpi-label">{label}</div>'
            f'<div class="kpi-val {extra_cls}">{val_html}</div>'
            f'<div class="kpi-sub">{sub}</div>'
            f'</div>'
        )

    def _kpi_panel(run, shift: str) -> str:
        """渲染一个班次 tab 对应的 panel：5 张 Tier 1 内容价值 KPI + 底部状态条。"""
        active_attr = ' active' if shift == default_shift else ''
        if run is None:
            return (
                f'<div class="kpi-panel{active_attr}" data-shift="{shift}">'
                f'<div class="kpi-empty">⏳ 本班次尚未发布</div>'
                f'</div>'
            )

        kept = max(int(run.get('kept', 0) or 0), 1)  # 防 0 除
        important = int(run.get('important_count', 0) or 0)
        multi = int(run.get('multi_source_count', 0) or 0)
        official = int(run.get('official_count', 0) or 0)
        depth = int(run.get('depth_count', 0) or 0)
        entities = int(run.get('entity_count', 0) or 0)

        important_pct = round(important / kept * 100, 0)
        multi_pct = round(multi / kept * 100, 1)
        depth_pct = round(depth / kept * 100, 0)

        # 用于状态条
        llm_pct = int(round((run.get('llm_coverage') or 0) * 100))
        dur_sec = int(run.get('duration_sec') or 0)
        dur_mmss = f'{dur_sec // 60}:{dur_sec % 60:02d}'
        ok = int(run.get('sources_healthy', 0) or 0)
        fail = int(run.get('sources_failing', 0) or 0)
        dead = len(run.get('dead_sources') or [])
        deploy_ok = bool(run.get('deploy_ok'))
        ts_label = _fmt_label(run.get('run_id', ''))

        important_events_list = run.get('important_events') or []

        cards = _kpi_card(
            '重要事件',
            str(important),
            f'importance ≥ 4 · 占总条目 {int(important_pct)}%',
            extra_cls='hi' if important > 0 else '',
            expandable_items=important_events_list if important > 0 else None,
        )

        # ── Tier 3 状态条（运维降级为单行小字）──
        bullets = []
        bullets.append(
            f'<span class="sb-{"ok" if deploy_ok else "fail"}">'
            f'{"✅" if deploy_ok else "❌"} 部署{"成功" if deploy_ok else "失败"}'
            f'</span>')
        bullets.append(f'<span class="sb-mut">⏱ 用时 {dur_mmss} <em>({dur_sec}s)</em></span>')
        llm_cls = 'sb-ok' if llm_pct >= 80 else 'sb-warn'
        bullets.append(f'<span class="{llm_cls}">🧠 LLM {llm_pct}%</span>')
        dead_cls = 'sb-ok' if dead == 0 else 'sb-fail'
        fail_cls = 'sb-ok' if fail == 0 else 'sb-warn'
        bullets.append(
            f'<span class="{dead_cls}">💀 死源 {dead}</span>'
            f'<span class="sb-mut sb-sep">·</span>'
            f'<span class="{fail_cls}">⚠ 告警 {fail}</span>'
            f'<span class="sb-mut sb-sep">·</span>'
            f'<span class="sb-mut">📡 OK {ok}</span>'
        )
        status_bar = (
            f'<div class="status-bar">'
            f'<span class="sb-label">系统状态</span>'
            + '<span class="sb-mut sb-sep">·</span>'.join(bullets)
            + f'</div>'
        )

        return (
            f'<div class="kpi-panel{active_attr}" data-shift="{shift}">'
            f'<div class="kpi-panel-meta">{escape(ts_label)} · 收录 <b>{kept}</b> 条 AI 资讯</div>'
            f'<div class="kpis">{cards}</div>'
            f'{status_bar}'
            f'</div>'
        )

    # 默认激活的 tab：最近一次跑的班次；若它无效则退回另一个
    default_shift = latest.get('shift', '') or 'pm'
    if default_shift == 'am' and today_am is None and today_pm is not None:
        default_shift = 'pm'
    elif default_shift == 'pm' and today_pm is None and today_am is not None:
        default_shift = 'am'

    am_status = '⏳ 尚未发布' if today_am is None else _fmt_label(today_am.get('run_id', ''))
    pm_status = '⏳ 尚未发布' if today_pm is None else _fmt_label(today_pm.get('run_id', ''))
    am_active = ' active' if default_shift == 'am' else ''
    pm_active = ' active' if default_shift == 'pm' else ''

    kpi_blocks_html = (
        f'<div class="kpi-tabs">'
        f'<button type="button" class="kpi-tab{am_active}" data-shift="am">'
        f'<span class="kt-emoji">🌅</span><span class="kt-name">早班</span>'
        f'<span class="kt-meta">{escape(am_status)}</span></button>'
        f'<button type="button" class="kpi-tab{pm_active}" data-shift="pm">'
        f'<span class="kt-emoji">🌆</span><span class="kt-name">晚班</span>'
        f'<span class="kt-meta">{escape(pm_status)}</span></button>'
        f'</div>'
        f'<div class="kpi-panels">'
        f'{_kpi_panel(today_am, "am")}'
        f'{_kpi_panel(today_pm, "pm")}'
        f'</div>'
    )

    # 早期版本这里构建过 知识库累积 / 错误聚合 / 数据源质量 三个 section，
    # 反馈：日常读者用不上这些运维数据，已从首屏下线。
    # 函数 _render_kb_section / _collect_recent_errors / _collect_source_stats
    # 保留在文件顶部，未来做独立"运维页"时可直接复用。

    # 本周维度的两个 section：补充早报只看当天的局限
    week_important_html = _render_week_important(script_dir)
    week_entities_html = _render_week_entities(script_dir)

    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 早报 · 跑步仪表盘</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  /* ═════ Tech Innovation theme: 电蓝 + 霓虹青 + 深灰 ═════ */
  :root {{
    --bg: #0a0e1a;
    --bg-elev: #11162a;
    --bg-card: rgba(0, 102, 255, 0.04);
    --bg-card-hover: rgba(0, 102, 255, 0.08);
    --text-100: #e8eaed;
    --text-70: rgba(232,234,237,0.72);
    --text-50: rgba(232,234,237,0.50);
    --text-30: rgba(232,234,237,0.30);
    --border: rgba(0, 102, 255, 0.16);
    --border-strong: rgba(0, 102, 255, 0.32);
    --accent: #0066ff;       /* 电蓝 */
    --accent-hi: #00ffff;    /* 霓虹青 */
    --accent-glow: rgba(0, 255, 255, 0.35);
    --green: #34d399; --amber: #fbbf24; --red: #f87171;
    --sans: 'Inter', 'Noto Sans SC', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    --mono: 'JetBrains Mono', 'SF Mono', Menlo, monospace;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: radial-gradient(circle at 0% 0%, rgba(0,102,255,0.06), transparent 50%),
                radial-gradient(circle at 100% 100%, rgba(0,255,255,0.04), transparent 50%),
                var(--bg);
    color: var(--text-100); font-family: var(--sans);
    padding: 32px 24px; line-height: 1.55;
    -webkit-font-smoothing: antialiased;
    min-height: 100vh;
  }}
  .wrap {{ max-width: 1240px; margin: 0 auto; }}

  /* ─── Header ─── */
  .hdr {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 6px; flex-wrap: wrap; }}
  h1 {{
    font-size: 28px; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, var(--text-100) 0%, var(--accent-hi) 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  .hdr-tag {{
    font-family: var(--mono); font-size: 10px; font-weight: 700;
    color: var(--accent-hi); border: 1px solid var(--accent-hi);
    padding: 2px 8px; border-radius: 3px; letter-spacing: 0.5px;
    text-shadow: 0 0 8px var(--accent-glow);
  }}
  .subtitle {{ color: var(--text-50); font-size: 12px; margin-bottom: 32px; font-family: var(--mono); }}
  .subtitle a {{ color: var(--accent-hi); text-decoration: none; transition: text-shadow 0.2s; }}
  .subtitle a:hover {{ text-shadow: 0 0 8px var(--accent-glow); }}

  /* ─── 今日双班快捷卡 ─── */
  .today-grid {{ margin: 0 0 28px; }}
  .today-title {{
    font-family: var(--mono); font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1.6px;
    color: var(--accent-hi); margin-bottom: 12px;
    padding-left: 12px; border-left: 3px solid var(--accent-hi);
    text-shadow: 0 0 8px rgba(0,255,255,0.3);
  }}
  .today-cards {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 14px;
  }}
  .today-card {{
    display: block; padding: 18px 22px; border-radius: 14px;
    background: linear-gradient(135deg, rgba(0,102,255,0.12) 0%, rgba(0,255,255,0.04) 100%);
    border: 1px solid rgba(0,255,255,0.20);
    text-decoration: none; color: inherit; position: relative; overflow: hidden;
    transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
  }}
  .today-card::before {{
    content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
    background: linear-gradient(180deg, var(--accent) 0%, var(--accent-hi) 100%);
  }}
  .today-card.today-active:hover {{
    transform: translateY(-2px);
    border-color: var(--accent-hi);
    box-shadow: 0 10px 32px rgba(0,255,255,0.18), 0 0 0 1px rgba(0,255,255,0.32) inset;
  }}
  .today-card.today-empty {{
    opacity: 0.55; cursor: default; pointer-events: none;
    border-color: rgba(255,255,255,0.08);
  }}
  .today-card.today-empty::before {{ background: rgba(255,255,255,0.12); }}
  .tc-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .tc-emoji {{ font-size: 22px; line-height: 1; }}
  .tc-shift {{
    font-family: var(--sans); font-size: 16px; font-weight: 700;
    color: var(--text-100); letter-spacing: 0.3px;
  }}
  .tc-meta {{
    font-family: var(--mono); font-size: 11px; color: var(--text-70);
    letter-spacing: 0.3px; line-height: 1.5;
  }}
  .tc-meta b {{ color: var(--accent-hi); font-weight: 700; }}
  .tc-warn {{
    color: #ef4444; margin-left: 6px; font-weight: 600;
  }}
  .tc-status {{
    font-family: var(--mono); font-size: 12px; color: var(--text-50);
    letter-spacing: 0.4px;
  }}
  .tc-cta {{
    margin-top: 12px; font-family: var(--mono); font-size: 11px;
    text-transform: uppercase; letter-spacing: 1.4px;
    color: var(--accent-hi); text-shadow: 0 0 8px rgba(0,255,255,0.4);
  }}

  /* ─── KPI grid ─── */
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 36px; }}
  .kpi {{
    position: relative;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; padding: 16px 18px;
    transition: border-color 0.2s, transform 0.2s;
  }}
  .kpi::before {{
    content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 2px;
    background: linear-gradient(180deg, var(--accent), var(--accent-hi));
    opacity: 0.6; border-radius: 6px 0 0 6px;
  }}
  .kpi:hover {{ border-color: var(--border-strong); transform: translateY(-1px); }}
  .kpi-label {{
    font-size: 10px; color: var(--text-50); letter-spacing: 1.2px;
    text-transform: uppercase; margin-bottom: 8px; font-weight: 600;
  }}
  .kpi-val {{
    font-family: var(--mono); font-size: 28px; font-weight: 700;
    color: var(--text-100); line-height: 1.1; letter-spacing: -0.5px;
  }}
  .kpi-val.hi {{ color: var(--accent-hi); text-shadow: 0 0 12px var(--accent-glow); }}
  .kpi-sub {{ font-size: 11px; color: var(--text-70); margin-top: 6px; font-weight: 500; }}

  /* 可展开 KPI 卡（重要事件） */
  details.kpi-expandable {{ cursor: pointer; }}
  details.kpi-expandable > summary {{
    list-style: none; cursor: pointer;
  }}
  details.kpi-expandable > summary::-webkit-details-marker {{ display: none; }}
  details.kpi-expandable .kpi-toggle {{
    font-size: 10px; color: var(--text-50); margin-left: 4px;
    transition: transform 0.15s;
    display: inline-block;
  }}
  details.kpi-expandable[open] .kpi-toggle {{ transform: rotate(180deg); }}
  .kpi-list {{
    list-style: none; padding: 12px 0 0 0; margin: 12px 0 0 0;
    border-top: 1px solid var(--border);
  }}
  .kpi-list li {{ margin: 0; padding: 0; }}
  .kpi-list li + li {{ margin-top: 6px; }}
  .kpi-list a {{
    display: block; padding: 8px 10px; border-radius: 4px;
    color: var(--text-100); text-decoration: none;
    background: rgba(255,255,255,0.02);
    transition: background 0.15s, transform 0.1s;
    font-size: 12px; line-height: 1.4;
  }}
  .kpi-list a:hover {{ background: rgba(255,255,255,0.06); transform: translateX(2px); }}
  .kpi-list-star {{ margin-right: 6px; }}
  .kpi-list-title {{ font-weight: 500; color: var(--text-100); }}
  .kpi-list-src {{
    display: block; font-size: 10px; color: var(--text-50);
    margin-top: 3px; margin-left: 18px;
  }}

  /* ─── KPI 班次 Tab 切换 ─── */
  .kpi-tabs {{
    display: flex; gap: 0; margin-bottom: 16px;
    border-bottom: 1px solid rgba(0,255,255,0.14);
  }}
  .kpi-tab {{
    background: none; border: 0; cursor: pointer;
    padding: 10px 20px 12px;
    display: flex; align-items: center; gap: 8px;
    font-family: var(--sans); font-size: 13px; font-weight: 600;
    color: var(--text-50); letter-spacing: 0.3px;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: color 0.15s, border-color 0.15s;
  }}
  .kpi-tab:hover {{ color: var(--text-100); }}
  .kpi-tab.active {{
    color: var(--accent-hi);
    border-bottom-color: var(--accent-hi);
    text-shadow: 0 0 8px var(--accent-glow);
  }}
  .kt-emoji {{ font-size: 16px; line-height: 1; }}
  .kt-name {{ font-size: 13px; }}
  .kt-meta {{
    font-family: var(--mono); font-size: 10px; font-weight: 500;
    color: var(--text-30); letter-spacing: 0.4px;
    text-shadow: none; margin-left: 4px;
  }}
  .kpi-tab.active .kt-meta {{ color: var(--text-50); }}
  .kpi-panels {{ margin-bottom: 32px; }}
  .kpi-panel {{ display: none; }}
  .kpi-panel.active {{ display: block; }}
  .kpi-panel-meta {{
    font-family: var(--mono); font-size: 11px; color: var(--text-50);
    letter-spacing: 0.4px; margin-bottom: 12px;
  }}
  .kpi-panel-meta b {{ color: var(--accent-hi); font-weight: 700; }}
  .kpi-val-sm {{ font-size: 22px !important; }}
  .kpi-empty {{
    color: var(--text-50); font-family: var(--mono); font-size: 12px;
    padding: 24px; text-align: center;
    border: 1px dashed rgba(0,255,255,0.16); border-radius: 6px;
  }}

  /* ─── Tier 3 状态条（运维降级到一行） ─── */
  .status-bar {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 0;
    margin-top: 14px; padding: 10px 14px;
    background: rgba(0,102,255,0.04);
    border: 1px solid rgba(0,255,255,0.08);
    border-radius: 6px;
    font-family: var(--mono); font-size: 11px; letter-spacing: 0.4px;
  }}
  .sb-label {{
    color: var(--text-50); font-weight: 700;
    text-transform: uppercase; margin-right: 12px;
    padding-right: 10px; border-right: 1px solid rgba(0,255,255,0.12);
  }}
  .sb-ok {{ color: var(--text-100); }}
  .sb-warn {{ color: var(--amber, #fbbf24); }}
  .sb-fail {{ color: var(--red, #ef4444); }}
  .sb-mut {{ color: var(--text-50); }}
  .sb-sep {{ margin: 0 8px; }}
  .status-bar em {{ font-style: normal; color: var(--text-30); }}

  /* ─── Tier 2 知识库 section 的 meta 行 ─── */
  .kb-title {{ margin-top: 28px; }}
  .kb-meta {{
    font-family: var(--mono); font-size: 11px; color: var(--text-50);
    letter-spacing: 0.3px; margin: -6px 0 12px;
  }}

  /* ─── Section titles ─── */
  .section-title {{
    font-size: 13px; font-weight: 700; color: var(--text-100);
    margin: 36px 0 6px; letter-spacing: 0.5px;
    padding-left: 12px; position: relative;
  }}
  .section-title::before {{
    content: ''; position: absolute; left: 0; top: 4px; bottom: 4px; width: 3px;
    background: linear-gradient(180deg, var(--accent), var(--accent-hi));
    box-shadow: 0 0 8px var(--accent-glow); border-radius: 2px;
  }}

  /* 本周重要事件 */
  .wi-block {{ margin: 8px 0 28px; }}
  .wi-date {{
    font-size: 11px; color: var(--text-50); letter-spacing: 1px;
    margin: 14px 0 6px; padding-left: 2px; font-weight: 600;
    text-transform: uppercase;
  }}
  .wi-row {{ margin: 0 0 4px; }}
  .wi-row a {{
    display: block; padding: 10px 12px; border-radius: 5px;
    background: rgba(255,255,255,0.025);
    color: var(--text-100); text-decoration: none;
    transition: background 0.15s, transform 0.1s;
    font-size: 13px; line-height: 1.45;
    border-left: 2px solid var(--accent);
  }}
  .wi-row a:hover {{ background: rgba(120,200,255,0.08); transform: translateX(2px); }}
  .wi-star {{ margin-right: 6px; }}
  .wi-time {{
    display: inline-block; margin-right: 10px;
    font-family: var(--mono);
    font-size: 11px; color: var(--accent);
    min-width: 36px;
  }}
  .wi-title {{ font-weight: 500; }}
  .wi-src {{
    display: inline-block; margin-left: 10px;
    font-size: 11px; color: var(--text-50);
  }}

  /* 本周焦点实体 */
  .ent-chips {{
    display: flex; flex-wrap: wrap; gap: 8px;
    margin: 12px 0 32px; align-items: baseline;
  }}
  .ent-chip {{
    padding: 4px 10px; border-radius: 14px;
    background: rgba(120,200,255,0.08);
    border: 1px solid rgba(120,200,255,0.18);
    color: var(--text-100); font-weight: 500;
    transition: background 0.15s;
  }}
  .ent-chip:hover {{ background: rgba(120,200,255,0.16); }}
  .ent-count {{
    margin-left: 5px; font-size: 0.78em;
    color: var(--text-50); font-weight: 400;
  }}

  /* 历史跑记录默认收起 */
  details.run-history {{ margin: 32px 0 8px; }}
  details.run-history > summary {{
    list-style: none; cursor: pointer;
    font-size: 13px; font-weight: 700; color: var(--text-100);
    padding: 8px 12px 8px 14px; letter-spacing: 0.5px;
    border-left: 3px solid var(--accent);
    background: var(--bg-card); border-radius: 0 4px 4px 0;
    transition: background 0.15s;
  }}
  details.run-history > summary::-webkit-details-marker {{ display: none; }}
  details.run-history > summary:hover {{ background: var(--bg-card-hi, rgba(255,255,255,0.04)); }}
  details.run-history .hist-toggle {{
    float: right; color: var(--text-50); font-size: 11px;
    transition: transform 0.15s; display: inline-block;
  }}
  details.run-history[open] .hist-toggle {{ transform: rotate(180deg); }}
  .section-hint {{ font-size: 11px; color: var(--text-50); margin-bottom: 14px; padding-left: 12px; }}

  /* ─── Tables ─── */
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  thead {{ background: linear-gradient(180deg, rgba(0,102,255,0.06), transparent); }}
  th {{
    text-align: left; padding: 10px 12px;
    color: var(--accent-hi); font-weight: 700;
    letter-spacing: 0.8px; text-transform: uppercase; font-size: 10px;
    border-bottom: 1px solid var(--border-strong);
    font-family: var(--mono);
  }}
  td {{
    padding: 9px 12px; border-bottom: 1px solid var(--border);
    color: var(--text-100); vertical-align: middle;
  }}
  tbody tr {{ transition: background 0.12s; }}
  tbody tr:hover td {{ background: var(--bg-card-hover); }}
  tbody tr:nth-child(even) td {{ background: rgba(0,102,255,0.015); }}
  td a {{ color: var(--accent-hi); text-decoration: none; transition: text-shadow 0.15s; }}
  td a:hover {{ text-shadow: 0 0 6px var(--accent-glow); }}
  /* 数字类列右对齐（按位次启发：3-7 列通常是数字） */
  table td:nth-child(n+3):nth-child(-n+7),
  table th:nth-child(n+3):nth-child(-n+7) {{ text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }}

  /* ─── 状态色 ─── */
  .ok {{ color: var(--green); }}
  .fail {{ color: var(--red); }}
  .warn {{ color: var(--amber); }}

  /* ─── 滚动条（可选小细节，提升专业感） ─── */
  ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{ background: rgba(0,102,255,0.18); border-radius: 5px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: rgba(0,102,255,0.28); }}

  @media (max-width: 640px) {{
    body {{ padding: 20px 12px; }}
    .kpis {{ grid-template-columns: repeat(2, 1fr); }}
    .today-cards {{ grid-template-columns: 1fr; }}
    h1 {{ font-size: 22px; }}
    .kpi-val {{ font-size: 22px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>AI 早报 · 跑步仪表盘</h1>
    <span class="hdr-tag">ANALYTICS · v1</span>
  </div>
  <div class="subtitle">
    最近 {len(runs)} 次跑 · 生成于 {generated_at} ·
    <a href="../index.html">← 返回早报</a>
  </div>

  {today_html}

{kpi_blocks_html}

{week_important_html}

{week_entities_html}

  <details class="run-history">
    <summary>📜 历史跑记录（最近 14 次）<span class="hist-toggle">▾</span></summary>
    <div style="font-size:11px;color:var(--text-50);margin:8px 0 10px">
      点"时间"列跳到该次归档；条目=0 或 部署失败的行会标红
    </div>
    <table>
      <thead><tr>
        <th>时间</th><th>班</th><th>条目</th><th>LLM</th><th>多源</th>
        <th>用时(分)</th><th>源 OK/死</th><th>部署</th>
      </tr></thead>
      <tbody>
{table_body}
      </tbody>
    </table>
  </details>
</div>

<script>
  // ── KPI 班次 Tab 切换 ──
  document.querySelectorAll('.kpi-tab').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var target = btn.getAttribute('data-shift');
      document.querySelectorAll('.kpi-tab').forEach(function(b) {{
        b.classList.toggle('active', b.getAttribute('data-shift') === target);
      }});
      document.querySelectorAll('.kpi-panel').forEach(function(p) {{
        p.classList.toggle('active', p.getAttribute('data-shift') === target);
      }});
    }});
  }});
</script>
</body>
</html>
'''

    out_path.write_text(html, encoding='utf-8')
    size_kb = round(len(html) / 1024, 1)
    print(f'📊 dashboard 已生成: {out_path} ({size_kb} KB, {len(runs)} 次记录)')


if __name__ == '__main__':
    main()
