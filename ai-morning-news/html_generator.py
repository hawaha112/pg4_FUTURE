"""
html_generator.py - HTML page generation for AI Morning News

Generates the complete HTML page using external template files:
  templates/page.html  — HTML skeleton with $variable placeholders
  templates/style.css  — all CSS styles
  templates/script.js  — all JavaScript (modal, filter, search, keyboard)

Uses string.Template for substitution (zero external dependencies).
"""

import json
import re
from html import escape, unescape


def _safe_escape(text):
    """先 unescape 已有 HTML 实体（如 RSS 里的 &#x27;），再 escape 一次防 XSS。
    避免双重 escape 让 &#x27; 变成 &amp;#x27; 在浏览器里显示为字面量。"""
    return escape(unescape(str(text or '')))


def _render_editorial(raw: str) -> str:
    """把"今日速览"原文渲染成多段 HTML。

    新版 digest prompt 让 LLM 输出三层结构（主旋律 / 分类组 / 收束），用
    `\\n\\n` 分段、用 `**xxx**` 加粗主题。这里负责：
      1. 按空行切段
      2. 每段 escape 后把 `**...**` 还原成 `<strong>...</strong>`
      3. 包成 <p class="br-editorial">

    `**` 在 escape 之后仍是字面量（HTML 不转义星号），所以正则替换安全。
    """
    paragraphs = []
    for raw_para in (raw or '').split('\n\n'):
        para = raw_para.strip()
        if not para:
            continue
        body = _safe_escape(para)
        body = re.sub(r'\*\*([^*\n]+?)\*\*',
                      r'<strong class="br-bold">\1</strong>', body)
        paragraphs.append(f'<p class="br-editorial">{body}</p>')
    return ''.join(paragraphs) if paragraphs else ''
from datetime import datetime, timezone


def _format_local_time(pub_val) -> str:
    """把 published 字段统一格式化为本地时区的 'MM-DD HH:MM'。

    背景：源头的 published_at 通常是 UTC ISO 字符串（带 +00:00 或 Z），
    早期版本直接 strftime 输出 UTC 时间，导致"04-28 22:02 UTC"在北京时区
    应显示为"04-29 06:02"，但卡片上变成 04-28，让用户以为是昨天的内容。
    """
    if not pub_val:
        return ""
    try:
        if isinstance(pub_val, str):
            clean = pub_val.replace('Z', '+00:00')
            pub_val = datetime.fromisoformat(clean)
        # 若 naive，按 UTC 处理；统一 astimezone 到本地
        if pub_val.tzinfo is None:
            pub_val = pub_val.replace(tzinfo=timezone.utc)
        return pub_val.astimezone().strftime("%m-%d %H:%M")
    except (AttributeError, ValueError, TypeError):
        return ""
from pathlib import Path
from string import Template

from logger import get_logger

log = get_logger('html_generator')

# Template directory (relative to this file)
_TEMPLATE_DIR = Path(__file__).parent / 'templates'


def _load_template(name: str) -> str:
    """Load a template file from the templates/ directory."""
    path = _TEMPLATE_DIR / name
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# -- 来源类型中文映射 --
SOURCE_TYPE_LABELS = {
    "paper": "学术论文",
    "news": "新闻报道",
    "official": "官方发布",
    "opinion": "观点文章",
    "community": "社区讨论",
    "video": "视频",
}

# -- 源 tier / category → 徽章（图标 + 中文名） --
# tier 0 = 官方；tier 1 + category=academic/research → 学术；其余按 source_type
SOURCE_TIER_BADGES = {
    "official":   ("🏢", "官方"),
    "academic":   ("🎓", "学术"),
    "media":      ("📰", "媒体"),
    "community":  ("💬", "社区"),
    "video":      ("🎥", "视频"),
}

# -- 目标读者枚举 → 中文标签 + 图标 --
AUDIENCE_LABELS = {
    "researcher": ("研究员", "🔬"),
    "developer":  ("开发者", "💻"),
    "pm":         ("产品",   "📐"),
    "investor":   ("投资人", "📈"),
    "general":    ("大众",   "👥"),
}


def _source_badge(item):
    """返回 (icon, label) 作为源徽章；返回 None 时不渲染徽章。"""
    tier = item.get('source_tier', 2)
    if tier == 0:
        return SOURCE_TIER_BADGES["official"]
    src_type = (item.get('analysis', {}).get('source_type') or '').strip()
    if src_type == 'paper':
        return SOURCE_TIER_BADGES["academic"]
    if src_type == 'video':
        return SOURCE_TIER_BADGES["video"]
    if src_type == 'official':
        return SOURCE_TIER_BADGES["official"]
    if src_type == 'community':
        return SOURCE_TIER_BADGES["community"]
    if tier == 1:
        # tier 1 非 paper/video → 学术/研究源
        return SOURCE_TIER_BADGES["academic"]
    return SOURCE_TIER_BADGES["media"]


def _trust_badge(item):
    """信任信号徽章: 多源证实 / 官方单源 / 单源 → (css_class, text)。

    准确性是核心: 让读者一眼判断这条的可信度。数据来自 _evidence_chain(多源去重)
    + source_tier。多源相互印证最可信; 单源(非官方)提示"自行多看一眼"。
    """
    ev = item.get('_evidence_chain', []) or []
    srcs = {(e.get('source_name') or '').strip() for e in ev}
    srcs.add((item.get('source_name') or '').strip())
    srcs.discard('')
    n = len(srcs)
    if n >= 3:
        return ('trust-strong', f'✅ {n} 源证实')
    if n == 2:
        return ('trust-ok', '✅ 2 源证实')
    if item.get('source_tier', 2) == 0:
        return ('trust-ok', '🏢 官方单源')
    return ('trust-weak', '◦ 单源')


def _days_badge(item):
    """跨日持续事件徽章: 事件首次入库早于今天 → '🔁 第N天' (此事是老事件的新进展)。

    数据 _first_seen 由 briefing_renderer 从 canonical_events.first_seen_at 注入。
    当天新事件(绝大多数)返回 '' 不显示——徽章只给"持续追踪中"的少数事件提供连续感。
    """
    fs = str(item.get('_first_seen') or '').strip()
    if len(fs) < 10:
        return ''
    try:
        from datetime import datetime as _dt, timezone as _tz
        d0 = _dt.fromisoformat(fs.replace('Z', '+00:00'))
        if d0.tzinfo is None:
            d0 = d0.replace(tzinfo=_tz.utc)
        # 两侧统一本地(北京)日: AM 班渲染在北京 06:0x = UTC 前一日 22:0x,
        # 若左侧取 UTC 日期会系统性少 1 天(『第2天』徽章在早班永不出现)。
        days = (_dt.now(_tz.utc).astimezone().date() - d0.astimezone().date()).days
    except (ValueError, TypeError):
        return ''
    if days < 1:
        return ''
    return (f'<span class="days-badge" title="此事件 {days} 天前首次报道, 今天有新进展">'
            f'🔁 第 {days + 1} 天</span>')


# ── MECE 主题树: 单维度(新闻核心主题域)、互斥、穷尽。每条归到唯一叶子。──
# 页面按顶层 6 域分组, 叶子作为卡片子标签。(emoji, 域名, key, [叶子...])
_TOPIC_TREE = [
    ('🧠', '模型与算法',    'model',    ['旗舰模型', '开源模型与权重', '训练·算法·架构', '多模态与专用模型']),
    ('⚙️', '算力与基础设施', 'infra',    ['芯片与硬件', '云·数据中心·能源', '推理·部署·优化']),
    ('🛠️', '应用与产品',    'app',      ['智能体 Agent', '编程与开发', '企业·行业应用', '消费级产品', '具身·机器人·自动驾驶']),
    ('🔬', '研究与评测',    'research', ['前沿论文', '评测·基准', '安全·对齐研究']),
    ('📜', '治理与安全',    'gov',      ['政策·监管·法律', '安全事件·风险·滥用', '伦理·社会影响']),
    ('💼', '商业与产业',    'biz',      ['融资·投资', '并购·合作·商业策略', '市场·产业格局', '人事·组织']),
]
_DOMAIN_ORDER = [(e, n, k) for e, n, k, _ in _TOPIC_TREE]
_DOMAIN_BY_NAME = {n: (e, n, k) for e, n, k, _ in _TOPIC_TREE}
_DOMAIN_BY_KEY = {k: (e, n, k) for e, n, k, _ in _TOPIC_TREE}
_LEAF_TO_DOMAIN = {leaf: (e, n, k) for e, n, k, leaves in _TOPIC_TREE for leaf in leaves}
_OTHER_DOMAIN = ('📰', '其他', 'other')

# 兜底: LLM 没给合法 topic_domain 时, 从 categories+标题 关键词归域(保证不漏)。
# 顺序=优先级: 法律/治理信号最特异放最前, 再商业/算力/研究, 应用次之, 模型最泛放最后。
# 不中时默认归 app(见 _domain_of) —— 永不返回"其他", 让 6 域真正穷尽(用户 2026-06-13)。
_DOMAIN_KEYWORDS = [
    ('gov',      ['政策', '监管', '治理', '合规', '伦理', '滥用', '审查', '军事', '隐私', '版权',
                  '法案', '诉讼', '起诉', '法院', '裁定', '行政令', '政治', '献金', '否决', '国会',
                  '白宫', '选举', '制裁', '反垄断', '工会', '罢工', '立法', '听证', '封禁', '出口管制']),
    ('biz',      ['融资', '投资', '估值', '并购', '收购', '商业', '营收', '市场', 'ipo', '人事',
                  '裁员', '招聘', '上市', '经济', 'gdp', '股份', '持股', '募资', '基金', '创投',
                  '出海', '支付', '预算', '开支', '咨询', '顾问', '利润', '股价', '发债', '借款',
                  '热潮', '格局', '营收', '增长', '商业模式']),
    ('infra',    ['芯片', '算力', 'gpu', 'tpu', '硬件', '数据中心', '半导体', '能源', '集群',
                  '显卡', '超算', '超级计算', '机房', '带宽', '储能', '光纤', '产能', 'asic', '算子']),
    ('research', ['论文', '研究', '学术', 'benchmark', '基准', '评测', '对齐', '理论', 'arxiv',
                  '框架', '指标', '评估', '检测', '压缩', '微调', '神经网络', '张量', '蒸馏',
                  '数据集', '技术报告', '综述', '圆桌', '攻击']),
    ('app',      ['应用', '产品', 'agent', '智能体', '编程', '工具', '驾驶', '机器人', '具身',
                  '场景', '企业', '消费', '插件', 'codex', 'copilot', '助手', '功能', '集成',
                  '客服', '医疗', '教育', '浏览器', '订阅', '软件', '课程']),
    ('model',    ['大模型', '模型', '开源', '权重', '训练', '算法', '架构', '多模态', '视觉',
                  '语音', '视频', '参数']),
]


def _domain_of(item):
    """返回 (emoji, 域名, key) —— 每条唯一主题域(MECE)。
    优先用 LLM 的 topic_domain(单叶, 最准); 否则 叶子→域 / categories+标题 关键词兜底。"""
    a = item.get('analysis', {}) or {}
    d = (a.get('topic_domain') or '').strip()
    if d in _DOMAIN_BY_NAME:
        return _DOMAIN_BY_NAME[d]
    leaf = (a.get('topic_leaf') or '').strip()
    if leaf in _LEAF_TO_DOMAIN:
        return _LEAF_TO_DOMAIN[leaf]
    # 标题里若有 6 域名直接出现(LLM 偶尔把域名塞进 leaf 或 title), 也认
    title = str(a.get('chinese_title') or '')
    blob = (' '.join(str(c) for c in (a.get('categories') or [])) + ' '
            + title + ' ' + leaf).lower()
    for key, kws in _DOMAIN_KEYWORDS:
        if any(k in blob for k in kws):
            return _DOMAIN_BY_KEY[key]
    # 永不返回"其他"(用户 2026-06-13: 不要模糊大类)。关键词全不中的多是行业杂项/动态,
    # 默认归"应用与产品"(AI 新闻最高频的真实类别、最泛的筐); 治本靠 prompt 强制 LLM 归 6 域。
    return _DOMAIN_BY_KEY['app']


def _card_img_html(item, base_cls):
    """卡片配图块: 设计感占位图打底, 有图则叠加真图。

    - 占位图(底层): 域配色渐变 + 大图标 + 关键词(域名/最短叶子类), 每卡都有视觉。
    - 真图(上层 <img>): 加载成功盖住占位图; **加载失败(死链/403 防盗链)onerror 自移除,
      自动露出底层占位图** —— 治"有图块却空白"(用户 2026-06-14: 晚报很多卡有空位)。
    base_cls = 'featured-img' / 'card-img'(复用尺寸); 配色见 style.css .ph-{key} / .cimg-real。
    """
    emoji, name, key = _domain_of(item)
    kw = name
    cats = (item.get('analysis', {}) or {}).get('categories') or item.get('categories') or []
    for c in cats:
        c = str(c).strip()
        if 0 < len(c) <= 6:   # 用够短的叶子类做关键词, 否则退域名
            kw = c
            break
    url = (item.get('image') or '').strip()
    overlay = ''
    if url:
        overlay = (f'<img class="cimg-real" src="{_safe_escape(url)}" alt="" '
                   f'loading="lazy" onerror="this.remove()">')
    return (f'<div class="{base_cls} card-img-ph ph-{key}" aria-hidden="true">'
            f'<span class="cimg-ph-ico">{emoji}</span>'
            f'<span class="cimg-ph-kw">{_safe_escape(kw)}</span>{overlay}</div>')


def _domain_key(item):
    """卡片 data-layer/筛选用的域 key。"""
    return _domain_of(item)[2]


def _leaf_of(item):
    """该条的叶子(子类)名 —— LLM 的 topic_leaf, 否则退回首个 category。"""
    a = item.get('analysis', {}) or {}
    leaf = (a.get('topic_leaf') or '').strip()
    if leaf:
        return leaf
    for c in (a.get('categories') or []):
        if c and c != '其他':
            return str(c)
    return ''


def _dim_tags(item):
    """卡片子标签: 叶子(细类, 可点筛选)。域已作为分组小标题、来源类型走 tier 徽章, 不重复。"""
    leaf = _leaf_of(item)
    if not leaf:
        return ''
    le = _safe_escape(leaf)
    return (f'<span class="dim dim-topic" data-fcat="{le}" '
            f'role="button" tabindex="0" title="只看{le}">{le}</span>')


def _importance_dots(level):
    """生成重要性圆点 HTML — Tufte 风格，最小有效差异"""
    filled = min(max(level, 1), 5)
    colors = {1: '#555', 2: '#888', 3: '#c9a227', 4: '#e8913a', 5: '#e05252'}
    active_color = colors.get(filled, '#888')
    return ''.join(
        f'<span class="imp-dot" style="color:{active_color if i < filled else "rgba(255,255,255,0.12)"}">'
        f'{"●" if i < filled else "○"}</span>'
        for i in range(5)
    )


def _importance_label(level):
    """重要性文字标签"""
    labels = {1: "一般", 2: "关注", 3: "重要", 4: "很重要", 5: "重大事件"}
    return labels.get(level, "")


def _merge_small_groups(groups, min_size=3, catchall='其他'):
    """把卡片数 < min_size 的类别并入 catchall，避免"1-2 张就单起一个小标题"导致版面过碎。

    实测一版"今日必读"40 张被拆成 11 个分类、其中 7 个只有 1-2 张，反而更难读。
    合并后只保留有规模的主题分组 + 一个兜底"其他"。groups 是 {类别: [card_html,...]}。
    """
    small = [c for c, cards in list(groups.items())
             if c != catchall and len(cards) < min_size]
    for c in small:
        groups.setdefault(catchall, []).extend(groups.pop(c))
    return groups


def generate_html(all_items, config, digest=None, meta=None):
    """生成重要性分层的 HTML 页面 — 必读区块 + 普通卡片网格

    meta: dict, optional — 可选的页面级元信息。支持字段:
        - llm_coverage (float, 0..1): LLM 深度分析覆盖率，< 0.5 时渲染页首 banner
        - llm_count (int): 有 LLM 分析的条目数
        - multi_source_count (int): 多源报道的事件数
    """
    now = datetime.now()
    date_str = now.strftime("%Y年%m月%d日")
    weekday_map = {0: '一', 1: '二', 2: '三', 3: '四', 4: '五', 5: '六', 6: '日'}
    weekday = weekday_map[now.weekday()]
    time_str = now.strftime("%H:%M")

    # 标题随班次切换：早班 → AI 早报；晚班 → AI 晚报；无 shift → AI 早报（兼容默认）
    import os as _os_briefing
    _shift = _os_briefing.environ.get('BRIEFING_SHIFT', '').lower()
    if _shift == 'pm':
        briefing_title = 'AI 晚报'
        briefing_emoji = '🌆'
    else:
        briefing_title = 'AI 早报'
        briefing_emoji = '📡'

    # 统计
    by_source = {}
    for item in all_items:
        src = item['source_name']
        by_source.setdefault(src, []).append(item)

    total = len(all_items)
    sources_count = len(by_source)

    avg_importance = 0
    imp_items = [i for i in all_items if i.get('analysis', {}).get('importance', 0) > 0]
    if imp_items:
        avg_importance = sum(i['analysis']['importance'] for i in imp_items) / len(imp_items)

    # 收集分类 + 读者
    all_categories = set()
    all_audiences = set()
    for item in all_items:
        for cat in item.get('analysis', {}).get('categories', []):
            all_categories.add(cat)
        for aud in item.get('analysis', {}).get('audience', []) or []:
            if aud in AUDIENCE_LABELS:
                all_audiences.add(aud)

    # ── 拆分为 featured（importance >= 4）和 regular ──
    featured_items = []
    regular_items = []
    modal_data = []

    # featured 门槛 ≥3（"重要"级别）— 避免 LLM 低估新版模型发布等条目（ChatGPT 5.4
    # 被打 ★3 等）。门槛降低后用排序把真正重大的顶到前面。
    today_str = now.strftime('%Y-%m-%d')
    for idx, item in enumerate(all_items):
        analysis = item.get('analysis', {})
        importance = analysis.get('importance', 1)

        if importance >= 3:
            featured_items.append((idx, item))
        else:
            regular_items.append((idx, item))

    def _is_today(item):
        """item.published 是否今天（按生成时的本地日期）"""
        pub = item.get('published')
        if not pub:
            return False
        try:
            if hasattr(pub, 'strftime'):
                return pub.strftime('%Y-%m-%d') == today_str
            if isinstance(pub, str) and len(pub) >= 10:
                return pub[:10] == today_str
        except (ValueError, TypeError, AttributeError):
            pass
        return False

    def _feat_key(pair):
        _idx, item = pair
        a = item.get('analysis', {}) or {}
        imp = a.get('importance', 1)
        # 多源 bonus（按 unique source 数）
        evidence_chain = item.get('_evidence_chain', []) or []
        unique_srcs = len({ev.get('source_name', '') for ev in evidence_chain if ev.get('source_name')})
        if unique_srcs == 0:
            unique_srcs = 1
        multi_bonus = 0.0
        if unique_srcs >= 4: multi_bonus = 1.0
        elif unique_srcs == 3: multi_bonus = 0.8
        elif unique_srcs == 2: multi_bonus = 0.5
        # 今日 bonus：新鲜度加权
        today_bonus = 0.7 if _is_today(item) else 0.0
        return -(imp + multi_bonus + today_bonus)
    featured_items.sort(key=_feat_key)

    # ── 必读"少而精"：按分排序后只取 Top N 进必读，其余降到"更多"(保留分类) ──
    # 不动 importance≥3 的入选门槛(不漏 LLM 低估的)，只限制必读的展示量；被降级的
    # 条目带着 categories 回流 regular，"更多"的主题分布也随之更丰富(不再 77% 堆在其他)。
    FEATURED_CAP = int((config.get('settings', {}) or {}).get('featured_cap', 10))
    if len(featured_items) > FEATURED_CAP:
        _demoted = featured_items[FEATURED_CAP:]
        featured_items = featured_items[:FEATURED_CAP]
        regular_items = _demoted + regular_items   # prepend: 重要的排在各自分类组前面

    # ── "更多资讯"也设上限(用户 2026-06-12: 每天推送的条目偏多, 过滤一些) ──
    # 按 (importance, 多源数) 排序取 Top N, 长尾直接不上页 —— 它们仍进长期归档
    # (archive/data)与仪表盘, 可搜可查, 只是不再占读者注意力。
    GRID_CAP = int((config.get('settings', {}) or {}).get('grid_cap', 10))
    if len(regular_items) > GRID_CAP:
        regular_items.sort(key=lambda t: (
            -(t[1].get('analysis', {}) or {}).get('importance', 0),
            -(t[1].get('_cluster_size', 1) or 1)))
        _grid_dropped = len(regular_items) - GRID_CAP
        regular_items = regular_items[:GRID_CAP]
    else:
        _grid_dropped = 0

    # ── 今日三件大事：importance >= 4 且 cluster_size >= 2 的前 3 条 ──
    top3_items = []
    seen_idx = set()
    # 优先：importance=5 且多源
    for idx, item in featured_items:
        if len(top3_items) >= 3:
            break
        if item.get('analysis', {}).get('importance', 0) == 5 and item.get('_cluster_size', 1) >= 2:
            top3_items.append((idx, item))
            seen_idx.add(idx)
    # 补：importance=4 且多源
    for idx, item in featured_items:
        if len(top3_items) >= 3:
            break
        if idx in seen_idx:
            continue
        if item.get('analysis', {}).get('importance', 0) >= 4 and item.get('_cluster_size', 1) >= 2:
            top3_items.append((idx, item))
            seen_idx.add(idx)
    # 再补：importance >= 4（不要求多源）
    if len(top3_items) < 3:
        for idx, item in featured_items:
            if len(top3_items) >= 3:
                break
            if idx in seen_idx:
                continue
            top3_items.append((idx, item))
            seen_idx.add(idx)

    # ── 类别顺序（featured 必读 与 regular 更多资讯 共用，统一主题分组）──
    _GRID_CAT_ORDER = [
        ('大模型发布', '🧠'), ('开源生态', '🔓'), ('学术研究', '🔬'), ('AI编程', '💻'),
        ('AI工具', '🛠️'), ('产品与应用', '📦'), ('芯片与算力', '⚡'), ('融资与商业', '💰'),
        ('AI政策监管', '📜'), ('安全与对齐', '🛡️'), ('具身智能', '🤖'), ('自动驾驶', '🚗'),
        ('行业观点', '💬'), ('其他', '📰'),
    ]
    _cat_rank = {c: i for i, (c, _) in enumerate(_GRID_CAT_ORDER)}

    # ── 构建必读卡片（featured block）—— 按主题分组，每组一个小标题（与"更多资讯"同款）──
    featured_html = ""
    _feat_groups = {}   # {主类别: [featured_card_html, ...]}，组内保持 relevance 排序
    if featured_items:
        for idx, item in featured_items:
            analysis = item.get('analysis', {})
            importance = analysis.get('importance', 1)

            # 中文标题处理
            chinese_title_raw = analysis.get('chinese_title', '') or ''
            raw_summary = analysis.get('summary', '') or ''
            if not raw_summary or raw_summary in ('无法提取摘要', '无法获取分析'):
                raw_summary = item.get('title', '')[:100]
            _is_entity_label = (
                chinese_title_raw
                and '|' in chinese_title_raw
                and len(chinese_title_raw) < 40
                and not any('\u4e00' <= c <= '\u9fff' for c in chinese_title_raw)
            )
            if not chinese_title_raw or _is_entity_label:
                fallback_src = raw_summary or item.get('title', '') or ''
                if len(fallback_src) <= 50:
                    chinese_title_raw = fallback_src
                else:
                    truncated = fallback_src[:50]
                    for sep in ['。', '，', '；', '. ', ', ', '; ', ' ']:
                        last_sep = truncated.rfind(sep)
                        if last_sep > 15:
                            chinese_title_raw = truncated[:last_sep + len(sep)].rstrip()
                            break
                    else:
                        chinese_title_raw = truncated.rstrip()

            chinese_title = _safe_escape(chinese_title_raw)
            # 卡片显示客观事实陈述(summary),不显示意义解读(why_it_matters)。
            # 后者跟 modal 里的 deep_analysis 内容重叠,留给 modal 展开看。
            card_summary = _safe_escape(analysis.get('summary', '') or raw_summary)
            categories = analysis.get('categories', ['其他'])

            pub_str = _format_local_time(item.get('published'))

            icon = item.get('source_icon', '📰')
            source_name = _safe_escape(item.get("source_name", ""))
            image_url = _safe_escape(item.get("image", ""))
            cat_data = '|'.join(categories)

            # 左侧边条颜色
            border_color = '#e05252' if importance == 5 else '#e8913a'

            # 图片区域: 占位图打底 + 真图叠加(失败自动露占位图), 每卡都有视觉、永不空白
            img_html = _card_img_html(item, 'featured-img')

            # 标题
            title_html = f'<div class="featured-title-text">{chinese_title}</div>'

            # 卡片简介:用 summary(事实)而不是 why_it_matters(意义),后者留给 modal
            why_html = f'<div class="featured-why">{card_summary}</div>' if card_summary else ""

            # 来源徽章 + 时间
            tb = _source_badge(item)
            tier_badge_html = (
                f'<span class="tier-badge tier-{tb[1]}" title="{tb[1]}源">{tb[0]} {tb[1]}</span>'
                if tb else ''
            )
            time_part = f' · {pub_str}' if pub_str else ''

            _ftb = _trust_badge(item)
            _ftrust = f'<span class="trust-badge {_ftb[0]}">{_ftb[1]}</span> ' if _ftb else ''
            _fdays = _days_badge(item)
            src_html = (
                f'<div class="featured-source">'
                f'{_ftrust}{_fdays}{tier_badge_html} {icon} {source_name}{time_part}'
                f'</div>'
            )

            # 多源详细列表：列出所有 evidence 的"其他媒体"报道（去重，同源多篇合并）
            # 注意：cluster_size 数的是 evidence 条数，同一家媒体发多篇也会 >=2，
            # 但"多源"业务语义应是 unique source > 1，所以这里按 source_name 去重。
            other_sources_html = ''
            evidence_chain = item.get('_evidence_chain', []) or []
            canonical_source = item.get('source_name', '') or ''
            others_map = {}  # source_name → earliest reported_at
            for ev in evidence_chain:
                sn = ev.get('source_name', '') or ''
                if not sn or sn == canonical_source:
                    continue
                ra = ev.get('reported_at', '') or ''
                # 同源保留最早时间
                if sn not in others_map or (ra and ra < others_map[sn]):
                    others_map[sn] = ra
            if others_map:
                def _fmt(ra):
                    try:
                        if ra:
                            return datetime.fromisoformat(ra.replace('Z', '+00:00')).strftime('%m-%d %H:%M')
                    except (ValueError, TypeError):
                        pass
                    return (ra[:16].replace('T', ' ')) if ra else ''
                rows = ''.join(
                    f'<div class="fos-item">· {_safe_escape(sn)}'
                    f'{" · " + _safe_escape(_fmt(ra)) if _fmt(ra) else ""}</div>'
                    for sn, ra in others_map.items()
                )
                other_sources_html = (
                    f'<div class="featured-other-sources">'
                    f'<div class="fos-label">📡 另有 {len(others_map)} 源报道</div>'
                    f'{rows}'
                    f'</div>'
                )
            multi_pill = ''  # 兼容下方模板字符串引用

            # importance=5 的 "行业级" 徽章（置于卡片顶部，左侧）
            hero_badge = ''
            if importance == 5:
                hero_badge = '<span class="hero-badge" title="行业格局级">🚀 行业级</span>'

            # data-audience 属性，供前端 tab 过滤
            aud_list = analysis.get('audience', []) or ['general']
            aud_data = '|'.join(a for a in aud_list if a in AUDIENCE_LABELS) or 'general'

            # 优先用 canonical_event_id —— dashboard"本周重要事件"深链(#evt-)用的就是它,
            # 必须一致才能滚到卡片/展开 modal。退回 _event_id(文章id) / link。
            _iid = item.get('_canonical_event_id') or item.get('_event_id') or item.get('link') or f'i{idx}'
            _feat_card = f'''    <div class="featured-card" data-cat="{_safe_escape(_leaf_of(item))}" data-layer="{_domain_key(item)}" data-aud="{_safe_escape(aud_data)}" data-idx="{idx}" data-iid="{_safe_escape(_iid)}" style="border-left-color: {border_color}">
        {img_html}
        <div class="featured-body">
            {hero_badge}
            {multi_pill}
            <div class="featured-dims">{_dim_tags(item)}</div>
            {title_html}
            {why_html}
            {src_html}
            {other_sources_html}
        </div>
    </div>
'''
            _, _flname, _ = _domain_of(item)   # 按层面分组(粗维度, 不碎)
            _feat_groups.setdefault(_flname, []).append(_feat_card)

        # 按层面顺序拼接, 每层一个小标题(可点筛选), 组内独立 featured-grid
        _fparts = ['<div class="featured-section">\n<h2 class="featured-title">⭐ 今日必读</h2>\n']
        for _femoji, _flname, _flkey in _DOMAIN_ORDER:
            _fcards = _feat_groups.get(_flname)
            if not _fcards:
                continue
            _fparts.append(
                f'<h3 class="grid-cat-head gch-{_flkey}" data-flayer="{_flkey}" role="button" '
                f'title="只看{_flname}">{_femoji} {_flname}'
                f'<span class="gc-n">{len(_fcards)}</span></h3>\n'
                '<div class="featured-grid">\n' + ''.join(_fcards) + '</div>\n'
            )
        _fparts.append('</div>\n')
        featured_html = ''.join(_fparts)

    # ── 构建普通卡片（regular grid）—— 按主类别分组，每组一个全宽小标题 ──
    # _GRID_CAT_ORDER / _cat_rank 已在 featured block 上方定义（两处共用，统一主题归类）
    _grid_groups = {}   # {主类别: [card_html, ...]}，组内保持原(relevance)排序
    for idx, item in regular_items:
        analysis = item.get('analysis', {})

        # 中文标题处理
        chinese_title_raw = analysis.get('chinese_title', '') or ''
        raw_summary = analysis.get('summary', '') or ''
        if not raw_summary or raw_summary in ('无法提取摘要', '无法获取分析'):
            raw_summary = item.get('title', '')[:100]
        _is_entity_label = (
            chinese_title_raw
            and '|' in chinese_title_raw
            and len(chinese_title_raw) < 40
            and not any('\u4e00' <= c <= '\u9fff' for c in chinese_title_raw)
        )
        if not chinese_title_raw or _is_entity_label:
            fallback_src = raw_summary or item.get('title', '') or ''
            if len(fallback_src) <= 50:
                chinese_title_raw = fallback_src
            else:
                truncated = fallback_src[:50]
                for sep in ['。', '，', '；', '. ', ', ', '; ', ' ']:
                    last_sep = truncated.rfind(sep)
                    if last_sep > 15:
                        chinese_title_raw = truncated[:last_sep + len(sep)].rstrip()
                        break
                else:
                    chinese_title_raw = truncated.rstrip()

        chinese_title = _safe_escape(chinese_title_raw)
        # 卡片用 summary(事实陈述)替代 why_it_matters(意义),
        # 避免和 modal 的 deep_analysis 重复。
        card_summary = _safe_escape(analysis.get('summary', '') or raw_summary)
        categories = analysis.get('categories', ['其他'])
        source_type = analysis.get('source_type', 'news')
        reading_minutes = analysis.get('reading_minutes', 1)

        pub_str = _format_local_time(item.get('published'))

        icon = item.get('source_icon', '📰')
        source_name = _safe_escape(item.get("source_name", ""))
        cat_data = '|'.join(categories)
        image_url = _safe_escape(item.get("image", ""))

        # Z1: 多维标签(层面 + 主题) + 阅读时间。来源类型走下方 tier 徽章, 不重复。
        z1_html = f'''<div class="z1">
            <span class="z1-left">{_dim_tags(item)}</span>
            <span class="z1-meta">{reading_minutes} min</span>
        </div>'''

        # 图片区域: 占位图打底 + 真图叠加(失败自动露占位图), 每卡都有视觉、永不空白
        img_html = _card_img_html(item, 'card-img')

        # 标题
        title_html = f'<div class="card-title">{chinese_title}</div>'

        # 卡片简介用 summary(事实陈述),不重复 modal 里的 deep_analysis(意义解读)
        z3_html = ""
        if card_summary:
            z3_html = f'<div class="z3"><div class="z3-why">{card_summary}</div></div>'

        # Z5: 来源徽章 + 来源 + 时间 + 事件状态（多源指示提升为独立 pill，见下方）
        tb = _source_badge(item)
        tier_badge_html = (
            f'<span class="tier-badge tier-{tb[1]}" title="{tb[1]}源">{tb[0]}</span>'
            if tb else ''
        )
        time_part = f' · {pub_str}' if pub_str else ''
        status_badge = ''
        if item.get('_event_status_display'):
            st = item['_event_status_display']
            status_badge = f' <span style="color:{st.get("color","#888")};font-size:9px;font-weight:600">{st.get("icon","")} {st.get("label","")}</span>'
        _tb = _trust_badge(item)
        trust_html = f'<span class="trust-badge {_tb[0]}">{_tb[1]}</span> ' if _tb else ''
        _dbadge = _days_badge(item)
        z5_html = f'<div class="z5">{trust_html}{_dbadge}{tier_badge_html} {icon} {source_name}{time_part}{status_badge}</div>'

        # 多源报道 pill（普通卡片右上角）
        cluster_size = item.get('_cluster_size', 1) or 1
        report_count = item.get('_report_count', cluster_size) or cluster_size
        multi_pill = ''
        if cluster_size >= 2 or report_count >= 2:
            n = max(cluster_size, report_count)
            multi_pill = f'<span class="src-pill" title="共 {n} 个来源报道同一事件">📡 {n} 源</span>'

        # 外部热度信号 pill (P2): HN/Reddit/HF/GitHub 关联结果
        ext_signals = item.get('_external_signals') or {}
        hot_pills_html = ''
        if ext_signals:
            pill_parts = []
            if 'hn' in ext_signals:
                pts = int(ext_signals['hn'].get('points') or 0)
                hn_url = _safe_escape(ext_signals['hn'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-hn" target="_blank" rel="noopener" '
                    f'href="{hn_url}" title="HN {pts} 分">🔥 HN {pts}</a>'
                )
            if 'reddit' in ext_signals:
                sub = _safe_escape(ext_signals['reddit'].get('subreddit') or 'reddit')
                r_url = _safe_escape(ext_signals['reddit'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-reddit" target="_blank" rel="noopener" '
                    f'href="{r_url}" title="r/{sub} hot">💬 r/{sub}</a>'
                )
            if 'hf' in ext_signals:
                likes = int(ext_signals['hf'].get('likes') or 0)
                hf_url = _safe_escape(ext_signals['hf'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-hf" target="_blank" rel="noopener" '
                    f'href="{hf_url}" title="HuggingFace {likes} 个 likes">⭐ HF {likes}</a>'
                )
            if 'github' in ext_signals:
                stars = int(ext_signals['github'].get('stars') or 0)
                stars_disp = f'{stars // 1000}K' if stars >= 1000 else str(stars)
                gh_url = _safe_escape(ext_signals['github'].get('url') or '#')
                pill_parts.append(
                    f'<a class="hot-pill hp-gh" target="_blank" rel="noopener" '
                    f'href="{gh_url}" title="GitHub {stars} stars">🐙 {stars_disp}</a>'
                )
            if pill_parts:
                hot_pills_html = '<div class="hot-pills">' + ''.join(pill_parts) + '</div>'

        # data-audience 属性
        aud_list = analysis.get('audience', []) or ['general']
        aud_data = '|'.join(a for a in aud_list if a in AUDIENCE_LABELS) or 'general'

        _riid = item.get('_canonical_event_id') or item.get('_event_id') or item.get('link') or f'r{idx}'
        _card_html = f'''
        <div class="card" data-cat="{_safe_escape(_leaf_of(item))}" data-layer="{_domain_key(item)}" data-aud="{_safe_escape(aud_data)}" data-idx="{idx}" data-iid="{_safe_escape(_riid)}"
             style="animation-delay:{min(idx * 25, 500)}ms">
            {img_html}
            <div class="card-body">
                {multi_pill}
                {z1_html}
                {title_html}
                {z3_html}
                {hot_pills_html}
                {z5_html}
            </div>
        </div>'''
        _, _glname, _ = _domain_of(item)   # 按层面分组(粗维度, 不碎)
        _grid_groups.setdefault(_glname, []).append(_card_html)

    # 按域成块拼接(用户 2026-06-13: 瀑布流里分类分割不明显 —— 多列布局会让一个
    # 分类的卡片"流"到下一列、标题与内容对不上)。每个域一个独立 .grid-group 区块:
    # 彩色重标题(gch-{key} 左色条) + 该域自己的卡片网格(.grid-cards), 分割一目了然。
    cards_html = ""
    for _emoji, _glname, _glkey in _DOMAIN_ORDER:   # _domain_of 永不返回"其他", 6 域穷尽
        _cards = _grid_groups.get(_glname)
        if not _cards:
            continue
        cards_html += (
            f'<section class="grid-group">'
            f'<h3 class="grid-cat-head gch-{_glkey}" data-flayer="{_glkey}" role="button" '
            f'title="只看{_glname}">{_emoji} {_glname}'
            f'<span class="gc-n">{len(_cards)}</span></h3>'
            f'<div class="grid-cards">' + ''.join(_cards) + '</div>'
            '</section>'
        )

    # ── 为所有卡片构建 modal_data ──
    # 必须按 all_items 原顺序遍历，因为卡片的 data-idx 用的是 enumerate(all_items) 的 idx。
    # 如果按 featured+regular 顺序 append 会导致 modal_data[idx] 与 data-idx 错位（featured 卡点开显示错条目）。
    for idx, item in enumerate(all_items):
        analysis = item.get('analysis', {})

        chinese_title_raw = analysis.get('chinese_title', '') or ''
        raw_summary = analysis.get('summary', '') or ''
        if not raw_summary or raw_summary in ('无法提取摘要', '无法获取分析'):
            raw_summary = item.get('title', '')[:100]
        _is_entity_label = (
            chinese_title_raw
            and '|' in chinese_title_raw
            and len(chinese_title_raw) < 40
            and not any('\u4e00' <= c <= '\u9fff' for c in chinese_title_raw)
        )
        if not chinese_title_raw or _is_entity_label:
            fallback_src = raw_summary or item.get('title', '') or ''
            if len(fallback_src) <= 50:
                chinese_title_raw = fallback_src
            else:
                truncated = fallback_src[:50]
                for sep in ['。', '，', '；', '. ', ', ', '; ', ' ']:
                    last_sep = truncated.rfind(sep)
                    if last_sep > 15:
                        chinese_title_raw = truncated[:last_sep + len(sep)].rstrip()
                        break
                else:
                    chinese_title_raw = truncated.rstrip()

        categories = analysis.get('categories', ['其他'])
        source_type = analysis.get('source_type', 'news')
        reading_minutes = analysis.get('reading_minutes', 1)

        pub_str = _format_local_time(item.get('published'))

        icon = item.get('source_icon', '📰')
        source_name = _safe_escape(item.get("source_name", ""))

        orig_title = item.get('title', '')
        if not analysis.get('detailed_content') and orig_title and len(orig_title) > 60:
            analysis_copy = dict(analysis)
            analysis_copy['detailed_content'] = orig_title
            analysis = analysis_copy

        # 源 tier 徽章
        tb_m = _source_badge(item)
        modal_entry = {
            "title": orig_title,
            "chinese_title": chinese_title_raw,
            "summary": raw_summary,
            "why_it_matters": analysis.get('why_it_matters', ''),
            "key_details": analysis.get('key_details', []),
            "detailed_content": analysis.get('detailed_content', ''),
            "background": analysis.get('background', ''),
            "deep_analysis": analysis.get('deep_analysis', ''),
            "importance": analysis.get('importance', 1),
            "categories": categories,
            "source_type": source_type,
            "source_name": source_name,
            "source_icon": icon,
            "source_badge_icon": tb_m[0] if tb_m else '',
            "source_badge_label": tb_m[1] if tb_m else '',
            "link": item.get('link', '#'),
            "pub_date": pub_str,
            "image": item.get('image', ''),
            "extra_images": item.get('extra_images', []),
            "reading_minutes": reading_minutes,
            "audience": analysis.get('audience', ['general']) or ['general'],
            "item_id": item.get('_canonical_event_id', '') or item.get('_event_id', '') or item.get('link', '') or f'i{idx}',
        }
        if analysis.get('causal_matches'):
            modal_entry['causal_matches'] = analysis['causal_matches']
            modal_entry['impact_summary'] = analysis.get('impact_summary', '')
        if item.get('_event_status_display'):
            modal_entry['event_status'] = item['_event_status']
            modal_entry['event_status_label'] = item['_event_status_display'].get('label', '')
            modal_entry['event_status_icon'] = item['_event_status_display'].get('icon', '')
        if item.get('_also_reported_by'):
            modal_entry['also_reported_by'] = item['_also_reported_by']
            modal_entry['report_count'] = item.get('_report_count', 0)
        modal_data.append(modal_entry)

    modal_json = json.dumps(modal_data, ensure_ascii=False)
    modal_js_content = f"const __data = {modal_json};"

    # 筛选按钮（分类）
    filter_html = '<button class="f-btn active" data-filter="all">全部</button>\n'
    ordered_cats = ['大模型发布', '开源生态', 'AI 政策监管', '芯片与算力', '产品与应用',
                    '安全与对齐', '融资与商业', '学术研究', 'AI 工具', '具身智能',
                    '自动驾驶', 'AI 医疗', 'AI 编程', '行业观点', '其他']
    for cat in ordered_cats:
        if cat in all_categories:
            filter_html += f'<button class="f-btn" data-filter="{_safe_escape(cat)}">{_safe_escape(cat)}</button>\n'

    # 筛选按钮（读者画像）
    audience_filter_html = ''
    if all_audiences:
        audience_filter_html = '<button class="a-btn active" data-audience="all">全部读者</button>\n'
        aud_order = ['researcher', 'developer', 'pm', 'investor', 'general']
        for aud in aud_order:
            if aud in all_audiences:
                label, icon = AUDIENCE_LABELS[aud]
                audience_filter_html += (
                    f'<button class="a-btn" data-audience="{aud}" '
                    f'title="只看对{label}有用的资讯">{icon} {label}</button>\n'
                )

    # 今日三件大事（第一屏）
    top3_html = ''
    if top3_items:
        top3_cards = ''
        for idx, item in top3_items:
            analysis = item.get('analysis', {})
            ct = _safe_escape(analysis.get('chinese_title', '') or item.get('title', '')[:40])
            why = _safe_escape(analysis.get('why_it_matters', '') or analysis.get('summary', '')[:100])
            src = _safe_escape(item.get("source_name", ""))
            icon = item.get('source_icon', '📰')
            cluster = item.get('_cluster_size', 1) or 1
            multi = (f' · <b>📡 {cluster} 源确认</b>' if cluster >= 2 else '')
            imp = analysis.get('importance', 4)
            imp_marker = '🚀' if imp == 5 else '⭐'
            top3_cards += (
                f'<div class="top3-card" data-idx="{idx}">'
                f'<span class="top3-badge">{imp_marker}</span>'
                f'<div class="top3-body">'
                f'<div class="top3-title">{ct}</div>'
                f'<div class="top3-why">{why}</div>'
                f'<div class="top3-meta">{icon} {src}{multi}</div>'
                f'</div>'
                f'</div>'
            )
        top3_html = (
            '<section class="top3-section" aria-label="今日三件大事">'
            '<h2 class="top3-title-h">🗞️ 今日三件大事</h2>'
            f'<div class="top3-grid">{top3_cards}</div>'
            '</section>'
        )

    # 今日速览 — v2 优先渲染 3 个判断卡片, 没有则回退到旧版 editorial
    briefing_html = ""
    judgments = (digest or {}).get('judgments') or []
    if judgments:
        # v2: 3 个判断卡 (产品定位升级: 从"罗列"到"判断")
        headline = _safe_escape((digest.get('headline') or '').strip())
        outro = _safe_escape((digest.get('outro') or '').strip())

        # 注意: 用 j_cards_html 而非 cards_html, 避免与外层"文章卡片列表"的同名变量冲突
        j_cards_html = ''
        for j in judgments[:3]:
            j_emoji = _safe_escape(str(j.get('emoji', '🔹')))
            j_title = _safe_escape(str(j.get('title', '')).strip())
            j_body = _safe_escape(str(j.get('body', '')).strip())
            # 允许 body 内嵌 ** 加粗
            j_body = re.sub(r'\*\*([^*\n]+?)\*\*',
                            r'<strong class="jc-bold">\1</strong>', j_body)

            # 顶部徽章位(预留)。曾挂"⚠ N 处与新闻不符"红旗 —— 2026-06-14 移除: 在旗舰
            # 判断上贴自家核查的矛盾警告是自我拆台、对读者也不可操作。改为上游直接不
            # 发布"与新闻不符"的判断(见 llm_analyzer.generate_digest 的 contradicted 过滤),
            # 读者只看到干净判断; contradicted 仅留内部日志供监控误判率。
            badges_html = ''

            # 一次遍历 evidence_ids 同时算两件事(下标对齐 all_items, briefing_renderer 同一份):
            #   ① 这个判断"综合了哪几个域"(去重取前3) —— 判断是跨域结论
            #   ② 来源链接 → 页内对应卡片锚点(#evt-, _handleEvtHash 滚动+展开), 让"判断←依据"可点直达
            _dom_seen = []
            src_links = []
            seen_src = set()
            for ei in (j.get('evidence_ids') or []):
                if not isinstance(ei, int) or ei < 0 or ei >= len(all_items):
                    continue
                it = all_items[ei]
                _de, _dn, _dk = _domain_of(it)
                if _dk not in [d[2] for d in _dom_seen]:
                    _dom_seen.append((_de, _dn, _dk))
                iid = (it.get('_canonical_event_id') or it.get('_event_id') or it.get('link') or '').strip()
                if not iid or iid in seen_src:
                    continue
                seen_src.add(iid)
                sname = _safe_escape(it.get('source_name', '') or '原文')
                sicon = _safe_escape(str(it.get('source_icon', '') or '🔗'))
                eid_attr = _safe_escape(str(iid))
                src_links.append(
                    f'<a class="jc-src" href="#evt-{eid_attr}" data-iid="{eid_attr}" '
                    f'role="button">{sicon} {sname}</a>'
                )
            # 只露出真实 6 域, 过滤兜底"其他"(它不是用户定的分类, 露出来像噪声)
            dom_chips = ''.join(
                f'<span class="dim dim-{_dk}" data-flayer="{_dk}" role="button" '
                f'title="只看{_dn}">{_de} {_dn}</span>'
                for _de, _dn, _dk in _dom_seen[:3] if _dk != 'other'
            )
            src_html = ''
            if src_links:
                src_html = '<div class="jc-sources">📎 依据(点开看详情)：' + ' · '.join(src_links) + '</div>'

            j_cards_html += (
                '<article class="judgment-card">'
                f'<div class="jc-header"><span class="jc-emoji">{j_emoji}</span>'
                f'<span class="jc-doms">{dom_chips}</span>{badges_html}</div>'
                f'<h3 class="jc-title">{j_title}</h3>'
                f'<p class="jc-body">{j_body}</p>'
                f'{src_html}'
                '</article>'
            )

        headline_html = f'<p class="br-headline">{headline}</p>' if headline else ''
        outro_html = f'<p class="br-outro">{outro}</p>' if outro else ''
        n_judgments = len(judgments[:3])
        briefing_html = f'''
    <section class="briefing">
        <h2 class="br-title">📌 今日 {n_judgments} 个判断</h2>
        {headline_html}
        <div class="judgments-grid jcg-{n_judgments}">{j_cards_html}</div>
        {outro_html}
    </section>'''
    elif digest and digest.get('editorial'):
        # 兼容兜底: judgments 缺失时仍渲染旧版 editorial 文本
        editorial_html = _render_editorial(digest.get('editorial', ''))
        if editorial_html:
            briefing_html = f'''
    <section class="briefing">
        <h2 class="br-title">今日速览</h2>
        {editorial_html}
    </section>'''

    # LLM 覆盖率 banner：覆盖率 < 50% 时在页首提示读者"部分内容为规则兜底"
    llm_banner_html = ""
    if meta:
        coverage = meta.get('llm_coverage')
        if coverage is not None and coverage < 0.5 and total > 0:
            pct = int(round(coverage * 100))
            llm_cnt = meta.get('llm_count', 0)
            llm_banner_html = (
                '<div class="llm-banner" role="status">'
                '<span class="llm-banner-icon">⚠️</span>'
                f'<span class="llm-banner-text">今日 LLM 深度分析覆盖率 <b>{pct}%</b>'
                f'（{llm_cnt} / {total} 条），其余为规则兜底内容，质量与标题生成可能简化。</span>'
                '</div>'
            )

    # 实体追踪入口 (P1): 30 天实体动态时间线 chips
    entity_tracker_html = ""
    entity_timelines = (meta or {}).get('entity_timelines') or []
    if entity_timelines:
        chips = []
        for et in entity_timelines:
            chips.append(
                f'<a class="et-chip" href="{_safe_escape(et.get("html_path", "#"))}">'
                f'<span class="et-icon">{_safe_escape(et.get("icon", "🏢"))}</span>'
                f'<span class="et-name">{_safe_escape(et.get("name", ""))}</span>'
                f'<span class="et-count">{int(et.get("count", 0))}</span>'
                f'</a>'
            )
        entity_tracker_html = (
            '<section class="entity-tracker">'
            '<h2 class="et-title">📍 实体追踪 · 过去 30 天动态</h2>'
            '<p class="et-hint">点击进入该公司/人物的 30 天演进时间线</p>'
            '<div class="et-chips">' + ''.join(chips) + '</div>'
            '</section>'
        )

    # ── 大V 动态区块：X-*/YouTube 等社交媒体大V，importance 通常<4 达不到 featured
    # 但仍值得独立展示（观点/访谈/短视频解读）。排除已进 featured 的，避免重复 ──
    # VIP 名单 = config.json 里 "vip": true 的源 + 所有 X-* 推特源 + source_type==video
    _vip_names = set()
    for _lang in ('english', 'chinese'):
        for _s in (config or {}).get('sources', {}).get(_lang, []) or []:
            if _s.get('vip'):
                _vip_names.add(_s.get('name', ''))
    def _is_vip(item):
        name = item.get('source_name', '') or ''
        if name.startswith('X-') or name in _vip_names:
            return True
        return item.get('analysis', {}).get('source_type') == 'video'

    featured_idx_set = {idx for idx, _ in featured_items}
    vip_candidates = []
    for idx, item in enumerate(all_items):
        if idx in featured_idx_set or not _is_vip(item):
            continue
        a = item.get('analysis', {}) or {}
        if a.get('ai_relevant') is False:
            continue
        imp = a.get('importance', 0)
        if imp < 2:
            continue
        vip_candidates.append((idx, item, imp))
    vip_candidates.sort(key=lambda x: -x[2])
    vip_candidates = vip_candidates[:6]   # 用户 2026-06-12: 条目偏多, 收紧

    vip_html = ''
    if vip_candidates:
        _vip_groups = {}   # {主类别: [vip_item_html, ...]}, 与必读/更多同款主题细分
        for idx, item, _imp in vip_candidates:
            a = item.get('analysis', {}) or {}
            ct = _safe_escape(a.get('chinese_title', '') or item.get('title', '')[:70])
            why = _safe_escape(a.get('why_it_matters', '') or (a.get('summary', '') or '')[:110])
            src = _safe_escape(item.get("source_name", ""))
            icon = item.get('source_icon', '🎙️')
            why_html = f'<div class="vip-why">{why}</div>' if why else ''
            _viid = item.get('_canonical_event_id') or item.get('_event_id') or item.get('link') or f'vip{idx}'
            _vcats = a.get('categories') or ['其他']
            _vrow = (
                f'<div class="vip-item" data-cat="{_safe_escape(_leaf_of(item))}" '
                f'data-layer="{_domain_key(item)}" data-aud="general" '
                f'data-idx="{idx}" data-iid="{_safe_escape(_viid)}">'
                f'<div class="vip-meta">{icon} {src}</div>'
                f'<div class="vip-title-txt">{ct}</div>'
                f'{why_html}'
                f'</div>'
            )
            _, _vlname, _ = _domain_of(item)   # 按主题域分组(MECE)
            _vip_groups.setdefault(_vlname, []).append(_vrow)
        # 若只剩一个层面(常见于条目少)就不加多余小标题, 直接平铺
        _single = len(_vip_groups) <= 1
        _vparts = []
        for _vemoji, _vlname, _vlkey in _DOMAIN_ORDER:
            _vrows = _vip_groups.get(_vlname)
            if not _vrows:
                continue
            _head = '' if _single else (
                f'<h3 class="grid-cat-head gch-{_vlkey}" data-flayer="{_vlkey}" role="button" '
                f'title="只看{_vlname}">{_vemoji} {_vlname}<span class="gc-n">{len(_vrows)}</span></h3>'
            )
            _vparts.append(_head + '<div class="vip-list">' + ''.join(_vrows) + '</div>')
        vip_html = (
            '<section class="vip-section" aria-label="大V 动态">'
            '<h2 class="vip-heading">🎙️ 大V 动态</h2>'
            + ''.join(_vparts) +
            '</section>'
        )

    # ── SEO / 分享：description 优先取 digest.editorial，其次拼 top3 标题 ──
    meta_description = ''
    if digest and digest.get('editorial'):
        meta_description = digest['editorial'].strip().replace('\n', ' ')
    elif top3_items:
        titles = [
            (i.get('analysis', {}).get('chinese_title') or i.get('title', ''))[:40]
            for _, i in top3_items
        ]
        meta_description = f"今日共 {total} 条 AI 资讯 · 重点：" + '；'.join(t for t in titles if t)
    else:
        meta_description = f"每日 AI 行业早报 · 共 {total} 条资讯，覆盖 {sources_count} 个信息源"
    # 裁剪到 160 字符（search engine / og 常规上限）+ 双重 escape（meta content 属性）
    meta_description = _safe_escape(meta_description[:157] + ('…' if len(meta_description) > 160 else ''))

    # ══════════════════════════════════════════════════════════
    # 从模板文件组装 HTML
    # ══════════════════════════════════════════════════════════
    css_content = _load_template('style.css')
    js_content = _load_template('script.js')
    page_template = Template(_load_template('page.html'))

    # 口播: 音频播放器"嵌入早报顶部"(用户 2026-06-10: 打开早报即可听), 文字稿留页底。
    # 音频文件在渲染之后由 tts_broadcast.py 生成 —— 按约定路径先引用;
    # preload=metadata 让缺失(TTS 失败/超14天被裁)在页面加载时即触发 onerror 隐藏。
    broadcast_html = ''
    audio_top_html = ''
    _bc = ((meta or {}).get('broadcast_script') or '').strip()
    if _bc:
        _bc_audio = ((meta or {}).get('broadcast_audio') or '').strip()
        if _bc_audio:
            audio_top_html = (
                '<div class="bc-audio-wrap audio-top">'
                f'<audio class="bc-player" controls preload="metadata" '
                f'src="{_safe_escape(_bc_audio)}" '
                "onerror=\"var w=this.closest('.bc-audio-wrap');if(w)w.hidden=true\"></audio>"
                '<p class="bc-hint">🎧 今日音频版 · 约 5-6 分钟听完全天 · 文字稿在页底 🎙</p>'
                '</div>'
            )
        broadcast_html = (
            '<section class="broadcast">'
            '<div class="bc-head"><h2 class="bc-title">🎙 今日口播文字稿</h2>'
            '<button class="bc-copy" type="button" '
            "onclick=\"navigator.clipboard.writeText(document.getElementById('bcText').innerText)"
            ".then(()=>{this.textContent='已复制 ✓'})\">复制文稿</button></div>"
            '<details class="bc-details"><summary>查看文字稿（音频播放器在页面顶部）</summary>'
            f'<pre class="bc-text" id="bcText">{_safe_escape(_bc)}</pre></details>'
            '</section>'
        )

    # ── 今日速览：top N 标题, 30 秒扫完全天, 按「层面」(粗维度)分组。──
    # 用层面而非 14 个细主题: 少而杂的内容(每个细主题常 1 条)按细主题会全碎进"其他",
    # 按 5 个层面则摊得开、每层有好几条, 分组才有意义。放在判断前先拿全貌; 点条目开 modal。
    _glance_groups = {}   # {层面名: [(idx, title), ...]}
    for _gidx, _gitem in featured_items[:8]:   # 速览 10→8(用户: 条目偏多)
        _ga = _gitem.get('analysis', {}) or {}
        _gt = (_ga.get('chinese_title') or _gitem.get('title') or '').strip()
        if not _gt:
            continue
        _, _gname, _ = _domain_of(_gitem)
        _glance_groups.setdefault(_gname, []).append((_gidx, _gt))
    _glance_n = sum(len(v) for v in _glance_groups.values())
    _gparts = []
    for _gemoji, _gname, _gkey in _DOMAIN_ORDER:
        _gitems = _glance_groups.get(_gname)
        if not _gitems:
            continue
        _rows = ''.join(
            f'<li class="glance-item" data-idx="{_i}">'
            f'<span class="gl-title">{_safe_escape(_t[:60])}</span></li>'
            for _i, _t in _gitems
        )
        _gparts.append(
            f'<div class="glance-group">'
            f'<div class="gl-grp-head">{_gemoji} {_gname}'
            f'<span class="gl-grp-n">{len(_gitems)}</span></div>'
            f'<ul class="glance-list">{_rows}</ul></div>'
        )
    today_glance = (
        f'<h2 class="glance-title">⚡ 今日速览<span class="gl-n">{_glance_n}</span></h2>'
        f'<div class="glance-groups">{"".join(_gparts)}</div>'
    ) if _gparts else ''

    # ── 今日导览：按"非空版块"生成跳转 chip，给页面一个一眼可记的层次地图 ──
    # 阅读顺序：速览 → 判断 → 必读 → 更多 → 大V → 实体 → 口播。
    # 突发已并入早晚报正文(12h 报道一次足够实时), 不再做独立版块。
    _nav_items = [
        ('sec-glance', '⚡', '速览', bool(today_glance)),
        ('sec-judgment', '🎯', '判断', bool(briefing_html and str(briefing_html).strip())),
        ('sec-featured', '⭐', '必读', bool(featured_html)),
        ('sec-more', '📚', '更多', bool(cards_html)),
        ('sec-vip', '👤', '大V', bool(vip_html)),
        ('sec-entity', '🔗', '实体', bool(entity_tracker_html)),
        ('sec-broadcast', '🎙', '口播', bool(broadcast_html)),
    ]
    _nav_links = []
    for _sid, _emoji, _label, _present in _nav_items:
        if not _present:
            continue
        _nav_links.append(f'<a class="tn-link" href="#{_sid}">{_emoji} {_label}</a>')
    # 往期早晚报：始终可达的历史入口（指向仪表盘的归档列表），解决"查不到历史"。
    # 不是页内锚点而是真实链接，故单独追加、靠右分隔。
    _nav_links.append(
        '<a class="tn-link tn-history" href="archive/dashboard.html" '
        'title="浏览往期早报 / 晚报归档">📅 往期</a>')
    today_nav = (f'<nav class="today-nav" aria-label="今日导览">{"".join(_nav_links)}</nav>'
                 if _nav_links else '')

    html = page_template.safe_substitute(
        today_nav=today_nav,
        today_glance=today_glance,
        audio_top_html=audio_top_html,
        broadcast_html=broadcast_html,
        vip_html=vip_html,
        date_str=date_str,
        weekday=weekday,
        time_str=time_str,
        briefing_title=briefing_title,
        briefing_emoji=briefing_emoji,
        total=total,
        sources_count=sources_count,
        filter_html=filter_html,
        audience_filter_html=audience_filter_html,
        top3_html=top3_html,
        llm_banner_html=llm_banner_html,
        briefing_html=briefing_html,
        entity_tracker_html=entity_tracker_html,
        featured_html=featured_html,
        cards_html=cards_html,
        css_content=css_content,
        js_content=js_content,
        meta_description=meta_description,
    )

    return html, modal_js_content
