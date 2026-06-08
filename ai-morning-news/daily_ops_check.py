"""
daily_ops_check.py — 每日运维健康检查 (自治版)

设计哲学升级 (2026-05-16):
- 用户只看研究成果, 不看 raw 运维告警
- 检测到死源 → 触发 auto-fix-sources.yml workflow (而非推 TG)
- briefing-state stale → 自动 trigger 兜底 collector (而非推 TG)
- PAT 过期 → 自动开 GH Issue (用户在 GH 邮箱看, 不烦 TG)
- 真正不可逆的失败 → GH Issue + needs-human label

检查项:
1. 死源激增 → 触发 auto-fix-sources workflow (静默自愈)
2. briefing-state >= 36h 没 push → 触发 morning-briefing 紧急补跑 (兜底)
3. PAT 过期 < 14 天 → 自动开 GH Issue

不再做的事:
- ❌ 直接推 TG raw 告警 (用户嫌噪音, 应该已自动处理)
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


def _token_days_left(token: str, api_url: str):
    """用 token 请求 api_url, 从 GitHub-Authentication-Token-Expiration 响应头读剩余天数.
    返回 (days_left, 'YYYY-MM-DD') 或 None(无过期头/请求失败).
    注意: api_url 必须是该 token 有权访问的端点(否则 403 拿不到头) —— fine-grained PAT
    只给某仓 Contents/Metadata 时, 用该仓的 /repos/{owner}/{repo} 端点, 不能用 /user."""
    if not token:
        return None
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            # 格式: "2026-09-06 14:54:33 UTC"
            exp_header = resp.headers.get('GitHub-Authentication-Token-Expiration', '')
            if not exp_header:
                return None  # 无过期头 (Actions GITHUB_TOKEN / 经典无期限 token)
            exp_str = exp_header.split('UTC')[0].strip()
            exp_dt = datetime.strptime(exp_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            return (exp_dt - datetime.now(timezone.utc)).days, exp_dt.strftime('%Y-%m-%d')
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log.info("PAT 自检跳过 (%s): %s", api_url, str(e)[:80])
        return None


def check_pat_expiry() -> list[str]:
    """检查各 GitHub PAT 剩余天数, 临期(< PAT_EXPIRY_WARN_DAYS)告警.
    重点: DEPLOY_REPO_TOKEN(推部署仓的 PAT) —— 它过期会让早晚报渲染成功却部署失败
    (2026-06-08 实际踩过)。用部署仓 metadata 端点验证它(该 token 有 Metadata:Read)。
    旧的只查 GITHUB_TOKEN(Actions 自带、永不过期)等于白查, 故改成按 token 逐个查。"""
    out = []
    # (env 名, 显示名, 该 token 有权访问的验证端点)
    checks = [
        ('DEPLOY_REPO_TOKEN', '部署仓 PAT (DEPLOY_REPO_TOKEN)',
         'https://api.github.com/repos/hawaha112/ai-morning-briefing'),
        ('GITHUB_PAT', 'GITHUB_PAT', 'https://api.github.com/user'),
    ]
    seen = set()
    for env_name, label, api_url in checks:
        tok = os.environ.get(env_name, '')
        if not tok or tok in seen:
            continue
        seen.add(tok)
        res = _token_days_left(tok, api_url)
        if res is None:
            continue
        days_left, exp_date = res
        log.info("PAT 检查: %s 剩 %d 天 (到期 %s)", label, days_left, exp_date)
        if days_left <= PAT_EXPIRY_WARN_DAYS:
            out.append(
                f"🔑 <b>{label} 剩 {days_left} 天过期</b> ({exp_date}) — 须 "
                f"<a href='https://github.com/settings/personal-access-tokens'>续期</a> "
                f"并 <code>gh secret set {env_name}</code>"
            )
    return out


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


def _trigger_auto_fix_workflow() -> bool:
    """链式触发 auto-fix-sources.yml workflow (用 GITHUB_TOKEN 调 GH API)."""
    pat = os.environ.get('GITHUB_TOKEN', '')
    if not pat:
        log.warning("GITHUB_TOKEN 缺, 无法链式触发 auto-fix")
        return False
    payload = json.dumps({'ref': 'main'}).encode('utf-8')
    req = urllib.request.Request(
        'https://api.github.com/repos/hawaha112/pg4_FUTURE/actions/workflows/auto-fix-sources.yml/dispatches',
        data=payload, method='POST',
        headers={
            'Authorization': f'Bearer {pat}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 204
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log.warning("触发 auto-fix 失败: %s", e)
        return False


def _open_issue(title: str, body: str, labels: list[str]) -> bool:
    """用 gh CLI 开 issue (workflow 内 gh CLI 用 GH_TOKEN env 认证).

    label 不存在时降级到不带 label 重试 (避免因为 repo 没建过 label 就失败).
    """
    import subprocess
    base_args = [
        'gh', 'issue', 'create',
        '--repo', 'hawaha112/pg4_FUTURE',
        '--title', title, '--body', body,
    ]
    # 第 1 次: 带 label
    if labels:
        result = subprocess.run(
            base_args + ['--label', ','.join(labels)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return True
        log.info("带 label 开 issue 失败 (label 可能不存在), 降级重试无 label: %s",
                 (result.stderr or '')[:120])
    # 第 2 次: 无 label
    result = subprocess.run(base_args, capture_output=True, text=True)
    if result.returncode == 0:
        return True
    log.warning("开 issue 失败 (无 label 也失败): %s", (result.stderr or '')[:200])
    return False


def main():
    log.info("=" * 50)
    log.info("📋 每日运维自治检查启动")
    log.info("=" * 50)

    # 1. 死源 → 触发 auto-fix (不推 TG)
    source_issues = check_source_health()
    if source_issues:
        log.warning("检测到信源问题: %s", source_issues)
        log.info("🤖 链式触发 auto-fix-sources workflow...")
        if _trigger_auto_fix_workflow():
            log.info("✓ auto-fix 已触发, 静默 (它会自己处理 + 推 TG 维护小报)")
        else:
            log.warning("⚠️ auto-fix 触发失败, 退化为开 issue")
            _open_issue(
                title=f"🤖 自治维护: 触发 auto-fix 失败 ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})",
                body=f"daily_ops_check 检测到信源问题但无法触发 auto-fix-sources workflow.\n\n问题:\n" +
                     "\n".join(f"- {i}" for i in source_issues),
                labels=['auto-maintenance', 'needs-human'],
            )

    # 2. PAT 过期 → 直接开 issue (无法自愈, 必须人工)
    pat_issues = check_pat_expiry()
    if pat_issues:
        for iss in pat_issues:
            log.warning("PAT 问题: %s", iss)
        _open_issue(
            title=f"🔑 PAT 即将过期 ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})",
            body="daily_ops_check 检测到 GitHub PAT 即将过期, 必须人工续期:\n\n" +
                 "\n".join(f"- {i}" for i in pat_issues) +
                 "\n\n续期流程见 CLAUDE.md `Anthropic Routine 用的 GitHub PAT` 章节.",
            labels=['security', 'needs-human'],
        )

    # 3. briefing-state stale → 开 issue (流水线可能死了, 必须人工查)
    stale_issues = check_state_branch_freshness()
    if stale_issues:
        for iss in stale_issues:
            log.warning("流水线问题: %s", iss)
        _open_issue(
            title=f"📦 流水线 stale ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})",
            body="briefing-state 分支长时间未更新, 流水线可能挂了:\n\n" +
                 "\n".join(f"- {i}" for i in stale_issues) +
                 "\n\n建议: 1) 看最近一次 morning-briefing run 的失败日志; 2) 手动 trigger 一次 workflow.",
            labels=['critical', 'needs-human'],
        )

    if not (source_issues or pat_issues or stale_issues):
        log.info("✅ 所有检查通过, 系统健康, 完全静默")
    else:
        log.info("📋 检查完成 (问题已自治处理或开 issue, 不推 TG raw 告警)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
