#!/usr/bin/env python3
"""为 dispatch-tg.yml workflow 构建预设消息内容。

读 stdin / 环境变量,把消息写入 stdout。

usage:
  PRESET=dashboard_snapshot BRIEFING_URL=https://... build_tg_message.py
  PRESET=health_ping build_tg_message.py
  PRESET=none MSG="任意消息" build_tg_message.py
"""
import datetime
import json
import os
import sys
import urllib.request


def fetch_stats(base_url: str) -> dict:
    """从部署的 GH Pages 拉 stats.json。"""
    cache_buster = int(datetime.datetime.now().timestamp())
    url = f"{base_url.rstrip('/')}/stats.json?_={cache_buster}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"WARN: failed to fetch stats: {e}", file=sys.stderr)
        return {}


def build_dashboard_snapshot() -> str:
    base = os.environ.get('BRIEFING_URL', '').rstrip('/')
    if not base:
        return "<b>⚠️ BRIEFING_URL 未配置</b>"

    stats = fetch_stats(base)
    if not stats:
        return f"<b>⚠️ stats.json 拉取失败</b>\n\n<a href=\"{base}/archive/dashboard.html\">📈 仪表盘</a>"

    ev = stats.get('event_db_stats', {})
    kept = stats.get('article_count', '?')
    llm_pct = int(round(stats.get('llm_coverage') or 0) * 100)
    multi = stats.get('multi_source_count', 0)
    imp = stats.get('important_count', 0)
    official = stats.get('official_count', 0)

    gen = stats.get('generated_at', '')
    try:
        gen_local = datetime.datetime.fromisoformat(
            gen.replace('Z', '+00:00')
        ).astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        gen_str = gen_local.strftime('%m-%d %H:%M')
    except Exception:
        gen_str = '?'

    lines = [
        f"<b>📊 跑步仪表盘 · {gen_str}</b>",
        "",
        "<b>本班次产出:</b>",
        f"· 收录 <b>{kept}</b> 条 AI 资讯",
        f"· 🧠 LLM 深度分析 <b>{llm_pct}%</b>",
        f"· ⭐ 重要事件 <b>{imp}</b> 条 (importance ≥ 4)",
        f"· 🔗 多源交叉确认 <b>{multi}</b> 条",
        f"· 🏢 原厂直发 <b>{official}</b> 条",
        "",
        "<b>知识库累积:</b>",
        f"· 总事件 <b>{ev.get('total_events', '?')}</b> 个",
        f"· 总文章 <b>{ev.get('total_articles', '?')}</b> 篇",
        "",
        f'<a href="{base}/archive/dashboard.html">📈 打开完整仪表盘 →</a>',
        f'<a href="{base}/">📖 当前最新早报</a>',
    ]
    return "\n".join(lines)


def build_health_ping() -> str:
    now = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime('%Y-%m-%d %H:%M:%S CST')
    return f"<b>🟢 系统健康检查</b>\n{now}\n来自 GitHub Actions runner"


def main():
    preset = os.environ.get('PRESET', 'none').strip()
    if preset == 'dashboard_snapshot':
        body = build_dashboard_snapshot()
    elif preset == 'health_ping':
        body = build_health_ping()
    else:
        body = os.environ.get('MSG', '')

    sys.stdout.write(body)


if __name__ == '__main__':
    main()
