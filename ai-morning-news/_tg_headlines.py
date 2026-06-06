#!/usr/bin/env python3
"""读 output/stats.json, 输出 TG 早报消息用的"头条"行(内容优先改造 P0)。

用法: _tg_headlines.py <stats.json 路径> [最多几条=5]
输出(stdout): 每条头条两行 —— "图标 <b>标题</b>" + 下一行"一句话概括"(发生了什么);
            第一条带 ⭐, 其余带 ·; 取 importance 最高的 N 条。让 TG 消息能独立读完大概,
            不必点进页面(对标 TLDR/Rundown 的"5 分钟扫读")。
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

    limit = 5
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
        if len(title) > 46:
            title = title[:45] + '…'
        # 一句话概括(发生了什么) —— 让读者在 TG 里直接读懂大概
        gist = (e.get('summary') or '').strip()
        if len(gist) > 52:
            gist = gist[:51] + '…'
        bullet = '⭐ ' if i == 0 else '· '
        line = bullet + '<b>' + html.escape(title) + '</b>'  # 标题加粗、便于扫
        if gist:
            line += '\n' + html.escape(gist)
        lines.append(line)

    # 条目之间空一行, 扫读更清晰
    sys.stdout.write('\n\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
