#!/usr/bin/env python3
"""
AI Morning Briefing - 每日 AI 资讯聚合器 v3

流程：RSS 抓取 → 文章原文提取 → LLM Toulmin 分析 + 翻译 → 六区卡片 HTML 生成

用法:
    python3 fetch_news.py              # 抓取并生成页面
    python3 fetch_news.py --open       # 抓取、生成并在浏览器中打开
    python3 fetch_news.py --no-llm     # 跳过 LLM 分析（仅抓取）
    python3 fetch_news.py --resume     # 从上次断点继续
"""

import json
import re
import sys
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path

from logger import get_logger
log = get_logger('fetch_news')

# 本地模块
from checkpoint import PipelineCheckpoint
from content_fetcher import fetch_feed, enrich_articles_with_content, SourceHealthTracker
from html_generator import generate_html
from llm_analyzer import LLMAnalyzer, LLMCache, create_analyzer_from_config
from dedup_engine import DedupEngine
from source_ranker import SourceRanker
from causal_engine import CausalKB
from config_validator import validate_and_warn


# ═══════════════════════════════════════════════════════════════════════
# 关键词预过滤：对非 AI 专用源做规则排除，减少 LLM 调用
# ═══════════════════════════════════════════════════════════════════════

_AI_KEYWORDS = re.compile(
    r'(?i)\b(?:'
    # 核心概念
    r'ai|artificial.intelligence|machine.learning|deep.learning|'
    r'neural.net|llm|large.language.model|foundation.model|'
    r'transformer|diffusion.model|reinforcement.learning|'
    r'computer.vision|natural.language|nlp|nlu|'
    r'generative|gen.?ai|agi|alignment|'
    r'multi.?modal|reasoning|agent|agentic|'
    r'token|embedding|fine.?tun|rag|vector.?db|'
    # 公司和产品
    r'gpt|chatgpt|openai|o1|o3|o4|'
    r'anthropic|claude|sonnet|opus|haiku|'
    r'gemini|gemma|copilot|cursor|'
    r'midjourney|stable.diffusion|sora|flux|'
    r'deepseek|mistral|llama|qwen|通义|文心|豆包|kimi|'
    r'hugging.?face|pytorch|tensorflow|jax|'
    r'meta.ai|perplexity|cohere|'
    # 硬件与基础设施
    r'chip|gpu|tpu|npu|nvidia|cuda|'
    r'robot|autonomous|self.driving|autopilot|'
    r'humanoid|embodied|'
    # 应用场景
    r'vibe.?cod|ai.?cod|code.?gen|'
    r'text.to|image.gen|video.gen|voice.clone|'
    r'ai.?search|ai.?agent|mcp|model.context|'
    # 中文
    r'人工智能|机器学习|深度学习|大模型|大语言模型|'
    r'神经网络|自然语言|智能体|算力|芯片|'
    r'自动驾驶|具身智能|生成式|训练|推理|'
    r'向量|微调|对齐|多模态|'
    r'AI编程|AI搜索|AI助手|AI应用|'
    r'模型|蒸馏|量化|开源模型|闭源|'
    r'语音合成|文生图|文生视频|数字人|'
    r'人形机器人|无人驾驶|智能驾驶'
    r')\b'
)


def _keyword_prefilter(items, ai_only_sources):
    """对非 ai_only 源的条目做关键词预筛选

    ai_only 源的条目全部保留（已经是 AI 频道）。
    非 ai_only 源的条目需要标题或摘要中含 AI 关键词才保留。
    """
    result = []
    filtered = 0
    for item in items:
        source = item.get('source_name', '')
        if source in ai_only_sources:
            result.append(item)
            continue
        # 非 AI 专用源 → 用关键词判断
        text = (item.get('title', '') + ' ' + item.get('summary', '')[:300]).lower()
        if _AI_KEYWORDS.search(text):
            result.append(item)
        else:
            filtered += 1
    if filtered > 0:
        log.info("🔍 关键词预过滤移除 %s 条明显非 AI 内容（节省 LLM 调用）", filtered)
    return result


def _default_analysis(title):
    """生成默认的分析结构（LLM 跳过或未配置时使用）"""
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
# 主流程
# ═══════════════════════════════════════════════════════════════════════

def main():
    script_dir = Path(__file__).parent
    config_path = script_dir / 'config.json'

    log.info("=" * 55)
    log.info("📡 AI 早报 v3 — 实用信息框架版")
    log.info("=" * 55)

    # 读取配置
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 验证配置
    validate_and_warn(config)

    sources = config['sources']
    settings = config['settings']
    source_authority = config.get('source_authority', {})
    max_items = settings.get('max_items_per_source', 10)
    max_age = settings.get('max_age_hours', 24)
    skip_llm = '--no-llm' in sys.argv
    resume_mode = '--resume' in sys.argv

    # Checkpoint 管理
    ckpt = PipelineCheckpoint(script_dir / 'pipeline_checkpoint.json')
    resumed_from = None

    # ── 尝试从 checkpoint 恢复 ──
    if resume_mode and ckpt.exists():
        ckpt_data = ckpt.load()
        stage = ckpt.get_stage()
        log.info("💾 检测到 checkpoint（阶段: %s），从断点恢复...", stage)
        all_items = ckpt_data['items']
        resumed_from = stage
        log.info("📦 恢复 %s 篇文章", len(all_items))
        llm_done = ckpt.get_llm_progress()
        if llm_done:
            log.info("🧠 已有 %d/%d 篇 LLM 分析结果", len(llm_done), len(all_items))
            for idx_str, analysis in llm_done.items():
                idx = int(idx_str)
                if idx < len(all_items):
                    all_items[idx]['analysis'] = analysis
    else:
        resumed_from = None

    all_sources_raw = sources.get('english', []) + sources.get('chinese', [])
    # 过滤掉显式禁用的源（enabled=false 或 disabled=true）
    # 这样可以"软禁用"失效源而无需删除配置，方便日后恢复
    all_sources = [
        s for s in all_sources_raw
        if s.get('enabled', True) and not s.get('disabled', False)
    ]
    _disabled_count = len(all_sources_raw) - len(all_sources)

    if not resumed_from:
        # ── 全新运行：从 RSS 抓取开始 ──
        if _disabled_count:
            log.info("📋 共 %s 个信息源（已跳过 %s 个禁用源）\n",
                     len(all_sources), _disabled_count)
        else:
            log.info("📋 共 %s 个信息源\n", len(all_sources))

        # 初始化源健康度追踪
        health_tracker = SourceHealthTracker(
            health_path=str(script_dir / 'source_health.json'),
            alert_threshold=3
        )

        # ① 并发抓取 RSS
        all_items = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_feed, src, max_items, max_age, health_tracker, config): src
                       for src in all_sources}
            for future in concurrent.futures.as_completed(futures):
                all_items.extend(future.result())

        # 源健康度报警
        health_tracker.print_report()
        health_tracker.save()

        # 全局时间过滤（双重保险：即使单源过滤遗漏，这里也会拦截）
        global_cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
        before_age_filter = len(all_items)
        all_items = [i for i in all_items
                     if not i.get('published') or i['published'] >= global_cutoff]
        age_filtered = before_age_filter - len(all_items)
        if age_filtered > 0:
            log.info("🕐 全局时间过滤移除 %s 条超过 %s 小时的旧文章", age_filtered, max_age)

        # 按时间排序
        def sort_key(item):
            if item.get('published'):
                return (0, -item['published'].timestamp())
            return (1, 0)
        all_items.sort(key=sort_key)
        log.info("📊 共获取 %s 条资讯", len(all_items))

        # ② 去重（DedupEngine：哈希 + 语义两层去重，SQLite 持久化）
        dedup_db_path = str(script_dir / 'dedup.db')
        max_age = settings.get('max_age_hours', 24)
        dedup_engine = DedupEngine(
            db_path=dedup_db_path,
            semantic_threshold=0.60,
            recent_hours=max_age
        )
        ranker = SourceRanker(config)
        all_items = dedup_engine.deduplicate(
            all_items,
            source_authority=ranker.authority
        )
        # 标记所有通过去重的条目为"新"（DedupEngine 已处理历史对比）
        for item in all_items:
            item['_is_new'] = True

        # 限制总数：保留按时间排序后的前 200 条进后续流程
        # （100 偏紧，会把较旧但仍有价值的 YouTube / 官方博客等低频源砍掉）
        all_items = all_items[:200]

        # ③-B 关键词预过滤：对 ai_only=false 的源，先用规则排除明显非 AI 内容
        ai_only_sources = set()
        for lang in ('english', 'chinese'):
            for src in config.get('sources', {}).get(lang, []):
                if src.get('ai_only', False):
                    ai_only_sources.add(src['name'])

        all_items = _keyword_prefilter(all_items, ai_only_sources)

        # ④ 抓取文章原文（为 LLM 提供更多上下文）
        all_items = enrich_articles_with_content(all_items)

        # 💾 Checkpoint: RSS + 去重 + 原文抓取完成
        ckpt.save('enrich_done', all_items)
        log.info("💾 Checkpoint 已保存（%s 篇，阶段: enrich_done）", len(all_items))

    # ⑤ LLM 分析 + AI 相关性过滤
    digest = {"editorial": "", "top_stories": []}
    llm_cache = LLMCache(str(script_dir / 'llm_cache.db'))
    if not skip_llm:
        analyzer = create_analyzer_from_config(config)
        if analyzer:
            # 获取已完成的 LLM 分析（从 checkpoint 恢复时跳过已分析的）
            existing_analyses = ckpt.get_llm_progress() if resumed_from else {}
            skip_indices = set(existing_analyses.keys())
            if skip_indices:
                log.warning("⏭️ 跳过已完成的 %s 篇，续跑剩余文章", len(skip_indices))

            analyses = analyzer.batch_analyze(
                all_items,
                skip_indices=skip_indices,
                on_complete=lambda idx, result: ckpt.save_llm_result(idx, result),
                cache=llm_cache,
            )
            for item, analysis in zip(all_items, analyses):
                item['analysis'] = analysis

            # 过滤掉 AI 无关的内容
            before_filter = len(all_items)
            all_items = [i for i in all_items if i.get('analysis', {}).get('ai_relevant', True)]
            filtered_out = before_filter - len(all_items)
            if filtered_out > 0:
                log.info("🗑️ AI 相关性过滤移除 %s 条非 AI 内容", filtered_out)

            # 质量过滤：防御深度 —— LLM 可能返回一个 ai_relevant=true 但
            # summary/background/deep_analysis 全空的"僵尸记录"，这种卡片点开
            # 什么都没有。上游 llm_analyzer 已经会 drop 明显的空壳，这里再兜
            # 一层：summary 必须 ≥20 字，且下列任一非空即视为有实质内容：
            #   - background
            #   - deep_analysis
            #   - detailed_content（≥100 字）
            #   - key_details（≥2 条）
            # 这样 YouTube / Twitter 这类短内容源（LLM 偶尔只给 summary+要点
            # 不给 background）也能通过，同时挡住纯空壳。
            before_quality = len(all_items)
            def _has_substance(analysis):
                s = (analysis.get('summary') or '').strip()
                if len(s) < 20:
                    return False
                bg = (analysis.get('background') or '').strip()
                da = (analysis.get('deep_analysis') or '').strip()
                dc = (analysis.get('detailed_content') or '').strip()
                kd = analysis.get('key_details') or []
                return bool(
                    bg
                    or da
                    or (len(dc) >= 100)
                    or (isinstance(kd, list) and len([k for k in kd if k]) >= 2)
                )
            all_items = [i for i in all_items if _has_substance(i.get('analysis', {}))]
            quality_dropped = before_quality - len(all_items)
            if quality_dropped > 0:
                log.info("🧹 质量过滤移除 %s 条 LLM 分析不完整的文章（缺 summary/背景/分析）", quality_dropped)

            # ⑤b 因果分析知识库匹配
            try:
                causal_kb = CausalKB()
                causal_matched = 0
                for item in all_items:
                    analysis = item.get('analysis', {})
                    if analysis.get('causal_events'):
                        matches = causal_kb.match_article(analysis)
                        if matches:
                            analysis['causal_matches'] = causal_kb.format_impact_json(matches)
                            analysis['impact_summary'] = causal_kb.format_impact_summary(matches)
                            causal_matched += 1
                if causal_matched:
                    log.info("🔗 因果分析匹配 %s 条文章", causal_matched)
            except Exception as e:
                log.warning("⚠️ 因果分析知识库加载失败: %s", e)

            # ⑥ 生成今日速览
            log.info("📝 生成今日速览...")
            digest = analyzer.generate_digest(all_items)
        else:
            log.warning("⚠️ LLM 未配置，使用基础模式")
            for item in all_items:
                item['analysis'] = _default_analysis(item.get('title', ''))
    else:
        log.info("⏭️ 跳过 LLM 分析（--no-llm）")
        for item in all_items:
            item['analysis'] = _default_analysis(item.get('title', ''))

    # ⑦ 来源信誉评分 + 聚类标注 + 综合排序
    if not resumed_from:
        all_items = ranker.score_and_filter(all_items, min_authority=0)
        all_items = ranker.enrich_cluster_info(all_items)
        all_items = ranker.sort_by_relevance(all_items)

        # 持久化去重记录 + 清理过期 + 关闭
        dedup_engine.commit()
        dedup_engine.db.cleanup(keep_days=30)
        dedup_engine.close()
    else:
        # 从 checkpoint 恢复时，重新初始化 ranker
        ranker = SourceRanker(config)
        all_items = ranker.score_and_filter(all_items, min_authority=0)
        all_items = ranker.enrich_cluster_info(all_items)
        all_items = ranker.sort_by_relevance(all_items)

    # ⑨ 生成 HTML + modal 数据文件
    log.info("🎨 生成页面...")
    html, modal_js = generate_html(all_items, config, digest)

    output_path = script_dir / settings.get('output_file', 'output/index.html')
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    # 写入 modal 数据 JS 文件（与 index.html 同目录）
    modal_js_path = output_path.parent / 'modal_data.js'
    with open(modal_js_path, 'w', encoding='utf-8') as f:
        f.write(modal_js)

    html_kb = output_path.stat().st_size / 1024
    modal_kb = modal_js_path.stat().st_size / 1024
    log.info("✅ 页面已生成: %s (%.1f KB + modal %.1f KB)", output_path, html_kb, modal_kb)

    # 同时复制一份到上级目录方便预览（失败不中断流程）
    parent_copy = script_dir.parent / 'index.html'
    parent_modal = script_dir.parent / 'modal_data.js'
    try:
        with open(parent_copy, 'w', encoding='utf-8') as f:
            f.write(html)
        with open(parent_modal, 'w', encoding='utf-8') as f:
            f.write(modal_js)
        log.info("📋 副本: %s", parent_copy)
    except (PermissionError, OSError) as e:
        log.warning("⚠️ 复制副本到上级目录失败（已忽略）: %s", e)

    if '--open' in sys.argv:
        import webbrowser
        webbrowser.open(f'file://{output_path.resolve()}')
        log.info("🌐 已在浏览器中打开")

    # 清理 LLM 缓存过期条目 + 关闭
    try:
        llm_cache.cleanup()
        log.info("💾 LLM 缓存统计: %s", llm_cache.stats)
        llm_cache.close()
    except Exception as e:
        log.warning("⚠️ LLM 缓存清理失败（已忽略）: %s", e)

    # 先写 stats.json，再清 checkpoint —— 即使 checkpoint 清理失败，
    # run_daily.sh 读取的 stats.json 也已经是最新的
    try:
        stats_path = script_dir / 'output' / 'stats.json'
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats = {
            "article_count": len(all_items),
            # 相对路径：避免把调用方的绝对路径写入版本控制
            "output_file": str(output_path.relative_to(script_dir)),
            "file_size_kb": round(output_path.stat().st_size / 1024, 1),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "skipped_llm": bool(skip_llm),
        }
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        log.info("📊 stats.json 已更新: %s 条", stats["article_count"])
    except Exception as e:
        log.warning("⚠️ 写入 stats.json 失败（已忽略）: %s", e)

    # 💾 流程成功完成，清理 checkpoint（checkpoint.remove 已做防御，不会抛）
    ckpt.remove()
    log.info("💾 Checkpoint 已清理（流程完成）")

    log.info("=" * 55)
    log.info("☀️  AI 早报生成完毕！共 %s 条 AI 资讯", len(all_items))
    log.info("=" * 55)


if __name__ == '__main__':
    main()
