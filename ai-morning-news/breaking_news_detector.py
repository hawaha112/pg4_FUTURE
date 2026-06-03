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
import re
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
    fetch_reddit_hot,
)

log = get_logger('breaking')

# ── 阈值 (env 可调; 2026-05-30 重新校准) ──
# 旧值 HN>=800 / 2h 窗口实测永不触发: HN 故事要 6-12h 才攒够分, 最近 2h 内 AI 故事
# 常为 0 条 (见 breaking-news workflow 日志 "HN 抓到 0 条")。改成 24h 窗口看"当前
# 热榜爆款", 阈值降到 300 (实测 24h 内 AI top 故事约 300-720 分, 每天 0-3 条真命中)。
HN_POINTS_THRESHOLD = int(os.environ.get('BREAKING_HN_POINTS') or '300')  # or: 空串也回退默认
HN_WINDOW_HOURS = int(os.environ.get('BREAKING_HN_HOURS', '24'))
HF_LIKES_THRESHOLD = int(os.environ.get('BREAKING_HF_LIKES', '2000'))

# Reddit: 公开 hot.rss 无分数, 靠"在 AI 子版热榜 + 事件标题(发布/型号/事故)"判突发(默认开)。
# 默认 5 个高信号 AI 子版; 都过 _EVENT_ACTION_RE 严格动作词闸, 讨论帖进不来。
# BREAKING_REDDIT=false 可关; BREAKING_REDDIT_SUBS 可自定义子版列表。
REDDIT_ENABLED = os.environ.get('BREAKING_REDDIT', 'true').lower() == 'true'
REDDIT_SUBS = [s.strip() for s in os.environ.get(
    'BREAKING_REDDIT_SUBS',
    'LocalLLaMA,MachineLearning,StableDiffusion,singularity,OpenAI').split(',') if s.strip()]

# 单次最多推几条 (防冷启动 / 大新闻日一次性轰炸)
MAX_PUSH_PER_RUN = int(os.environ.get('BREAKING_MAX_PER_RUN', '5'))

# "只要大事": HN 高分 != 大事 (观点帖"Please Use AI"也能上 700 分)。只放行标题像
# 真新闻事件的 (发布/融资/收购/事故/带版本号的型号), 滤掉爆火的观点/讨论/提问帖。
# 想退回"所有热门 HN 都推"就设 BREAKING_HN_EVENT_ONLY=false。
HN_EVENT_ONLY = os.environ.get('BREAKING_HN_EVENT_ONLY', 'true').lower() == 'true'
_HN_EVENT_RE = re.compile(
    r'(?i)('
    # 动作: 发布 / 融资 / 收购 / 事故
    r'launch|releas|announc|unveil|introduc|debut|ships?\b|shipped|rolls?\s?out|'
    r'open[\s-]?sourc|now available|general availability|'
    r'raise[sd]?\b|raising|funding|\$\d|valuation|acqui|merger|\bipo\b|'
    r'shuts?\s?down|outage|\bdown\b|breach|hacked|\bleak|lawsuit|sue[sd]?\b|'
    r'\bbans?\b|banned|lay[s]?\s?off|layoffs?|fired|resign|'
    # 带版本号的型号 (GPT-5 / Claude 4 / Gemini 3 / Llama 4 / o3 ...)
    r'GPT-?\d|Claude\s?(?:Opus|Sonnet|Haiku|\d)|Gemini\s?\d|Llama\s?\d|'
    r'DeepSeek[-\s]?[RV]?\d|Grok\s?\d|Qwen\s?\d|\bo[1-9]\b|'
    # 中文
    r'发布|开源|推出|上线|融资|收购|宕机|崩溃|泄露|诉讼|封禁|裁员|下架'
    r')'
)

# Reddit 专用(更严): 只认"动作/事故"词, 去掉光有型号名也算的部分 —— Reddit 讨论帖
# 到处提 Qwen/Claude, 光匹配型号名会把"我把 Claude 换成 Qwen"这种讨论当成事件。
# 要求 released/launched/发布 这类真动作词才放行。
_EVENT_ACTION_RE = re.compile(
    r'(?i)('
    r'launch|releas|announc|unveil|introduc|debut|ships?\b|shipped|rolls?\s?out|'
    r'open[\s-]?sourc|now available|general availability|'
    r'raise[sd]?\b|raising|funding|\$\d|valuation|acqui|merger|\bipo\b|'
    r'shuts?\s?down|outage|\bdown\b|breach|hacked|\bleak|lawsuit|sue[sd]?\b|'
    r'\bbans?\b|banned|lay[s]?\s?off|layoffs?|fired|resign|'
    r'发布|开源|推出|上线|融资|收购|宕机|崩溃|泄露|诉讼|封禁|裁员|下架'
    r')'
)

# 噪声闸(在事件闸之前先否掉): 问句/观点/讨论帖即便蹭到动作词或型号名也不是突发。
# 实测泄漏案例 (min_points=10):
#   "When will DolphinGemma be released?" — 问句(蹭 released)
#   "Michael Burry says ... aren't worth $1 trillion" — 估值观点(蹭 $1)
#   "Weird problem with OpenCode and Qwen3.6" — 讨论帖(蹭型号名 Qwen3)
# 过滤跑在翻译前的英文原标题上, 故以英文标记为主, 附少量中文兜底。
_NOISE_RE = re.compile(
    r'(?i)('
    r'[?？]\s*$|'                                                  # 问号结尾 = 问句
    r'^\s*(why|how|what|when|where|who|whose|whether|should)\b|'   # 疑问词开头
    r'\b(vs\.?|versus|opinion|thoughts?\s+on|\brant\b|'
    r'please\s+(use|stop)|why\s+you\s+should|'
    r'(are|is)\s*n.?t\s+worth|not\s+worth|over\s?valued|under\s?valued|'
    r'\bbubble\b|weird|strange|\bodd\b|confusing|'
    r'i\s+(built|made|wrote|tried|switched|replaced))\b|'
    r'疑似|奇怪|吐槽|求助|请教'
    r')'
)


def _is_noise(title: str) -> bool:
    """问句/观点/讨论帖 → True (在事件闸之前先否掉)。HN_EVENT_ONLY 关时不生效。"""
    return HN_EVENT_ONLY and bool(_NOISE_RE.search(title or ''))


# ── 去重窗口 ── (48h: HF trending 模型常持续多天热, 24h TTL 会让同一爆款天天重推)
DEDUP_TTL_HOURS = int(os.environ.get('BREAKING_DEDUP_TTL_HOURS', '48'))

# ── 路径 ──
SCRIPT_DIR = Path(__file__).parent
PUSHED_PATH = SCRIPT_DIR / 'output' / 'pushed_breaking.json'
# detect 阶段把"本次该推的新突发"写这里, push 阶段读它翻译后累积 (两段式, 见 main)
PENDING_PATH = SCRIPT_DIR / 'output' / 'breaking_pending.json'
# push 阶段输出的突发数据(部署到 部署仓/archive/breaking.json, 早报页前端拉取渲染顶部突发区块)
BREAKING_JSON_PATH = SCRIPT_DIR / 'output' / 'breaking.json'
# workflow 读这个 flag 决定是否部署+更新 TG 链接: 内容 "新增数 窗口内总数"
PUSH_FLAG_PATH = SCRIPT_DIR / 'output' / 'breaking_push.flag'
# 突发页显示窗口(小时); 比 DEDUP_TTL(48h) 短, 页面只列近 24h
DISPLAY_WINDOW_HOURS = int(os.environ.get('BREAKING_DISPLAY_HOURS', '24'))

# ── TG 凭据 ──
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '')
BRIEFING_URL = (os.environ.get('BRIEFING_URL', '') or '').rstrip('/')


def _get_state_store():
    """懒初始化 StateStore (events.db 优先, 共享 SOT)."""
    from state_store import StateStore
    db_path = SCRIPT_DIR / 'events.db'
    return StateStore(db_path)


def _load_pushed() -> dict:
    """加载去重历史 (DB 主 + JSON fallback 兼容旧版).

    SOT 升级 (2026-05-16): 主存 events.db.pushed_breaking. JSON 仅在 DB 空时
    一次性回迁历史数据.
    """
    try:
        store = _get_state_store()
        db_data = store.load_pushed_breaking(ttl_hours=DEDUP_TTL_HOURS)
        if db_data:
            return db_data
    except Exception as e:
        log.warning("⚠️ DB load pushed_breaking 失败 (回退 JSON): %s", e)

    # DB 空 → 兼容回退 JSON
    if PUSHED_PATH.exists():
        try:
            data = json.loads(PUSHED_PATH.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                return {}
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=DEDUP_TTL_HOURS)
            ).isoformat()
            valid = {
                k: v for k, v in data.items()
                if isinstance(v, dict) and v.get('pushed_at', '') >= cutoff
            }
            # 顺手迁到 DB
            if valid:
                try:
                    store = _get_state_store()
                    for sid, info in valid.items():
                        store.mark_breaking_pushed(
                            sig_id=sid,
                            source=info.get('source', 'unknown'),
                            title=info.get('title', ''),
                            url=info.get('url', ''),
                            score=info.get('score', 0),
                        )
                    log.info("✓ 首次升级: 已迁 %d 条突发去重历史从 JSON → DB",
                             len(valid))
                except Exception as e:
                    log.warning("⚠️ JSON→DB 迁移失败: %s", e)
            return valid
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_pushed(pushed: dict) -> None:
    """主写 DB, 兼容写 JSON (旧 reader 保留一段时间).

    注意: 这个函数被调用时 pushed 已包含本次新推 + 历史. 为了避免 DB 重复 upsert
    所有历史 (浪费), 改为 caller 通过 _mark_pushed_one() 增量写, 这里只保 JSON 兼容.
    """
    PUSHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        PUSHED_PATH.write_text(
            json.dumps(pushed, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    except OSError as e:
        log.warning("⚠️ 保存 pushed_breaking.json 失败 (DB 已更新, 可忽略): %s", e)


def _mark_pushed_one(sig_id: str, source: str, title: str, url: str, score: int) -> None:
    """单条推送后立即写 DB (主路径)."""
    try:
        _get_state_store().mark_breaking_pushed(sig_id, source, title, url, score)
    except Exception as e:
        log.warning("⚠️ DB mark_breaking_pushed 失败: %s", e)


def _detect_breaking() -> list:
    """扫 4 个源, 返回命中突发阈值的 signals (含 _source 和 _id)."""
    breaking = []

    # HN — 看最近 HN_WINDOW_HOURS 小时内的 AI 热榜, 取高分爆款
    log.info("🔍 检查 HN top stories (近 %dh)...", HN_WINDOW_HOURS)
    try:
        hn_signals = fetch_hn_top(limit=20, hours=HN_WINDOW_HOURS)
        for sig in hn_signals:
            pts = int(sig.get('points') or 0)
            if pts < HN_POINTS_THRESHOLD:
                continue
            title = sig.get('title', '')
            # 只要"大事": 先否掉问句/观点/讨论, 再要求标题像真新闻事件
            if _is_noise(title):
                log.info("  ⏭️ HN 跳过(问句/观点/讨论, %d 分): %s", pts, title[:50])
                continue
            if HN_EVENT_ONLY and not _HN_EVENT_RE.search(title):
                log.info("  ⏭️ HN 跳过(非事件类, %d 分): %s", pts, title[:50])
                continue
            sig['_source'] = 'hn'
            sig['_id'] = f"hn:{sig.get('hn_id') or sig.get('url', '')}"
            breaking.append(sig)
            log.info("  🚨 HN 命中: %d 分 — %s", pts, title[:60])
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

    # Reddit — AI 子版 hot.rss (无分数, 用'事件标题'过滤判突发; 子版本身即 AI 相关)
    if REDDIT_ENABLED:
        log.info("🔍 检查 Reddit hot (%s)...", '/'.join(REDDIT_SUBS))
        try:
            for sig in fetch_reddit_hot(subreddits=REDDIT_SUBS, limit_per_sub=15):
                title = sig.get('title', '')
                # 无分数 + 讨论帖多 → 先否掉问句/观点/讨论, 再用更严的"动作词"过滤(不认光有型号名)
                if _is_noise(title):
                    continue
                if HN_EVENT_ONLY and not _EVENT_ACTION_RE.search(title):
                    continue
                sig['_source'] = 'reddit'
                sig['_id'] = f"reddit:{sig.get('url', '')}"
                breaking.append(sig)
                log.info("  💬 Reddit 命中: r/%s — %s", sig.get('subreddit', ''), title[:55])
        except Exception as e:
            log.warning("Reddit 抓取异常: %s", e)

    return breaking


def _translate_title(title: str) -> str:
    """调本地 LLM 代理把外文标题译成简洁中文标题 (best-effort, 卡片化用)。

    仅在 push 阶段、确有突发命中时才会被调到 —— 每天 0-3 次、每次几百 token,
    成本可忽略。代理不可用 / 超时 / 失败 → 返回 ''(调用方回退英文原标题, 永不阻塞推送)。
    """
    title = (title or '').strip()
    if not title:
        return ''
    base = os.environ.get('LLM_BASE_URL', 'http://localhost:3456/v1').rstrip('/')
    prompt = (
        "把下面这条 AI 新闻标题翻译成简洁、准确的中文标题: 保留公司/产品/型号原名"
        "(如 GPT-5、Claude、OpenAI、NVIDIA、DeepSeek), 不超过 40 字, 不要加引号或解释, "
        "只输出中文标题这一行。\n\n标题: " + title
    )
    payload = json.dumps({
        'model': 'claude-sonnet-4',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 120,
        'temperature': 0,
    }).encode('utf-8')
    req = urllib.request.Request(
        base + '/chat/completions', data=payload, method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        txt = ((body.get('choices') or [{}])[0].get('message') or {}).get('content', '')
        txt = (txt or '').strip().strip('"\'').strip()
        return txt.splitlines()[0][:60] if txt else ''
    except Exception as e:
        log.warning("⚠️ 标题翻译失败(回退英文原标题): %s", e)
        return ''


def _format_breaking_msg(sig: dict) -> str:
    """生成突发 TG 卡片 (HTML)。标题翻译成中文卡片化呈现, 同时附英文原标题便于核对;
    翻译不可用时只显示原标题, 永不阻塞推送。命中信号(HN 分/HF 赞)保留。"""
    src = sig.get('_source')
    title = (sig.get('title') or '').strip()
    url = sig.get('url', '')
    ts = datetime.now(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')

    zh = _translate_title(title)
    if zh and zh != title:
        title_block = f"<b>{_html_escape(zh)}</b>\n<i>{_html_escape(title)}</i>"
    else:
        title_block = f"<b>{_html_escape(title)}</b>"

    if src == 'hn':
        pts = int(sig.get('points') or 0)
        comments = int(sig.get('comments') or 0)
        return (
            f"🚨 <b>突发 · HN 热点</b> · {ts}\n\n"
            f"{title_block}\n\n"
            f"📊 {pts} 分 · {comments} 评论\n"
            f'<a href="{url}">🔗 阅读原文 →</a>'
        )
    if src == 'hf':
        likes = int(sig.get('likes') or 0)
        type_ = sig.get('type', 'model')
        type_zh = {'model': '模型', 'dataset': '数据集', 'space': '应用'}.get(type_, type_)
        return (
            f"🔥 <b>突发 · HuggingFace 爆款{type_zh}</b> · {ts}\n\n"
            f"{title_block}\n\n"
            f"❤️ {likes} 赞 · 7 天 trending\n"
            f'<a href="{url}">🔗 查看 →</a>'
        )
    if src == 'reddit':
        sub = sig.get('subreddit', '') or 'AI'
        return (
            f"🚨 <b>突发 · Reddit 热议</b> · {ts}\n\n"
            f"{title_block}\n\n"
            f"💬 r/{sub} · 正在热榜\n"
            f'<a href="{url}">🔗 查看讨论 →</a>'
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


def _detect_and_select() -> list:
    """检测 → 去重过滤 → 按热度排序 → 取 top N。返回本次该推的新 signals
    (尚未推送、尚未标记)。detect 与 all 两种模式共用。"""
    pushed = _load_pushed()
    log.info("📋 已推送历史 (%dh 内): %d 条", DEDUP_TTL_HOURS, len(pushed))
    breaking = _detect_breaking()
    log.info("🎯 检测到突发候选: %d 条", len(breaking))
    # 按分数降序, 大新闻优先
    breaking.sort(key=lambda s: int(s.get('points') or s.get('likes') or 0), reverse=True)
    selected = []
    for sig in breaking:
        if len(selected) >= MAX_PUSH_PER_RUN:
            log.info("  ⏸️ 已达单次上限 %d 条, 余下留待下轮", MAX_PUSH_PER_RUN)
            break
        if sig.get('_id') in pushed:
            log.info("  ⏭️ 已推过 (%dh): %s", DEDUP_TTL_HOURS, sig.get('_id'))
            continue
        selected.append(sig)
    return selected


def _signal_text(sig: dict) -> str:
    """命中信号文案(HN 分/HF 赞/Reddit 子版), 显示在突发卡片上。"""
    src = sig.get('_source')
    if src == 'hn':
        return f"HN {int(sig.get('points') or 0)} 分 · {int(sig.get('comments') or 0)} 评论"
    if src == 'hf':
        tz = {'model': '模型', 'dataset': '数据集', 'space': '应用'}.get(
            sig.get('type', 'model'), sig.get('type', 'model'))
        return f"HuggingFace 爆款{tz} · {int(sig.get('likes') or 0)} 赞"
    if src == 'reddit':
        return f"r/{sig.get('subreddit', '')} 热榜"
    return ''


def _accumulate_signals(signals: list) -> int:
    """翻译每条新突发并累积进 pushed_breaking(含中文标题+命中信号), 供突发页渲染。
    不再逐条推 TG —— TG 只保留一条指向突发页的链接(由 workflow 删旧推新)。返回新增条数。"""
    pushed = _load_pushed()
    n = 0
    for sig in signals:
        sid = sig.get('_id')
        if not sid or sid in pushed:
            continue
        title_en = (sig.get('title') or '').strip()
        zh = _translate_title(title_en)   # best-effort, 失败回退英文
        score_val = sig.get('points') or sig.get('likes') or sig.get('stars') or 0
        pushed[sid] = {
            'pushed_at': datetime.now(timezone.utc).isoformat(),
            'source': sig.get('_source'),
            'title': title_en[:160],
            'title_zh': (zh or '')[:80],
            'signal': _signal_text(sig),
            'url': sig.get('url', ''),
            'score': score_val,
        }
        _mark_pushed_one(sig_id=sid, source=sig.get('_source', 'unknown'),
                         title=title_en, url=sig.get('url', ''), score=score_val)
        n += 1
        log.info("  ➕ 收录突发: %s", (zh or title_en)[:50])
    _save_pushed(pushed)
    return n


def _breaking_events_in_window(hours: int) -> list:
    """从 pushed_breaking 取近 N 小时的突发事件, 按时间倒序, 供渲染突发页。"""
    pushed = _load_pushed()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    evs = [info for info in pushed.values()
           if isinstance(info, dict) and info.get('pushed_at', '') >= cutoff]
    evs.sort(key=lambda e: e.get('pushed_at', ''), reverse=True)
    return evs


def _breaking_payload(events: list) -> str:
    """把近 N 小时突发事件序列化成 JSON, 部署到 archive/breaking.json,
    供早报页前端拉取渲染顶部「🚨 突发」区块(打开早报时实时拉, 始终最新)。"""
    out = []
    for e in events:
        out.append({
            'zh': e.get('title_zh') or e.get('title') or '',
            'en': e.get('title') or '',
            'signal': e.get('signal') or '',
            'url': e.get('url') or '',
            'ts': e.get('pushed_at') or '',
        })
    return json.dumps(
        {'updated_at': datetime.now(timezone.utc).isoformat(), 'count': len(out), 'events': out},
        ensure_ascii=False)


def _render_breaking_html(events: list, briefing_url: str = '') -> str:  # noqa: 暂留(未使用)
    """[已弃用] 旧的独立突发页渲染; 现突发并入早报页前端渲染, 保留备用。"""
    now = datetime.now(timezone(timedelta(hours=8)))

    def _rel(iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=8)))
            mins = int((now - dt).total_seconds() // 60)
            if mins < 60:
                return f'{max(mins, 0)} 分钟前'
            if mins < 1440:
                return f'{mins // 60} 小时前'
            return dt.strftime('%m-%d %H:%M')
        except Exception:
            return ''

    cards = []
    for e in events:
        zh = _html_escape(e.get('title_zh') or e.get('title') or '(无标题)')
        en = _html_escape(e.get('title') or '')
        sig = _html_escape(e.get('signal') or '')
        url = e.get('url') or '#'
        en_html = f'<div class="bk-en">{en}</div>' if (en and e.get('title_zh')) else ''
        cards.append(
            f'<a class="bk-card" href="{url}" target="_blank" rel="noopener">'
            f'<div class="bk-title">🚨 {zh}</div>{en_html}'
            f'<div class="bk-meta"><span>{sig}</span>'
            f'<span class="bk-time">{_rel(e.get("pushed_at", ""))}</span></div></a>'
        )
    body = '\n'.join(cards) if cards else '<p class="bk-empty">近 24 小时暂无突发事件。</p>'
    back = (f'<a class="bk-back" href="{_html_escape(briefing_url)}">← 早报首页</a>'
            if briefing_url else '')
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🚨 AI 突发列表 · 近 24 小时</title>
<style>
:root{{--bg:#0c1220;--card:rgba(255,255,255,.04);--bd:rgba(255,255,255,.10);--t1:#ecf0f5;--t2:#b6bdcb;--t3:#8a93a6;--accent:#ff6b6b}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 16px;background:var(--bg);color:var(--t1);font-family:-apple-system,'PingFang SC','Noto Sans SC',sans-serif;line-height:1.6}}
.wrap{{max-width:760px;margin:0 auto}}
.bk-back{{color:var(--t3);font-size:12px;text-decoration:none;display:inline-block;margin-bottom:14px}}
.bk-h{{font-size:22px;font-weight:800;margin:0 0 4px}}
.bk-sub{{color:var(--t3);font-size:12px;margin:0 0 22px}}
.bk-card{{display:block;text-decoration:none;background:var(--card);border:1px solid var(--bd);border-left:3px solid var(--accent);border-radius:9px;padding:14px 16px;margin-bottom:12px;transition:border-color .15s,transform .15s}}
.bk-card:hover{{border-color:var(--accent);transform:translateY(-1px)}}
.bk-title{{font-size:16px;font-weight:700;color:var(--t1);line-height:1.5}}
.bk-en{{font-size:12px;color:var(--t3);margin-top:4px;font-style:italic}}
.bk-meta{{display:flex;justify-content:space-between;gap:10px;margin-top:9px;font-size:12px;color:var(--t2)}}
.bk-time{{color:var(--t3);white-space:nowrap}}
.bk-empty{{color:var(--t3);font-size:14px;padding:20px 0}}
</style></head><body><div class="wrap">
{back}
<h1 class="bk-h">🚨 AI 突发列表</h1>
<p class="bk-sub">近 24 小时 · 共 {len(events)} 条 · 更新于 {now.strftime('%m-%d %H:%M')} · 点卡片看原文</p>
{body}
</div></body></html>'''


def main() -> int:
    mode = sys.argv[1].strip().lower() if len(sys.argv) > 1 else 'all'
    log.info("=" * 55)
    log.info("📡 突发检测器 [mode=%s] — 阈值 HN>=%d / HF>=%d",
             mode, HN_POINTS_THRESHOLD, HF_LIKES_THRESHOLD)
    log.info("=" * 55)

    # detect: 只检测+选出, 写 pending, 不推不标记 (留给 push 阶段翻译后推)。
    # 无突发的小时(绝大多数)纯 stdlib 跑完即止, 不必装 claude → 省 token 省 Actions。
    if mode == 'detect':
        selected = _detect_and_select()
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text(json.dumps(selected, ensure_ascii=False), encoding='utf-8')
        log.info("✅ detect: %d 条新突发写入 pending", len(selected))
        return 0

    # push: 读 pending → 翻译累积进突发库 → 渲染突发页(近24h)。写 push flag 给 workflow
    # 决定是否部署+删旧推新 TG 链接(仅当有新增)。不再逐条推 TG。
    if mode == 'push':
        try:
            signals = json.loads(PENDING_PATH.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            signals = []
        new_n = _accumulate_signals(signals) if signals else 0
        try:
            PENDING_PATH.unlink()
        except OSError:
            pass
        events = _breaking_events_in_window(DISPLAY_WINDOW_HOURS)
        BREAKING_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        BREAKING_JSON_PATH.write_text(_breaking_payload(events), encoding='utf-8')
        # 必须带结尾换行: workflow 里 `read NEW TOTAL < flag` 在无换行(撞 EOF)时
        # 会返回非零, `bash -e` 下整步骤直接 exit 1 (踩过)。
        PUSH_FLAG_PATH.write_text(f'{new_n} {len(events)}\n', encoding='utf-8')
        log.info("✅ push: 新增 %d 条, 近 %dh 共 %d 条, 已写 breaking.json",
                 new_n, DISPLAY_WINDOW_HOURS, len(events))
        return 0

    # demo: 用给定(或示例)标题走完整 翻译→卡片→推送, 标注[演示]、不进去重。
    # 用途: 没有真实突发时, 也能验证翻译+卡片链路是否正常 (需代理可用)。
    if mode == 'demo':
        demo_title = (sys.argv[2] if len(sys.argv) > 2 else '').strip() or \
            'Mistral releases Large 3, an open-weight model rivaling GPT-5'
        sig = {
            '_source': 'hn', 'title': demo_title,
            'url': 'https://news.ycombinator.com/', 'points': 542, 'comments': 210,
        }
        card = _format_breaking_msg(sig)
        ok = _send_tg("🧪 <b>[翻译卡片演示 · 非真实突发]</b>\n\n" + card)
        log.info("✅ demo: 演示卡片推送 ok=%s — %s", ok, demo_title[:60])
        return 0

    # all (默认, 本地/兜底): 检测 → 累积 → 渲染突发页(不部署/不推 TG, 供本地查看)。
    selected = _detect_and_select()
    new_n = _accumulate_signals(selected) if selected else 0
    events = _breaking_events_in_window(DISPLAY_WINDOW_HOURS)
    BREAKING_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    BREAKING_JSON_PATH.write_text(_breaking_payload(events), encoding='utf-8')
    log.info("✅ 完成: 新增 %d 条, 近 %dh 共 %d 条, breaking.json 已写",
             new_n, DISPLAY_WINDOW_HOURS, len(events))
    return 0


if __name__ == '__main__':
    sys.exit(main())
