#!/usr/bin/env python3
"""
state_check.py — 应用状态健康快查 (架构师级运维工具)

用法:
    python state_check.py            # 默认: 全状态摘要
    python state_check.py source     # 详细: source_health 表
    python state_check.py breaking   # 详细: pushed_breaking 表
    python state_check.py migrate    # 一次性把 JSON → DB

设计目标:
- 任何时候 SSH 登录 / GH Actions runner, 一条命令看全应用状态健康
- 不跑业务流水线, 纯读 events.db, 几秒返回
- 输出便于人/机读 (terminal table + JSON option)
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / 'events.db'


def cmd_summary():
    """一屏总览 — 默认命令."""
    if not DB_PATH.exists():
        print(f"❌ events.db 不存在: {DB_PATH}")
        return 1

    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row

        # 主表统计
        ev_total = con.execute("SELECT COUNT(*) AS n FROM canonical_events").fetchone()['n']
        ev_24h = con.execute(
            "SELECT COUNT(*) AS n FROM canonical_events "
            "WHERE first_seen_at >= datetime('now', '-1 day')"
        ).fetchone()['n']
        ar_total = con.execute("SELECT COUNT(*) AS n FROM articles").fetchone()['n']

        print("══════════════════════════════════════════════════════")
        print("  📊 应用状态总览                                      ")
        print("══════════════════════════════════════════════════════")
        print(f"  events.db: {DB_PATH}")
        print(f"  大小: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
        print()
        print("  📦 主库:")
        print(f"     canonical_events: {ev_total:>6}  (24h 新增: {ev_24h})")
        print(f"     articles:         {ar_total:>6}")
        print()

        # source_health 表 (新)
        try:
            sh_total = con.execute("SELECT COUNT(*) AS n FROM source_health").fetchone()['n']
            sh_failing = con.execute(
                "SELECT COUNT(*) AS n FROM source_health "
                "WHERE consecutive_failures >= 3"
            ).fetchone()['n']
            sh_dead = con.execute(
                "SELECT COUNT(*) AS n FROM source_health "
                "WHERE consecutive_failures >= 10"
            ).fetchone()['n']
            print("  🩺 信源健康 (source_health):")
            print(f"     总计:    {sh_total:>6}")
            print(f"     告警:    {sh_failing:>6}  (>= 3 次连失)")
            print(f"     死源:    {sh_dead:>6}  (>= 10 次连失)")
        except sqlite3.OperationalError:
            print("  🩺 source_health: ⚠️ 表不存在 (运行 'state_check migrate')")
        print()

        # pushed_breaking 表 (新)
        try:
            pb_total = con.execute("SELECT COUNT(*) AS n FROM pushed_breaking").fetchone()['n']
            pb_24h = con.execute(
                "SELECT COUNT(*) AS n FROM pushed_breaking "
                "WHERE pushed_at >= datetime('now', '-1 day')"
            ).fetchone()['n']
            pb_7d = con.execute(
                "SELECT COUNT(*) AS n FROM pushed_breaking "
                "WHERE pushed_at >= datetime('now', '-7 day')"
            ).fetchone()['n']
            print("  🚨 突发推送去重 (pushed_breaking):")
            print(f"     总计:    {pb_total:>6}")
            print(f"     近 24h:  {pb_24h:>6}")
            print(f"     近 7d:   {pb_7d:>6}")
        except sqlite3.OperationalError:
            print("  🚨 pushed_breaking: ⚠️ 表不存在 (运行 'state_check migrate')")
        print()

        # 临时缓存文件 (未迁入 DB, 列出兼容性)
        print("  🟡 临时缓存 (按设计不迁入 DB):")
        for f in ['hot_signals.json', '.digest_cache.json']:
            p = SCRIPT_DIR / 'output' / f
            if p.exists():
                size_kb = p.stat().st_size / 1024
                print(f"     {f}: {size_kb:.1f} KB")
            else:
                print(f"     {f}: (不存在, 下次跑会重生成)")
        print()
        print("══════════════════════════════════════════════════════")
    return 0


def cmd_source():
    """详细 source_health 表."""
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT name, status, consecutive_failures, success_rate, "
                "last_response_time, total_runs, last_error "
                "FROM source_health "
                "ORDER BY consecutive_failures DESC, success_rate ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            print("⚠️ source_health 表不存在, 先跑 'state_check migrate'")
            return 1

    print(f"{'name':<30} {'status':<10} {'fail':>5} {'success%':>9} {'avg_ms':>7} {'runs':>6}  last_error")
    print("─" * 100)
    for r in rows:
        err = (r['last_error'] or '')[:40]
        print(f"{r['name']:<30} {r['status']:<10} {r['consecutive_failures']:>5} "
              f"{r['success_rate']:>8.1f}% {r['last_response_time']*1000:>6.0f}ms "
              f"{r['total_runs']:>6}  {err}")
    print(f"\n共 {len(rows)} 个源")
    return 0


def cmd_breaking():
    """详细 pushed_breaking 表 (近 24h)."""
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT sig_id, source, score, title, url, pushed_at "
                "FROM pushed_breaking "
                "WHERE pushed_at >= datetime('now', '-7 day') "
                "ORDER BY pushed_at DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            print("⚠️ pushed_breaking 表不存在, 先跑 'state_check migrate'")
            return 1

    print(f"{'pushed_at':<20} {'src':<8} {'score':>6}  title")
    print("─" * 100)
    for r in rows:
        title = (r['title'] or '')[:60]
        ts = (r['pushed_at'] or '')[:16]
        print(f"{ts:<20} {r['source']:<8} {r['score']:>6}  {title}")
    print(f"\n共 {len(rows)} 条 (近 7 天)")
    return 0


def cmd_migrate():
    """一次性 JSON → DB 迁移."""
    from state_store import migrate_from_json
    counts = migrate_from_json(
        db_path=DB_PATH,
        source_health_json=SCRIPT_DIR / 'source_health.json',
        pushed_breaking_json=SCRIPT_DIR / 'output' / 'pushed_breaking.json',
    )
    print(f"\n✓ 迁移完成: {counts}")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'summary'
    handlers = {
        'summary': cmd_summary,
        'source': cmd_source,
        'breaking': cmd_breaking,
        'migrate': cmd_migrate,
    }
    if cmd not in handlers:
        print(f"未知命令: {cmd}")
        print("可用: summary | source | breaking | migrate")
        return 2
    return handlers[cmd]()


if __name__ == '__main__':
    sys.exit(main())
