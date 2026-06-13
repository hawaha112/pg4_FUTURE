#!/usr/bin/env python3
"""
collector.py — 独立采集入口 v3

职责：RSS 抓取 → 去重 → 正文提取 → LLM 分析 → 事件聚类 → 写入事件库
设计为每 1-2 小时运行一次，与出报（briefing_renderer.py）彻底分离。

用法:
    python3 collector.py                # 完整采集 + LLM 分析 + 事件聚类
    python3 collector.py --no-llm       # 仅采集，跳过 LLM
    python3 collector.py --tier 0       # 仅采集指定层级的源
    python3 collector.py --tier 0,1     # 采集 Tier 0 + Tier 1

v3 改进（事件系统）：
- 引入 canonical event store：每个独立事件有生命周期
- 聚类保留证据链：谁先发、谁跟发、官方是否确认
- 事件状态自动升级：rumor → reported → confirmed → official
- 分层配额制：Tier 0 全保留、Tier 1 保底、Tier 2 填充剩余
- 实体覆盖矩阵：标记受保护条目，检查关键主体覆盖率
"""

import json
import os
import re
import sys
import concurrent.futures
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from logger import get_logger
log = get_logger('collector')

from content_fetcher import fetch_feed, enrich_articles_with_content, SourceHealthTracker
from dedup_engine import DedupEngine
from source_ranker import SourceRanker
from llm_analyzer import LLMAnalyzer, LLMCache, create_analyzer_from_config
from rule_analyzer import RuleAnalyzer, TieredAnalyzer
from causal_engine import CausalKB
from config_validator import validate_and_warn
from event_store import EventStore
from event_cluster import EventClusterer
from entity_coverage import EntityCoverageMatrix


# ═══════════════════════════════════════════════════════════════════════
# 关键词预过滤 v2
# ═══════════════════════════════════════════════════════════════════════

# 拆成两个 regex —— 历史版本把英文+中文混在一个 `\b...\b` 里, 但
# Python re 的 `\b` 在纯中文文本里从不触发（中文都是 \w 字符）, 所以
# "清华开源百亿参数大模型" 这种纯中文标题在外层 \b 包围下永远不匹配,
# 中文关键词层等于摆设. 拆开后英文保留 word-boundary 防止误命中
# (chip vs ship), 中文用裸子串匹配, 各取所需.
_AI_KEYWORDS_EN = re.compile(
    r'(?i)\b(?:'
    r'ai|artificial.intelligence|machine.learning|deep.learning|'
    r'neural.net|llm|large.language.model|foundation.model|'
    r'transformer|diffusion.model|reinforcement.learning|'
    r'computer.vision|natural.language|nlp|nlu|'
    r'generative|gen.?ai|agi|alignment|'
    r'multi.?modal|reasoning|agent|agentic|'
    r'token|embedding|fine.?tun|rag|vector.?db|'
    r'gpt|chatgpt|openai|o1|o3|o4|'
    r'anthropic|claude|sonnet|opus|haiku|'
    r'gemini|gemma|copilot|cursor|'
    r'midjourney|stable.diffusion|sora|flux|'
    r'deepseek|mistral|llama|qwen|kimi|'
    r'hugging.?face|pytorch|tensorflow|jax|'
    r'meta.ai|perplexity|cohere|'
    r'chip|gpu|tpu|npu|nvidia|cuda|'
    r'robot|autonomous|self.driving|autopilot|'
    r'humanoid|embodied|'
    r'vibe.?cod|ai.?cod|code.?gen|'
    r'text.to|image.gen|video.gen|voice.clone|'
    r'ai.?search|ai.?agent|mcp|model.context|'
    # 补遗：开源 / 工程 / 芯片 / 训练范式 / 评测
    r'open.?source|open.?model|model.?weight|'
    r'check.?point|model.?card|'
    r'inference.?engine|vllm|sglang|trt.?llm|tensorrt|'
    r'rlhf|dpo|ppo|grpo|moe|mixture.of.experts|'
    r'scaling.?law|emergent|long.?context|context.?window|'
    r'benchmark|eval|evaluation|leaderboard|'
    r'dataset|fine.?tuning.dataset|pretraining|'
    r'multi.?agent|tool.?use|function.?call|'
    r'prompt.?engineering|in.context|few.?shot|chain.?of.?thought|cot|'
    r'ai.?safety|red.?team|jailbreak|'
    r'ai.?startup|ai.?lab|ai.?fund'
    r')\b'
)

_AI_KEYWORDS_CN = re.compile(
    r'(?:'
    r'通义|文心|豆包|'
    r'人工智能|机器学习|深度学习|大模型|大语言模型|'
    r'神经网络|自然语言|智能体|算力|芯片|'
    r'自动驾驶|具身智能|生成式|训练|推理|'
    r'向量|微调|对齐|多模态|'
    r'AI编程|AI搜索|AI助手|AI应用|'
    r'模型|蒸馏|量化|开源模型|闭源|'
    r'开源|权重|检查点|预训练|强化学习|'
    r'基准|评测|榜单|数据集|微调数据|'
    r'多智能体|工具调用|思维链|长上下文|'
    r'AI安全|红队|越狱|涌现|缩放定律|'
    r'AI创业|AI投资|AI融资|'
    r'语音合成|文生图|文生视频|数字人|'
    r'人形机器人|无人驾驶|智能驾驶'
    r')'
)


class _CombinedKwMatcher:
    """让 collector 端调用方继续 _AI_KEYWORDS.search(text), 内部 OR 两个 regex"""
    @staticmethod
    def search(text: str):
        return _AI_KEYWORDS_EN.search(text) or _AI_KEYWORDS_CN.search(text)


_AI_KEYWORDS = _CombinedKwMatcher

# 高权威综合媒体：不做关键词过滤，交给 LLM 判断
# （这些源的编辑水准高，即使标题不含 AI 关键词也可能报道重要的 AI 相关新闻）
_HIGH_AUTHORITY_GENERAL_MEDIA = {
    'Ars Technica AI', 'MIT Tech Review', 'Wired AI',
    '虎嗅科技', '知乎日报',
}

# ───────────────────────────────────────────────────────────────────────
# 合订本/聚合帖过滤
# 部分综合媒体（如爱范儿）RSS 里每天混一条「早报｜A/B/C」式聚合帖：一条 item
# 塞多条互不相关的新闻。采集器若当成单一事件，LLM 只能把里面的多条硬揉进一张卡
# （实测一张卡同时出现华为盘古 + Kimi K2.7 + Genspark 融资）。命中即在采集端丢弃——
# 这些事件多半也能从一手源单独采到，丢掉聚合壳不丢内容。正则刻意收紧，只认
# 「栏目标签领头 + 分隔符」或「AI/科技+早晚周报」这类强信号，避免误伤单事件文章。
_ROUNDUP_LEAD_RE = re.compile(
    r'^\s*(?:【[^】]{0,12}】|\[[^\]]{0,12}\])?\s*'        # 可选 【科技】/[AI] 前缀标签
    r'(?:[^｜|│\s：:]{0,8}[：:]\s*)?'                     # 可选 "36氪：" 来源前缀
    r'(?:早报|晚报|午报|日报|早间快讯|晚间快讯|早间新闻|晚间新闻|今日简报)'
    r'\s*[｜|│\|：:·、\-—]'                              # 栏目名后必须跟分隔符（与"简报功能"等区分）
)
_ROUNDUP_KW_RE = re.compile(
    r'(?:AI|科技|每日|今日|大模型|互联网|创投|财经)\s*'
    r'(?:早报|晚报|日报|周报|月报|快讯合集|资讯合集|资讯汇总|要闻汇总)'
    r'|(?:一周|本周|上周|过去一周|过去7天|过去七天)\s*[^，。\n]{0,8}'
    r'(?:盘点|大事|要闻|回顾|热点|速览|汇总|总结)'
    r'|(?:AI|大模型|科技|行业)\s*(?:周报|周刊|月报|月刊)'
)


def _is_roundup_title(title: str) -> bool:
    """标题是否为合订本/聚合帖（多条不相关新闻塞进一条 item）。"""
    t = (title or '').strip()
    if not t:
        return False
    return bool(_ROUNDUP_LEAD_RE.match(t) or _ROUNDUP_KW_RE.search(t))


def _drop_roundup_posts(items):
    """丢弃合订本/聚合帖。返回 (保留列表, 丢弃的标题列表)。"""
    kept, dropped = [], []
    for item in items:
        if _is_roundup_title(item.get('title', '')):
            dropped.append(item.get('title', '')[:80])
        else:
            kept.append(item)
    return kept, dropped


def _keyword_prefilter(items, ai_only_sources, source_authority):
    """分层关键词预过滤

    - Tier 0/1：全部保留（不过滤）
    - Tier 2 + ai_only=True：全部保留（源本身是 AI 频道）
    - Tier 2 + 高权威综合媒体：全部保留（交给 LLM 判断）
    - Tier 2 + 实体保护条目：全部保留
    - Tier 2 + 其他综合源：关键词过滤
    """
    result = []
    filtered = 0
    passed_without_keyword = 0

    for item in items:
        source = item.get('source_name', '')
        tier = item.get('source_tier', 2)

        # Tier 0/1 全部保留
        if tier <= 1:
            result.append(item)
            continue

        # ai_only 源全部保留
        if source in ai_only_sources:
            result.append(item)
            continue

        # 高权威综合媒体：不做关键词过滤，交给 LLM
        if source in _HIGH_AUTHORITY_GENERAL_MEDIA:
            result.append(item)
            passed_without_keyword += 1
            continue

        # 实体保护条目：不做关键词过滤
        if item.get('_entity_protected', False):
            result.append(item)
            passed_without_keyword += 1
            continue

        # 其他综合源：关键词过滤
        text = (item.get('title', '') + ' ' + item.get('summary', '')[:300]).lower()
        if _AI_KEYWORDS.search(text):
            result.append(item)
        else:
            filtered += 1

    if filtered > 0:
        log.info("🔍 关键词预过滤移除 %d 条（%d 条高权威/实体保护条目跳过过滤）",
                 filtered, passed_without_keyword)
    return result


# ═══════════════════════════════════════════════════════════════════════
# 分层配额制
# ═══════════════════════════════════════════════════════════════════════

def _apply_tiered_quota(items, config):
    """分层配额：取代固定 all_items[:200] 硬截断

    配额策略：
      - Tier 0（官方一手源）：全保留，无上限
      - Tier 1（研究/开发者）：保底 60 条，按时间取最新
      - Tier 2（媒体/社区）：填充剩余配额至总数 250
      - 同源限额：单个源最多 per_source_max 条（防止高频源刷屏）
      - 实体保护：匹配关键实体的条目不受同源限额影响

    Args:
        items: 去重后的全部条目（已按时间排序）
        config: 配置（可读取自定义配额参数）

    Returns:
        配额裁剪后的条目列表
    """
    settings = config.get('settings', {})
    total_budget = settings.get('total_quota', 250)
    tier1_min = settings.get('tier1_min_quota', 60)
    per_source_max = settings.get('per_source_max', 8)

    # 按 tier 分桶
    tier0 = [i for i in items if i.get('source_tier', 2) == 0]
    tier1 = [i for i in items if i.get('source_tier', 2) == 1]
    tier2 = [i for i in items if i.get('source_tier', 2) == 2]

    # 同源限额（per_source_max），实体保护条目豁免
    def _apply_source_cap(bucket, cap):
        source_counts = Counter()
        result = []
        for item in bucket:
            src = item.get('source_name', '')
            if source_counts[src] >= cap and not item.get('_entity_protected', False):
                continue
            source_counts[src] += 1
            result.append(item)
        return result

    # Tier 0: 全保留（不限额，但做同源限额防刷屏，上限宽松 15）
    tier0 = _apply_source_cap(tier0, 15)

    # Tier 1: 同源限额后，保底 tier1_min 条
    tier1 = _apply_source_cap(tier1, per_source_max)
    remaining = total_budget - len(tier0)
    tier1_quota = max(tier1_min, remaining // 2)
    tier1 = tier1[:tier1_quota]

    # Tier 2: 同源限额后，填充剩余配额
    tier2 = _apply_source_cap(tier2, per_source_max)
    remaining = total_budget - len(tier0) - len(tier1)
    tier2 = tier2[:max(0, remaining)]

    result = tier0 + tier1 + tier2
    log.info("📦 分层配额: T0=%d, T1=%d, T2=%d（总 %d / 预算 %d）",
             len(tier0), len(tier1), len(tier2), len(result), total_budget)
    return result


# ═══════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════

def _parse_tier_arg() -> set:
    for i, arg in enumerate(sys.argv):
        if arg == '--tier' and i + 1 < len(sys.argv):
            return {int(t.strip()) for t in sys.argv[i + 1].split(',')}
    return set()


def _default_analysis(title):
    return {
        'ai_relevant': True,
        'chinese_title': '',
        'summary': title[:100] if title else '无标题',
        'why_it_matters': '',
        'key_details': [],
        'detailed_content': '',
        'background': '',
        'deep_analysis': '',
        'importance': 1,
        'is_follow_up': False,
        'categories': ['其他'],
        'source_type': 'news',
        'reading_minutes': 1,
        'causal_events': [],
        'affected_assets': [],
        'impact_direction': 'neutral',
        'impact_confidence': 'low',
    }


# ═══════════════════════════════════════════════════════════════════════
# 主采集流程
# ═══════════════════════════════════════════════════════════════════════

def main():
    script_dir = Path(__file__).parent
    config_path = script_dir / 'config.json'

    log.info("=" * 55)
    log.info("📡 采集器 v3 — 事件系统 + 证据链 + 状态生命周期")
    log.info("=" * 55)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    validate_and_warn(config)

    sources = config['sources']
    settings = config['settings']
    max_items = settings.get('max_items_per_source', 10)
    max_age = settings.get('max_age_hours', 24)
    skip_llm = '--no-llm' in sys.argv
    tier_filter = _parse_tier_arg()

    # 初始化
    event_db_path = str(script_dir / settings.get('event_db', 'events.db'))
    store = EventStore(event_db_path)
    run_id = store.start_collection_run()
    llm_cache = LLMCache(str(script_dir / 'llm_cache.db'))
    entity_matrix = EntityCoverageMatrix()

    # ── 构建源列表 ──
    all_sources_raw = sources.get('english', []) + sources.get('chinese', [])
    all_sources = [
        s for s in all_sources_raw
        if s.get('enabled', True) and not s.get('disabled', False)
    ]

    if tier_filter:
        all_sources = [s for s in all_sources if s.get('tier', 2) in tier_filter]
        log.info("📋 按 Tier %s 过滤，共 %s 个信息源", tier_filter, len(all_sources))
    else:
        log.info("📋 共 %s 个信息源", len(all_sources))

    # ① 并发抓取 RSS
    health_tracker = SourceHealthTracker(
        health_path=str(script_dir / 'source_health.json'),
        alert_threshold=3,
    )

    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_feed, src, max_items, max_age, health_tracker, config): src
            for src in all_sources
        }
        for future in concurrent.futures.as_completed(futures):
            src = futures[future]
            items = future.result()
            for item in items:
                item['source_tier'] = src.get('tier', 2)
            all_items.extend(items)

    # 已在 config 中 enabled=false 的源不参与告警（它们已被跳过抓取）
    disabled_names = {
        s['name'] for s in all_sources_raw
        if not s.get('enabled', True) or s.get('disabled', False)
    }
    health_tracker.print_report(disabled_sources=disabled_names)
    health_tracker.save()

    # 自动禁用长期失败的源（默认关闭，通过 AUTO_DISABLE_DEAD_SOURCES=1 或 config.settings.auto_disable_sources 启用）
    _auto_disable = (
        os.environ.get('AUTO_DISABLE_DEAD_SOURCES') == '1'
        or config.get('settings', {}).get('auto_disable_sources', False)
    )
    if _auto_disable:
        threshold = int(config.get('settings', {}).get('auto_disable_threshold', 20))
        disabled = health_tracker.auto_disable_dead_sources(
            str(script_dir / 'config.json'),
            disable_threshold=threshold,
        )
        if disabled:
            log.warning("⚠️ 已自动禁用长期失败源: %s", ', '.join(disabled))

    # 实体覆盖 - 检查源健康
    try:
        with open(script_dir / 'source_health.json', 'r', encoding='utf-8') as f:
            source_health = json.load(f)
        entity_matrix.check_source_health(source_health)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # 分层时间过滤：tier 0/1 默认 168h，tier 2 按 max_age；源可在 config 里加
    # "max_age_hours" 字段 override（例如 GitHub releases 类设 720h 让月发版本通过）。
    _TIER_MAX_AGE = {0: 168, 1: 168, 2: max_age}
    # 构建 source_name → max_age_hours override 映射
    _src_override = {}
    for _s in all_sources:
        if _s.get('max_age_hours'):
            _src_override[_s['name']] = int(_s['max_age_hours'])
    now_utc = datetime.now(timezone.utc)
    before = len(all_items)
    _kept = []
    for i in all_items:
        pub = i.get('published')
        if not pub:
            _kept.append(i)
            continue
        sn = i.get('source_name', '')
        if sn in _src_override:
            max_h = _src_override[sn]
        else:
            tier = i.get('source_tier', 2)
            max_h = _TIER_MAX_AGE.get(tier, max_age)
        if pub >= now_utc - timedelta(hours=max_h):
            _kept.append(i)
    all_items = _kept
    age_filtered = before - len(all_items)
    if age_filtered > 0:
        log.info("🕐 时间过滤移除 %s 条旧文章（T0/T1=%dh · T2=%dh · %d 源自定义）",
                 age_filtered, _TIER_MAX_AGE[0], max_age, len(_src_override))

    # 合订本/聚合帖过滤（如爱范儿「早报｜A/B/C」一条塞多新闻 → 丢弃，避免揉成一张卡）
    all_items, _roundups = _drop_roundup_posts(all_items)
    if _roundups:
        log.info("🗞️  合订本过滤移除 %d 条聚合帖：%s",
                 len(_roundups), " ｜ ".join(_roundups[:3]))

    # 按时间排序
    def sort_key(item):
        if item.get('published'):
            return (0, -item['published'].timestamp())
        return (1, 0)
    all_items.sort(key=sort_key)
    log.info("📊 共获取 %s 条资讯", len(all_items))

    # ② 去重（双阈值 + 可配置，默认 EN 0.60 / CJK 0.50）
    dedup_db_path = str(script_dir / 'dedup.db')
    _dedup_cfg = config.get('dedup', {}) if isinstance(config.get('dedup'), dict) else {}
    dedup_engine = DedupEngine(
        db_path=dedup_db_path,
        semantic_threshold=float(_dedup_cfg.get('threshold_en', 0.60)),
        semantic_threshold_cjk=float(_dedup_cfg.get('threshold_cjk', 0.50)),
        recent_hours=int(_dedup_cfg.get('recent_hours', max_age)),
    )
    ranker = SourceRanker(config)
    all_items = dedup_engine.deduplicate(all_items, source_authority=ranker.authority)

    for item in all_items:
        item['_is_new'] = True

    # ③ 实体标记（在配额裁剪之前，让受保护条目不被截断）
    all_items = entity_matrix.tag_items(all_items)

    # ④ 分层配额制（取代硬截断）
    all_items = _apply_tiered_quota(all_items, config)

    # ⑤ 实体限流（同一实体最多 5 条，防刷屏）
    all_items = entity_matrix.apply_entity_rate_limit(
        all_items, max_per_entity=5, protect_tier0=True
    )

    # ⑥ 关键词预过滤（弱化版：高权威综合媒体 + 实体保护条目跳过）
    ai_only_sources = set()
    for lang in ('english', 'chinese'):
        for src in config.get('sources', {}).get(lang, []):
            if src.get('ai_only', False):
                ai_only_sources.add(src['name'])

    all_items = _keyword_prefilter(all_items, ai_only_sources, ranker.authority)

    # ⑦ 抓取文章原文
    all_items = enrich_articles_with_content(all_items)

    # ⑧ 写入事件库
    new_count = 0
    for item in all_items:
        if store.upsert_event(item):
            new_count += 1
    store.commit()
    log.info("💾 写入事件库: %d 条新事件（共 %d 条）", new_count, len(all_items))

    # ⑨ 分级分析（规则兜底 + LLM 增强）
    items_analyzed = 0
    rule_analyzer = RuleAnalyzer()
    llm_analyzer = None if skip_llm else create_analyzer_from_config(config)

    pending = store.get_pending_analysis(limit=250)
    if pending:
        # 构建 TieredAnalyzer：LLM 可选，规则兜底
        tiered = TieredAnalyzer(
            rule_analyzer=rule_analyzer,
            llm_analyzer=llm_analyzer,
            deep_analysis_top_n=settings.get('deep_analysis_top_n', 20),
            llm_candidate_threshold=settings.get('llm_candidate_threshold', 2),
        )

        log.info("🧠 分级分析（%d 条待分析, LLM %s）...",
                 len(pending), '可用' if llm_analyzer else '不可用')

        # 调用分级分析
        analyses = tiered.analyze_batch(pending, llm_cache=llm_cache)

        for event, analysis in zip(pending, analyses):
            tier = event.get('source_tier', 2)
            url = event.get('url', '')

            if not analysis:
                analysis = _default_analysis(event.get('title', ''))

            # Tier 0 特殊处理：强制保留
            if tier == 0:
                analysis['ai_relevant'] = True
                if analysis.get('importance', 0) < 3:
                    analysis['importance'] = 3

            store.save_analysis(url, analysis)
            items_analyzed += 1

        store.commit()

        # 日志：分析级别分布
        ai_count = sum(1 for a in analyses if a and a.get('ai_relevant'))
        non_ai = sum(1 for a in analyses if a and not a.get('ai_relevant'))
        llm_count = sum(1 for a in analyses if a and a.get('_analysis_level', 0) >= 1)
        rule_count = ai_count - llm_count
        log.info("✅ 分级分析完成: %d 条（AI相关 %d: 规则=%d, LLM=%d | 非相关 %d）",
                 items_analyzed, ai_count, rule_count, llm_count, non_ai)

        # 因果分析（仅对有 LLM 分析结果的条目）
        if tiered.is_llm_available:
            try:
                causal_kb = CausalKB()
                causal_matched = 0
                analyzed_events = store.get_events_for_briefing(hours=max_age)
                for event in analyzed_events:
                    analysis = event.get('analysis', {})
                    if analysis.get('causal_events') and not analysis.get('causal_matches'):
                        matches = causal_kb.match_article(analysis)
                        if matches:
                            analysis['causal_matches'] = causal_kb.format_impact_json(matches)
                            analysis['impact_summary'] = causal_kb.format_impact_summary(matches)
                            store.save_analysis(event['url'], analysis)
                            causal_matched += 1
                if causal_matched:
                    store.commit()
                    log.info("🔗 因果分析匹配 %s 条", causal_matched)
            except Exception as e:
                log.warning("⚠️ 因果分析失败: %s", e)

        # 规则分析统计
        rule_stats = rule_analyzer.stats
        if rule_stats.get('analyzed', 0) > 0:
            log.info("📊 规则分析: %s", rule_stats)
    else:
        log.info("✅ 无新事件需要分析")

    # ⑩ 事件聚类：将已分析文章聚合为 canonical events + 证据链
    events_created = 0
    events_updated = 0
    try:
        clusterer = EventClusterer(
            store,
            # 实测：英文 0.55 / 中文 0.45 过严 —— GPT-Rosalind、Cursor 50B、AI Mode 等
            # 明显同事件的 pair sim 落在 0.41–0.55 被毙，连续 10+ 轮"更新 0"。
            # 降到 0.40 / 0.35 让这些对可以合并；实体共享时自动再 * 0.65 进一步降。
            similarity_threshold=0.40,
            similarity_threshold_cjk=0.35,
            merge_window_hours=max_age,
        )
        # 获取所有已分析但未关联的文章
        analyzed_articles = store.get_unlinked_articles(hours=max_age)
        if analyzed_articles:
            log.info("🔗 事件聚类（%d 篇待聚类文章）...", len(analyzed_articles))
            cluster_stats = clusterer.cluster_and_link(analyzed_articles)
            events_created = cluster_stats.get('events_created', 0)
            events_updated = cluster_stats.get('events_updated', 0)
        else:
            log.info("✅ 无需聚类：所有文章均已关联到事件")
    except Exception as e:
        log.warning("⚠️ 事件聚类失败: %s", e)
        import traceback
        traceback.print_exc()

    # 去重引擎持久化
    dedup_engine.commit()
    dedup_engine.db.cleanup(keep_days=30)
    dedup_engine.close()

    # LLM 缓存清理
    try:
        llm_cache.cleanup()
        log.info("💾 LLM 缓存: %s", llm_cache.stats)
        llm_cache.close()
    except Exception as e:
        log.warning("⚠️ LLM 缓存清理失败: %s", e)

    # 把 collector 阶段的 LLM 用量持久化, briefing_renderer 跑完会读这文件
    # 跟 renderer 自己的 generate_digest 用量加起来写进 stats.json
    try:
        if llm_analyzer is not None:
            usage = llm_analyzer.usage_stats()
            usage_path = script_dir / 'output' / '.collector_llm_usage.json'
            usage_path.parent.mkdir(parents=True, exist_ok=True)
            usage_path.write_text(json.dumps(usage, ensure_ascii=False),
                                  encoding='utf-8')
            log.info("📊 collector LLM 用量: calls=%d tokens=%d (in=%d out=%d) parse_fallback=%d",
                     usage['llm_call_count'], usage['llm_total_tokens'],
                     usage['llm_prompt_tokens'], usage['llm_completion_tokens'],
                     usage['llm_parse_fallback'])
    except Exception as e:
        log.warning("⚠️ collector LLM 用量持久化失败: %s", e)

    # 实体覆盖率检查
    entity_counts = entity_matrix.check_coverage(store, hours=max_age)
    entity_matrix.print_report(entity_counts)

    # 完成采集记录
    store.finish_collection_run(
        run_id, len(all_items), new_count, items_analyzed,
        events_created=events_created, events_updated=events_updated,
    )

    # 事件库维护
    store.cleanup(keep_days=30)

    stats = store.stats()
    log.info("📊 事件库: %s", stats)

    # 告警
    alerts = entity_matrix.get_alerts()
    if alerts:
        log.warning("🚨 %d 条实体覆盖告警", len(alerts))

    store.close()

    # ── 抓取外部热度信号 (HN / Reddit / HF / GitHub) ──
    # 信号会持久化到 output/hot_signals.json, 供 renderer 加载做 importance boost
    # 注意: 失败不阻塞 collector 流水线 (函数内部已捕获所有异常)
    try:
        from extractors.hot_signals import fetch_all_hot_signals
        hot_signals_path = Path(__file__).parent / 'output' / 'hot_signals.json'
        fetch_all_hot_signals(save_path=hot_signals_path)
    except Exception as e:
        log.warning("⚠️ 热度信号抓取异常 (不阻塞): %s", e)

    log.info("=" * 55)
    log.info("✅ 采集完成: 抓取 %d, 新增 %d, 分析 %d, "
             "事件新建 %d / 更新 %d",
             len(all_items), new_count, items_analyzed,
             events_created, events_updated)
    log.info("=" * 55)


if __name__ == '__main__':
    main()
