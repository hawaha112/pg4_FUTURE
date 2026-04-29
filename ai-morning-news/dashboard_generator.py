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
    for r in reversed(runs[-30:]):  # 从 20 → 30 更多历史
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
    def _kpi_card(label: str, val_html: str, sub: str, extra_cls: str = '') -> str:
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

        cards = (
            _kpi_card('重要事件',
                      str(important),
                      f'importance ≥ 4 · 占总条目 {int(important_pct)}%',
                      extra_cls='hi' if important > 0 else '')
            + _kpi_card('多源覆盖',
                        f'{multi}<span style="font-size:14px;color:var(--text-50)"> / {kept}</span>',
                        f'跨源交叉确认 · {multi_pct}%',
                        extra_cls='hi' if multi > 0 else '')
            + _kpi_card('原厂直发',
                        str(official),
                        '官方账号 / 公司发布的原始消息',
                        extra_cls='hi' if official > 0 else '')
            + _kpi_card('深度分析',
                        f'{int(depth_pct)}<span style="font-size:18px;color:var(--text-50)">%</span>',
                        f'{depth} / {kept} · 含 background + detailed_content')
            + _kpi_card('覆盖实体',
                        str(entities),
                        '本期出现的去重公司 / 项目 / 人物')
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

    # ── Tier 2：知识库累积（全库快照，跨班次/历史）──
    kb_section_html = _render_kb_section(script_dir)

    # ── 错误聚合区块 ──
    errors = _collect_recent_errors(script_dir, max_errors=15)
    if errors:
        err_rows = []
        for e in errors:
            color = 'var(--red)' if e['level'] == 'E' else 'var(--amber)'
            err_rows.append(
                f'<tr>'
                f'<td style="color:{color};font-weight:600">{e["level"]} ×{e["count"]}</td>'
                f'<td>{escape(e["module"])}</td>'
                f'<td>{escape(e["last_ts"])}</td>'
                f'<td style="font-family:monospace;font-size:11px">{escape(e["message"][:120])}</td>'
                f'</tr>'
            )
        errors_section_html = f'''
  <div class="section-title">⚠️ 最近错误事件 (top {len(errors)})</div>
  <div style="font-size:11px;color:var(--text-50);margin-bottom:10px">
    从 daily_run.log 聚合最近 ~500KB 内的 WARN/ERROR 行；按出现频次降序
  </div>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>级别×次数</th><th>模块</th><th>最近时间</th><th>消息</th></tr></thead>
    <tbody>{chr(10).join(err_rows)}</tbody>
  </table>
  </div>
'''
    else:
        errors_section_html = '<div style="color:var(--green);padding:10px 0">✓ 近期日志无 WARN/ERROR</div>'

    # ── 源质量区块 ──
    sources = _collect_source_stats(script_dir)

    def _tier_color(t):
        return {0: 'var(--green)', 1: 'var(--accent)', 2: 'var(--text-70)'}.get(t, 'var(--text-70)')

    def _tier_label(t):
        return {0: 'T0 官方', 1: 'T1 研究', 2: 'T2 媒体'}.get(t, f'T{t}')

    def _health_badge(tag):
        colors = {
            'ok': ('●', 'var(--green)', 'OK'),
            'failing': ('●', 'var(--amber)', '告警'),
            'dead': ('●', 'var(--red)', '死源'),
            'disabled': ('○', 'var(--text-50)', '禁用'),
        }
        m, c, lbl = colors.get(tag, ('●', 'var(--text-50)', '?'))
        return f'<span style="color:{c}">{m} {lbl}</span>'

    def _fmt_last(s):
        if not s:
            return '—'
        try:
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            return dt.astimezone().strftime('%m-%d %H:%M')
        except (ValueError, TypeError):
            return s[:16].replace('T', ' ')

    sources_rows = []
    for s in sources:
        url = s.get('url', '') or ''
        # youtube:// URL 转成可点击的 channel 链接
        if url.startswith('youtube://'):
            cid = url[len('youtube://'):]
            url_display = f'https://www.youtube.com/channel/{cid}'
        else:
            url_display = url
        url_cell = (
            f'<a href="{escape(url_display)}" target="_blank" rel="noopener">🔗 源</a>'
            if url_display.startswith('http') else '—'
        )
        sources_rows.append(
            f'<tr>'
            f'<td>{escape(s["name"])}</td>'
            f'<td style="color:{_tier_color(s["tier"])}">{_tier_label(s["tier"])}</td>'
            f'<td>{escape(s["lang"])}</td>'
            f'<td>{url_cell}</td>'
            f'<td><b>{s["w7"]}</b></td>'
            f'<td>{s["w7_hi"]}</td>'
            f'<td>{s["avg_imp_w7"] or "—"}</td>'
            f'<td>{s["total"]}</td>'
            f'<td>{_fmt_last(s["last_collected"])}</td>'
            f'<td>{_health_badge(s["health_tag"])}</td>'
            f'</tr>'
        )

    # 源汇总 KPI
    enabled_total = sum(1 for s in sources if s['enabled'])
    active_w7 = sum(1 for s in sources if s['enabled'] and s['w7'] > 0)
    dead_count = sum(1 for s in sources if s['health_tag'] == 'dead')
    silent_w7 = sum(1 for s in sources if s['enabled'] and s['w7'] == 0)

    sources_section_html = f'''
  <div class="section-title">📡 数据源质量（{len(sources)} 源：{enabled_total} 启用 / {active_w7} 近 7d 有贡献 / {silent_w7} 静默 / {dead_count} 死）</div>
  <div style="font-size:11px;color:var(--text-50);margin-bottom:10px">
    按"近 7 天贡献数"降序排列；点"🔗 源"直接打开原始 RSS/Feed 监控源头
  </div>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>源名</th><th>Tier</th><th>Lang</th><th>URL</th>
      <th>近 7d</th><th>近 7d ★≥3</th><th>近 7d 均 ★</th><th>总数</th>
      <th>最近采集</th><th>健康</th>
    </tr></thead>
    <tbody>
{chr(10).join(sources_rows)}
    </tbody>
  </table>
  </div>
'''

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

{kb_section_html}

  <div class="section-title">历史跑记录（最近 30 次）</div>
  <div style="font-size:11px;color:var(--text-50);margin-bottom:10px">
    点击"时间"列可跳转到该次的归档早报页面；异常行（条目=0 或 部署失败）标红
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

{errors_section_html}

{sources_section_html}
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
