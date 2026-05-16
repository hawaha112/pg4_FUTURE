"""
daily_ops_check.py — 每日运维健康检查 (架构师视角)

每天跑一次, 检查可能"静默坏掉"的几件事, 命中异常立即推 TG.

检查项:
1. 死源激增 (新增 >= 2 个死源 → 告警)
2. 信源告警激增 (>= 5 个 source 连失 >= 3 次)
3. briefing-state 分支最后一次 push 时间 (>= 36h 没更新 = 流水线可能死了)
4. GitHub PAT 过期天数 (< 14 天告警 — 通过 GitHub API 查)

设计原则:
- 只在"有问题"时推送, 没问题静默 (避免变成噪音)
- 单条 TG, 多个问题合并
- < 30 秒跑完, 不消耗 LLM token
"""

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from logger import get_logger

log = get_logger('daily_ops')

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / 'events.db'

TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '')
GITHUB_PAT = os.environ.get('GITHUB_PAT', '')  # 可选, 用于检查 PAT 过期

# 告警阈值
DEAD_SOURCE_ALERT = 2          # 新增 >=2 死源告警
FAILING_SOURCE_ALERT = 5       # >=5 个连失 >=3 次源
STATE_BRANCH_STALE_HOURS = 36  # briefing-state >= 36h 没 push 告警
PAT_EXPIRY_WARN_DAYS = 14      # PAT 剩余 < 14 天告警


def _send_tg(html_text: str) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("TG 凭据缺失, 跳过推送")
        return False
    payload = json.dumps({
        'chat_id': TG_CHAT_ID,
        'text': html_text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage',
        data=payload, method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get('ok', False)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.warning("TG 推送失败: %s", e)
        return False


def check_source_health() -> list[str]:
    """检查信源健康. 返回问题描述列表 (空 = 健康)."""
    issues = []
    if not DB_PATH.exists():
        issues.append(f"❌ events.db 不存在: {DB_PATH}")
        return issues
    try:
        with sqlite3.connect(str(DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            try:
                dead = con.execute(
                    "SELECT name, consecutive_failures FROM source_health "
                    "WHERE consecutive_failures >= 10 ORDER BY consecutive_failures DESC"
                ).fetchall()
                failing = con.execute(
                    "SELECT name, consecutive_failures FROM source_health "
                    "WHERE consecutive_failures >= 3 AND consecutive_failures < 10 "
                    "ORDER BY consecutive_failures DESC"
                ).fetchall()
            except sqlite3.OperationalError:
                issues.append("⚠️ source_health 表不存在 (运行 state_check.py migrate)")
                return issues

        if len(dead) >= DEAD_SOURCE_ALERT:
            names = ", ".join(f"{r['name']} ({r['consecutive_failures']}次)" for r in dead[:5])
            issues.append(f"💀 <b>{len(dead)} 个死源</b> (≥10 连失): {names}")
        if len(failing) >= FAILING_SOURCE_ALERT:
            names = ", ".join(f"{r['name']} ({r['consecutive_failures']}次)" for r in failing[:5])
            issues.append(f"⚠️ <b>{len(failing)} 个告警源</b> (3-9 连失): {names}")
    except sqlite3.Error as e:
        issues.append(f"❌ DB 读取异常: {e}")
    return issues


def check_pat_expiry() -> list[str]:
    """检查 GitHub PAT 过期天数 (需要 GITHUB_PAT env)."""
    if not GITHUB_PAT:
        return []  # 没传 token 就跳过 (这是 fine-grained PAT, 自检需要)
    try:
        req = urllib.request.Request(
            'https://api.github.com/user',
            headers={
                'Authorization': f'Bearer {GITHUB_PAT}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            # PAT 过期日期在 GitHub-Authentication-Token-Expiration 响应头
            exp_header = resp.headers.get('GitHub-Authentication-Token-Expiration', '')
            if not exp_header:
                return []  # 老 token 没有过期头
            # 格式: "2026-06-14 14:54:33 UTC"
            exp_str = exp_header.split('UTC')[0].strip()
            exp_dt = datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            days_left = (exp_dt - datetime.now(timezone.utc)).days
            if days_left <= PAT_EXPIRY_WARN_DAYS:
                return [f"🔑 <b>PAT 剩 {days_left} 天过期</b> ({exp_dt.strftime('%Y-%m-%d')}) — 须 <a href='https://github.com/settings/personal-access-tokens'>续期</a>"]
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        return [f"⚠️ PAT 自检失败: {str(e)[:60]}"]
    return []


def check_state_branch_freshness() -> list[str]:
    """检查 briefing-state 分支最后一次 push 时间."""
    try:
        req = urllib.request.Request(
            'https://api.github.com/repos/hawaha112/pg4_FUTURE/branches/briefing-state',
            headers={'Accept': 'application/vnd.github+json'},
        )
        if GITHUB_PAT:
            req.add_header('Authorization', f'Bearer {GITHUB_PAT}')
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            commit_date = data.get('commit', {}).get('commit', {}).get('author', {}).get('date')
            if not commit_date:
                return []
            commit_dt = datetime.fromisoformat(commit_date.replace('Z', '+00:00'))
            hours_old = (datetime.now(timezone.utc) - commit_dt).total_seconds() / 3600
            if hours_old >= STATE_BRANCH_STALE_HOURS:
                return [f"📦 <b>briefing-state {hours_old:.0f}h 没更新</b> (上次: {commit_dt.strftime('%m-%d %H:%M')} UTC) — 流水线可能挂了"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
        return [f"⚠️ 状态分支自检失败: {str(e)[:60]}"]
    return []


def main():
    log.info("=" * 50)
    log.info("📋 每日运维健康检查启动")
    log.info("=" * 50)

    all_issues = []
    all_issues.extend(check_source_health())
    all_issues.extend(check_pat_expiry())
    all_issues.extend(check_state_branch_freshness())

    if not all_issues:
        log.info("✅ 所有检查通过, 系统健康, 静默退出")
        return 0

    log.warning("🚨 发现 %d 个问题, 准备推送 TG", len(all_issues))
    for issue in all_issues:
        log.warning("  %s", issue)

    ts = datetime.now(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
    msg = (
        f"🛠 <b>每日运维健康检查 · {ts}</b>\n\n"
        + "\n".join(f"• {iss}" for iss in all_issues)
        + "\n\n<i>(本消息只在异常时推送, 健康时静默)</i>"
    )
    if _send_tg(msg):
        log.info("✓ TG 告警已发送")
    return 0


if __name__ == '__main__':
    sys.exit(main())
