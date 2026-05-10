#!/usr/bin/env python3
"""_run_summary.py — 生成本次 RUN_SUMMARY 行 + 追加到 run_health.jsonl

从 run_daily.sh 的内联 heredoc 抽出来 — bash 双引号 + 多行 Python +
`{}` `'` 之间的引用嵌套在 GH Actions runner 上偶发静默失败（stdout 空,
没 stderr，没 traceback）。本地能跑通，所以怀疑是 runner 环境的 shell
词法/编码差异。抽成独立脚本最干净。

用法:
    python3 _run_summary.py STATS_PATH HEALTH_PATH CONFIG_PATH \\
            SHIFT DURATION_SEC DEPLOY_OK_FLAG LLM_AVAILABLE_FLAG

输出: stdout 一行 "RUN_SUMMARY {json}"
副作用: 追加同一行 json 到 run_health.jsonl
"""

import json
import os
import sys
from datetime import datetime, timezone


def _load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    if len(sys.argv) < 8:
        print("RUN_SUMMARY {}", flush=True)
        return 1

    stats_path = sys.argv[1]
    health_path = sys.argv[2]
    config_path = sys.argv[3]
    shift = sys.argv[4]
    duration_sec = int(sys.argv[5] or 0)
    deploy_ok = sys.argv[6] == 'true'
    llm_available = sys.argv[7] == 'true'

    stats = _load_json(stats_path)
    health = _load_json(health_path)
    cfg = _load_json(config_path)

    # 收集 enabled=false 的源 — 让 dead/failing 计数排除已显式禁用的
    disabled = set()
    for lst in cfg.get('sources', {}).values():
        if isinstance(lst, list):
            for s in lst:
                if not s.get('enabled', True) or s.get('disabled', False):
                    nm = s.get('name')
                    if nm:
                        disabled.add(nm)

    healthy = sum(
        1 for n, v in health.items()
        if v.get('status') == 'ok' and n not in disabled
    )
    failing = sum(
        1 for n, v in health.items()
        if v.get('consecutive_failures', 0) >= 3 and n not in disabled
    )
    dead = [
        n for n, v in health.items()
        if v.get('consecutive_failures', 0) >= 10 and n not in disabled
    ][:5]

    summary = {
        'run_id': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'shift': shift,
        'duration_sec': duration_sec,
        'kept': stats.get('article_count', 0),
        'llm_coverage': stats.get('llm_coverage'),
        'llm_count': stats.get('llm_count'),
        'multi_source_count': stats.get('multi_source_count'),
        'important_count': stats.get('important_count'),
        'important_events': stats.get('important_events', []),
        'official_count': stats.get('official_count'),
        'depth_count': stats.get('depth_count'),
        'entity_count': stats.get('entity_count'),
        'sources_healthy': healthy,
        'sources_failing': failing,
        'dead_sources': dead,
        'deploy_ok': deploy_ok,
        'llm_available': llm_available,
    }
    line = json.dumps(summary, ensure_ascii=False)
    print('RUN_SUMMARY ' + line, flush=True)

    # 追加到 run_health.jsonl，dashboard / 周报消费
    health_log = os.path.join(os.path.dirname(stats_path), 'run_health.jsonl')
    try:
        with open(health_log, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass

    return 0


if __name__ == '__main__':
    sys.exit(main())
