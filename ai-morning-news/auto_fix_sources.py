"""
auto_fix_sources.py — 自治信源修复 (你不用看告警, 我自动修)

设计哲学:
- 用户只看研究成果, 不看运维告警
- 死源检测后, 由这个脚本自动尝试修复 (而非 TG 推告警给用户)
- 修好了 → 一句话维护小报
- 修不了 → 自动开 GitHub Issue (用户在 GH 邮箱看, 不烦 TG)

工作流程 (每个死源):
1. 从 events.db.source_health 拿到 N 个连失 >= 10 的死源
2. 按已知模式生成候选 URL (RSSHub mirror / official feed / well-known endpoints)
3. 用项目自己的 fetch_feed 实测每个候选
4. 找到第一个 work 的 → 改 config.json + commit + push
5. 候选全死 → 自动 disable 该源 (避免下次跑还浪费时间) + 标记 needs_human
6. 全部跑完 → 输出维护报告 (供 workflow 决定是否推 TG / 开 issue)

输出: maintenance_report.json (workflow 读它决定后续行动)
"""

import json
import os
import re
import subprocess
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from logger import get_logger

log = get_logger('auto_fix')

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / 'events.db'
CONFIG_PATH = SCRIPT_DIR / 'config.json'
REPORT_PATH = SCRIPT_DIR / 'output' / 'maintenance_report.json'

DEAD_THRESHOLD = 10  # consecutive_failures >= 此值视为死源


def _candidate_urls(source: dict) -> List[Tuple[str, str]]:
    """根据源现状生成候选 URL 列表 (按优先级排序).

    返回: [(候选 URL, 候选来源描述)]
    """
    name = source.get('name', '')
    current_url = source.get('url', '')
    candidates = []

    # 候选 1: 官方域名 + 标准 RSS 路径 (从当前 URL 推)
    try:
        parsed = urllib.parse.urlparse(current_url)
        # 如果是 RSSHub mirror, 提取 path 然后试官方域名
        if any(host in parsed.netloc for host in ['rsshub', 'rss.', 'feed.']):
            # rsshub.app/36kr/news → 36kr.com/feed
            path_parts = [p for p in parsed.path.split('/') if p]
            if len(path_parts) >= 1:
                guess_domain = f"{path_parts[0]}.com"
                candidates.append((f'https://{guess_domain}/feed', '官方域名+/feed'))
                candidates.append((f'https://{guess_domain}/rss', '官方域名+/rss'))
                candidates.append((f'https://{guess_domain}/feed.xml', '官方域名+/feed.xml'))
                candidates.append((f'https://{guess_domain}/atom.xml', '官方域名+/atom.xml'))

        # 如果当前是官方域名, 试 RSSHub mirror 们
        elif parsed.netloc and not any(h in parsed.netloc for h in ['rsshub', 'rss']):
            domain_root = parsed.netloc.replace('www.', '').split('.')[0]
            for mirror in ['rsshub.app', 'rss.ioiox.com', 'rsshub.atgw.io']:
                candidates.append((f'https://{mirror}/{domain_root}', f'RSSHub mirror ({mirror})'))
    except Exception:
        pass

    # 候选 2: 试当前 URL 的常见变体
    if current_url:
        if current_url.endswith('/'):
            candidates.append((current_url + 'feed', '当前 URL + /feed'))
            candidates.append((current_url + 'rss', '当前 URL + /rss'))
        elif not current_url.endswith(('feed', 'rss', '.xml')):
            candidates.append((current_url + '/feed', '当前 URL + /feed'))

        # https ↔ http 切换 (少数源仍要 http)
        if current_url.startswith('https://'):
            candidates.append((current_url.replace('https://', 'http://'), 'http 降级'))

    # 去重 + 排除当前 URL 本身 (因为它已死)
    seen = set([current_url])
    unique = []
    for url, desc in candidates:
        if url not in seen:
            seen.add(url)
            unique.append((url, desc))
    return unique[:6]  # 最多试 6 个候选, 避免太慢


def _test_candidate(source_template: dict, url: str) -> int:
    """测试候选 URL, 返回抓到的 item 数 (0 = 失败)."""
    from content_fetcher import fetch_feed
    test_source = dict(source_template)
    test_source['url'] = url
    try:
        items = fetch_feed(test_source, max_items=5, max_age_hours=720)
        return len(items) if items else 0
    except Exception as e:
        log.debug("    候选 %s 抓取异常: %s", url, str(e)[:60])
        return 0


def _load_dead_sources() -> List[dict]:
    """从 events.db 加载死源 (consecutive_failures >= DEAD_THRESHOLD).

    返回: [{name, consecutive_failures, ...}]
    """
    if not DB_PATH.exists():
        log.warning("events.db 不存在, 无源可修")
        return []
    try:
        with sqlite3.connect(str(DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM source_health WHERE consecutive_failures >= ? "
                "ORDER BY consecutive_failures DESC",
                (DEAD_THRESHOLD,)
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        log.warning("source_health 表不可读: %s", e)
        return []


def _find_source_in_config(name: str) -> Tuple[Optional[dict], Optional[str], Optional[int]]:
    """在 config.json 找到指定 name 的 source. 返回 (source dict, 'english'/'chinese', index)."""
    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    for lang in ('english', 'chinese'):
        for i, s in enumerate(cfg['sources'].get(lang, [])):
            if s.get('name') == name:
                return s, lang, i
    return None, None, None


def _save_config(cfg: dict) -> None:
    """原子写 config.json."""
    tmp = CONFIG_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(CONFIG_PATH)


def _try_fix_one(source_name: str) -> dict:
    """尝试修一个死源. 返回 result dict.

    {
      'name': str,
      'action': 'replaced' | 'disabled' | 'skipped' | 'no_candidates',
      'old_url': str,
      'new_url': str | None,
      'matched_via': str | None,  # 候选描述
      'fail_count': int,
      'tried_candidates': [(url, items), ...],
    }
    """
    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    source = None
    lang = None
    idx = None
    for lang_key in ('english', 'chinese'):
        for i, s in enumerate(cfg['sources'].get(lang_key, [])):
            if s.get('name') == source_name:
                source = s
                lang = lang_key
                idx = i
                break
        if source:
            break
    if not source:
        return {'name': source_name, 'action': 'skipped', 'reason': 'source not in config'}

    if source.get('enabled') is False:
        return {'name': source_name, 'action': 'skipped',
                'reason': 'already disabled in config'}

    candidates = _candidate_urls(source)
    if not candidates:
        return {
            'name': source_name, 'action': 'no_candidates',
            'old_url': source.get('url'),
        }

    log.info("  🔧 尝试修 %s (现 URL: %s, %d 个候选)",
             source_name, source.get('url', '')[:60], len(candidates))
    tried = []
    for cand_url, cand_desc in candidates:
        n_items = _test_candidate(source, cand_url)
        tried.append({'url': cand_url, 'desc': cand_desc, 'items': n_items})
        log.info("    候选 [%s] %s → %d items", cand_desc, cand_url[:60], n_items)
        if n_items >= 1:
            # 找到 work 的, 替换并保存
            old_url = source.get('url')
            cfg['sources'][lang][idx]['url'] = cand_url
            # 清掉历史 disabled 标记 (重新启用)
            for k in ['enabled', '_disabled_reason', '_auto_disabled_at',
                      '_auto_disabled_reason']:
                cfg['sources'][lang][idx].pop(k, None)
            _save_config(cfg)
            log.info("    ✓ 替换为: %s", cand_url)
            return {
                'name': source_name, 'action': 'replaced',
                'old_url': old_url, 'new_url': cand_url,
                'matched_via': cand_desc,
                'tried_candidates': tried,
            }

    # 所有候选都死, 自动 disable (不重复浪费时间)
    cfg['sources'][lang][idx]['enabled'] = False
    cfg['sources'][lang][idx]['_auto_disabled_at'] = datetime.now(timezone.utc).isoformat()
    cfg['sources'][lang][idx]['_auto_disabled_reason'] = (
        f'auto_fix_sources: {len(candidates)} 个候选全部不可用'
    )
    _save_config(cfg)
    log.warning("    ✗ 候选全死, 已自动 disable")
    return {
        'name': source_name, 'action': 'disabled',
        'old_url': source.get('url'),
        'tried_candidates': tried,
    }


def _git_commit_push(replaced: List[dict], disabled: List[dict]) -> bool:
    """如有 config 修改, 自动 commit + push."""
    if not (replaced or disabled):
        return False
    n_rep = len(replaced)
    n_dis = len(disabled)
    summary_parts = []
    if n_rep:
        summary_parts.append(f"{n_rep} replaced")
    if n_dis:
        summary_parts.append(f"{n_dis} auto-disabled")
    summary = ", ".join(summary_parts)

    detail_lines = []
    for r in replaced:
        detail_lines.append(
            f"- {r['name']}: {r['old_url']} → {r['new_url']} (via {r['matched_via']})"
        )
    for d in disabled:
        detail_lines.append(f"- {d['name']}: 候选全死, 自动 disable")
    detail = "\n".join(detail_lines)

    msg = f"""auto-fix(sources): {summary}

By auto_fix_sources.py daily ops job. Detected dead sources from
events.db.source_health (>= {DEAD_THRESHOLD} consecutive failures),
ran candidate URL probes, applied results.

{detail}
"""
    try:
        subprocess.run(['git', 'config', 'user.email', 'action@github.com'],
                       check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'auto-fix-bot'],
                       check=True, capture_output=True)
        subprocess.run(['git', 'add', str(CONFIG_PATH)],
                       check=True, capture_output=True)
        # 只 commit 如果有 stage 变更
        diff_check = subprocess.run(
            ['git', 'diff', '--cached', '--quiet'],
            capture_output=True
        )
        if diff_check.returncode == 0:
            log.info("  无 config 变更, 跳过 commit")
            return False
        subprocess.run(['git', 'commit', '-m', msg], check=True, capture_output=True)
        push = subprocess.run(['git', 'push', 'origin', 'HEAD:main'],
                              capture_output=True, text=True)
        if push.returncode != 0:
            log.warning("  push 失败: %s", push.stderr[:200])
            return False
        log.info("  ✓ 已 commit + push: %s", summary)
        return True
    except subprocess.CalledProcessError as e:
        log.warning("  git 操作失败: %s", e)
        return False


def _save_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding='utf-8')
    log.info("📋 维护报告写入: %s", REPORT_PATH)


def main():
    log.info("=" * 55)
    log.info("🤖 自治信源修复启动 (替代手工告警处理)")
    log.info("=" * 55)

    dead = _load_dead_sources()
    if not dead:
        log.info("✅ 没有死源, 系统健康, 静默退出")
        _save_report({'status': 'healthy', 'replaced': [], 'disabled': [],
                      'no_candidates': [], 'timestamp': datetime.now(timezone.utc).isoformat()})
        return 0

    log.info("🔍 发现 %d 个死源, 开始尝试修复", len(dead))
    for s in dead:
        log.info("  - %s (连失 %d 次)", s['name'], s['consecutive_failures'])

    replaced = []
    disabled = []
    no_candidates = []

    for src in dead:
        result = _try_fix_one(src['name'])
        if result['action'] == 'replaced':
            replaced.append(result)
        elif result['action'] == 'disabled':
            disabled.append(result)
        elif result['action'] == 'no_candidates':
            no_candidates.append(result)

    pushed = _git_commit_push(replaced, disabled)

    report = {
        'status': 'fixed' if (replaced or disabled) else 'unable',
        'replaced': replaced,
        'disabled': disabled,
        'no_candidates': no_candidates,
        'pushed_to_main': pushed,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    _save_report(report)

    log.info("=" * 55)
    log.info("📋 修复总结: %d 替换, %d 自动 disable, %d 无候选可试",
             len(replaced), len(disabled), len(no_candidates))
    log.info("=" * 55)
    return 0


if __name__ == '__main__':
    sys.exit(main())
