#!/usr/bin/env python3
"""读 output/stats.json, 输出 TG 早报消息用的"头条"行(内容优先改造 P0)。

用法: _tg_headlines.py <stats.json 路径> [最多几条=3]
输出(stdout): 每行一条头条; 第一条带 ⭐, 其余带 ·; 取 importance 最高的 N 条。
            无 important_events 时输出空(调用方据此省略头条块, 只留"共 N 条 + 链接")。

抽成独立脚本而非 run_daily.sh 内联 —— 与 _run_summary.py 同理, 避免 bash 多行
heredoc + python 在云端 runner 上偶发静默失败(stdout 空、无 stderr)。
"""
import html
import json
import sys


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    try:
        with open(sys.argv[1], encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return 0  # stats.json 缺失/损坏 → 输出空, 消息退化为"共 N 条 + 链接"

    limit = 3
    if len(sys.argv) > 2:
        try:
            limit = max(1, int(sys.argv[2]))
        except ValueError:
            pass

    events = data.get('important_events') or []
    if not isinstance(events, list):
        return 0
    events = sorted(events, key=lambda e: -(e.get('importance') or 0))[:limit]

    lines = []
    for i, e in enumerate(events):
        title = (e.get('title') or '').strip()
        if not title:
            continue
        if len(title) > 60:
            title = title[:59] + '…'
        title = html.escape(title)  # TG parse_mode=HTML: 转义 & < >
        lines.append(('⭐ ' if i == 0 else '· ') + title)

    sys.stdout.write('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
