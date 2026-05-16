"""
breaking_news_detector.py — 24 小时突发热点推送 (P1)

每小时由 GH Actions / Anthropic Routine 触发. 扫描外部热度信号
(HN / HuggingFace Trending / GitHub), 命中突发阈值即推 Telegram.

设计原则:
1. 不依赖 events.db (不跑 collector full pipeline). 仅消费 hot signals,
   保证 < 30 秒跑完, 不烧 LLM token.
2. 高阈值 — 只推真·突发, 避免轰炸 (一天最多推 3-5 条).
3. 24h 去重 — 同事件不重复推送.
4. 本地状态: output/pushed_breaking.json (GH Actions Cache 持久化).

阈值 (实测校准, 可后续调):
- HN points >= 800        (顶级讨论, 通常每天 0-2 条真正命中)
- HuggingFace likes7d >= 2500  (新模型/dataset 真炸了, 不是累积流行)

不用 GitHub: 实测 GitHub Search API 返回的是历史巨库 (AutoGPT/ollama 等),
绝对 stars 不是"突发"信号. GitHub trending 留给早晚日报用.
不用 Reddit: 公开 RSS 没分数, 无法判断"突发".
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 项目内 logger
sys.path.insert(0, str(Path(__file__).parent))
from logger import get_logger
from extractors.hot_signals import (
    fetch_hn_top,
    fetch_hf_trending,
)

log = get_logger('breaking')

# ── 阈值 ──
HN_POINTS_THRESHOLD = 800
HF_LIKES_THRESHOLD = 2500

# ── 去重窗口 ──
DEDUP_TTL_HOURS = 24

# ── 路径 ──
SCRIPT_DIR = Path(__file__).parent
PUSHED_PATH = SCRIPT_DIR / 'output' / 'pushed_breaking.json'

# ── TG 凭据 ──
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '')
BRIEFING_URL = (os.environ.get('BRIEFING_URL', '') or '').rstrip('/')


def _load_pushed() -> dict:
    """加载去重历史 + 清理 TTL 之外的条目."""
    if not PUSHED_PATH.exists():
        return {}
    try:
        data = json.loads(PUSHED_PATH.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {}
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=DEDUP_TTL_HOURS)
        ).isoformat()
        return {
            k: v for k, v in data.items()
            if isinstance(v, dict) and v.get('pushed_at', '') >= cutoff
        }
    except (json.JSONDecodeError, OSError):
        return {}


def _save_pushed(pushed: dict) -> None:
    PUSHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        PUSHED_PATH.write_text(
            json.dumps(pushed, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    except OSError as e:
        log.warning("⚠️ 保存 pushed_breaking.json 失败: %s", e)


def _detect_breaking() -> list:
    """扫 4 个源, 返回命中突发阈值的 signals (含 _source 和 _id)."""
    breaking = []

    # HN — 只看最近 2 小时, 阈值高
    log.info("🔍 检查 HN top stories...")
    try:
        hn_signals = fetch_hn_top(limit=15, hours=2)
        for sig in hn_signals:
            pts = int(sig.get('points') or 0)
            if pts >= HN_POINTS_THRESHOLD:
                sig['_source'] = 'hn'
                sig['_id'] = f"hn:{sig.get('hn_id') or sig.get('url', '')}"
                breaking.append(sig)
                log.info("  🚨 HN 命中: %d 分 — %s", pts, sig.get('title', '')[:60])
    except Exception as e:
        log.warning("HN 抓取异常: %s", e)

    # HuggingFace — top trending 模型/spaces
    log.info("🔍 检查 HF trending...")
    try:
        hf_signals = fetch_hf_trending(limit_per_type=5)
        for sig in hf_signals:
            likes = int(sig.get('likes') or 0)
            if likes >= HF_LIKES_THRESHOLD:
                sig['_source'] = 'hf'
                sig['_id'] = f"hf:{sig.get('url', '')}"
                breaking.append(sig)
                log.info("  🔥 HF 命中: %d ♥ — %s", likes, sig.get('title', '')[:60])
    except Exception as e:
        log.warning("HF 抓取异常: %s", e)

    return breaking


def _format_breaking_msg(sig: dict) -> str:
    """根据源类型生成简短 TG 推送消息 (HTML 格式)."""
    src = sig.get('_source')
    title = sig.get('title', '').strip()
    url = sig.get('url', '')

    # 时间戳
    ts = datetime.now(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')

    if src == 'hn':
        pts = int(sig.get('points') or 0)
        comments = int(sig.get('comments') or 0)
        return (
            f"🚨 <b>HN 突发</b> · {ts}\n"
            f"<b>{pts} 分</b> / {comments} 评论\n"
            f"\n"
            f'<a href="{url}">{_html_escape(title)}</a>'
        )
    if src == 'hf':
        likes = int(sig.get('likes') or 0)
        type_ = sig.get('type', 'model')
        type_zh = {'model': '模型', 'dataset': '数据集', 'space': '应用'}.get(type_, type_)
        return (
            f"🔥 <b>HuggingFace 爆款{type_zh}</b> · {ts}\n"
            f"<b>{likes} 赞</b> · 7 天 trending\n"
            f"\n"
            f'<a href="{url}">{_html_escape(title)}</a>'
        )
    return ''


def _html_escape(s: str) -> str:
    return (str(s).replace('&', '&amp;')
                  .replace('<', '&lt;')
                  .replace('>', '&gt;'))


def _send_tg(html_text: str) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("⚠️ TG_BOT_TOKEN / TG_CHAT_ID 未配置, 跳过推送")
        return False
    payload = json.dumps({
        'chat_id': TG_CHAT_ID,
        'text': html_text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage',
        data=payload, method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            if body.get('ok'):
                return True
            log.warning("TG API 返回非 ok: %s", str(body)[:200])
            return False
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.warning("TG 推送异常: %s", e)
        return False


def main() -> int:
    log.info("=" * 55)
    log.info("📡 突发热点检测器启动 — 阈值: HN>=%d / HF>=%d",
             HN_POINTS_THRESHOLD, HF_LIKES_THRESHOLD)
    log.info("=" * 55)

    pushed = _load_pushed()
    log.info("📋 已推送历史 (24h 内): %d 条", len(pushed))

    breaking = _detect_breaking()
    log.info("🎯 检测到突发候选: %d 条", len(breaking))

    if not breaking:
        log.info("✅ 无突发, 退出")
        return 0

    new_pushes = []
    for sig in breaking:
        sid = sig['_id']
        if sid in pushed:
            log.info("  ⏭️ 已推过 (24h): %s", sid)
            continue
        msg = _format_breaking_msg(sig)
        if not msg:
            continue
        if _send_tg(msg):
            pushed[sid] = {
                'pushed_at': datetime.now(timezone.utc).isoformat(),
                'source': sig.get('_source'),
                'title': sig.get('title', '')[:120],
                'url': sig.get('url', ''),
                'score': (
                    sig.get('points')
                    or sig.get('likes')
                    or sig.get('stars')
                    or 0
                ),
            }
            new_pushes.append(sid)
            log.info("  ✅ 已推送: %s", sig.get('title', '')[:60])

    _save_pushed(pushed)
    log.info("=" * 55)
    log.info("✅ 完成: 推送 %d 条新突发, 总历史 %d 条",
             len(new_pushes), len(pushed))
    log.info("=" * 55)
    return 0


if __name__ == '__main__':
    sys.exit(main())
