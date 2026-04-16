#!/usr/bin/env python3
"""
briefing_renderer.py — 出报入口 v2

职责：从事件库读取 canonical events → 排序 → 渲染 evidence chain + status badge → HTML
与采集（collector.py）彻底分离，每天早上运行一次。

v2 改进：
- 优先使用 canonical events（带证据链 + 事件状态）
- 回退兼容：如无 canonical events，使用传统文章模式
- 渲染 evidence chain：显示谁先报道、谁确认、官方源
- 渲染 status badge：rumor / reported / confirmed / official
- 渲染 cluster_size：多源报道数量标记

用法:
    python3 briefing_renderer.py              # 生成早报页面
    python3 briefing_renderer.py --open       # 生成并在浏览器中打开
    python3 briefing_renderer.py --hours 12   # 只取最近 12 小时的事件
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from logger import get_logger
log = get_logger('renderer')

from event_store import EventStore
from html_generator import generate_html
from source_ranker import SourceRanker
from llm_analyzer import LLMAnalyzer, create_analyzer_from_config
from config_validator import validate_and_warn


# ─── 状态展示配置 ────────────────────────────────────────────
STATUS_DISPLAY = {
    'rumor': {'label': '传闻', 'label_en': 'Rumor', 'color': '#f59e0b', 'icon': '🔮'},
    'reported': {'label': '已报道', 'label_en': 'Reported', 'color': '#3b82f6', 'icon': '📰'},
    'confirmed': {'label': '已确认', 'label_en': 'Confirmed', 'color': '#10b981', 'icon': '✅'},
    'official': {'label': '官方', 'label_en': 'Official', 'color': '#8b5cf6', 'icon': '🏛️'},
}

ROLE_DISPLAY = {
    'first_reporter': {'label': '首发', 'label_en': 'First'},
    'official': {'label': '官方', 'label_en': 'Official'},
    'confirmer': {'label': '确认', 'label_en': 'Confirmed'},
    'follower': {'label': '跟进', 'label_en': 'Follow-up'},
}


def _parse_hours_arg() -> int:
    for i, arg in enumerate(sys.argv):
        if arg == '--hours' and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return 24


def _has_substance(analysis: dict) -> bool:
    """质量过滤：确保分析结果有实质内容

    Level 0（规则分析）结果用中等标准：summary ≥ 20 字且 key_details ≥ 2 条。
    Level 1+（LLM 分析）结果用严格标准：需要 background/deep_analysis/detailed_content 之一。
    """
    s = (analysis.get('summary') or '').strip()
    if len(s) < 20:
        return False

    # Level 0 规则分析：summary ≥ 20 字且 key_details ≥ 2 条有实质内容
    analysis_level = analysis.get('_analysis_level', -1)
    if analysis_level == 0:
        kd = analysis.get('key_details') or []
        meaningful_kd = [k for k in kd if isinstance(k, str) and len(k.strip()) >= 10]
        return len(meaningful_kd) >= 2

    # Level 1+ LLM 分析：需要更充实的内容
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


def _enrich_item_with_event_info(item: dict, event: dict) -> dict:
    """将 canonical event 信息注入到渲染 item 中

    添加字段：
    - _event_status / _event_status_display: 事件状态
    - _evidence_chain: 证据链列表
    - _cluster_size: 多源报道数
    - _also_reported_by: 其他报道来源
    - _report_count: 报道来源数量
    - _event_type: 事件类型
    """
    status = event.get('status', 'reported')
    status_info = STATUS_DISPLAY.get(status, STATUS_DISPLAY['reported'])

    item['_event_status'] = status
    item['_event_status_display'] = status_info
    item['_event_type'] = event.get('event_type', 'news')
    item['_cluster_size'] = event.get('cluster_size', 1)
    item['_canonical_event_id'] = event.get('event_id', '')

    # 证据链
    evidence_chain = event.get('evidence_chain', [])
    enriched_chain = []
    for ev in evidence_chain:
        role = ev.get('role', 'follower')
        role_info = ROLE_DISPLAY.get(role, ROLE_DISPLAY['follower'])
        enriched_chain.append({
            **ev,
            'role_display': role_info,
        })
    item['_evidence_chain'] = enriched_chain

    # 从证据链提取 also_reported_by
    if len(evidence_chain) > 1:
        canonical_source = item.get('source_name', '')
        other_sources = list(set(
            ev['source_name'] for ev in evidence_chain
            if ev.get('source_name') and ev['source_name'] != canonical_source
        ))
        if other_sources:
            item['_also_reported_by'] = other_sources
            item['_report_count'] = len(evidence_chain)

    return item


def main():
    script_dir = Path(__file__).parent
    config_path = script_dir / 'config.json'

    log.info("=" * 55)
    log.info("📰 出报器 v2 — canonical events + evidence chain")
    log.info("=" * 55)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    validate_and_warn(config)

    settings = config['settings']
    hours = _parse_hours_arg()

    event_db_path = str(script_dir / settings.get('event_db', 'events.db'))
    store = EventStore(event_db_path)

    # ── 尝试使用 canonical events（新模式） ──
    canonical_events = store.get_canonical_events_for_briefing(
        hours=hours, min_importance=0
    )

    use_canonical = len(canonical_events) > 0
    if use_canonical:
        log.info("📦 从事件库读取 %d 个 canonical events（最近 %d 小时）",
                 len(canonical_events), hours)

        # 质量过滤（Tier 0 / official 状态豁免）
        before_quality = len(canonical_events)
        filtered_events = []
        for event in canonical_events:
            analysis = event.get('analysis', {})
            status = event.get('status', '')
            # 查 canonical article 的 tier
            tier = 2
            if event.get('article'):
                tier = event['article'].get('source_tier', 2)

            if tier == 0 or status == 'official':
                filtered_events.append(event)
            elif _has_substance(analysis):
                filtered_events.append(event)
        quality_dropped = before_quality - len(filtered_events)
        if quality_dropped > 0:
            log.info("🧹 质量过滤移除 %d 个事件（Tier 0 / official 豁免）",
                     quality_dropped)
        canonical_events = filtered_events

        # 转换为 ranker/html_generator 期望的格式
        all_items = []
        for event in canonical_events:
            article = event.get('article', {})
            item = {
                'url': event.get('canonical_url', '') or article.get('url', ''),
                'link': event.get('canonical_url', '') or article.get('url', ''),
                'title': event.get('title', '') or article.get('title', ''),
                'summary': article.get('summary', ''),
                'full_text': article.get('full_text', ''),
                'published': event.get('published_at') or event.get('published') or article.get('published_at') or article.get('published') or article.get('collected_at') or event.get('first_seen_at'),
                'source_name': event.get('canonical_source', '') or article.get('source_name', ''),
                'source_icon': event.get('source_icon', '') or article.get('source_icon', ''),
                'source_color': event.get('source_color', '') or article.get('source_color', ''),
                'source_category': event.get('source_category', '') or article.get('source_category', ''),
                'source_tier': article.get('source_tier', 2),
                'image': event.get('image_url', '') or article.get('image_url', ''),
                'analysis': event.get('analysis', {}) or article.get('analysis', {}),
                '_is_new': True,
                '_event_id': event.get('canonical_article_id', '') or article.get('id', ''),
            }
            # 提取证据链中的来源列表，供 ranker.enrich_cluster_info 使用
            evidence_chain = event.get('evidence_chain', [])
            cluster_sources = list(set(
                ev.get('source_name', '') for ev in evidence_chain
                if ev.get('source_name')
            ))
            item['_cluster_sources'] = cluster_sources
            item['_cluster_size'] = max(event.get('cluster_size', 1), len(cluster_sources))
            item = _enrich_item_with_event_info(item, event)
            all_items.append(item)

    else:
        # ── 回退：传统文章模式 ──
        log.info("⚠️ 无 canonical events，回退到传统文章模式")
        events = store.get_events_for_briefing(
            hours=hours, min_importance=0,
            include_unanalyzed_tier0=True,
        )
        log.info("📦 从事件库读取 %d 条文章（最近 %d 小时）", len(events), hours)

        if not events:
            log.warning("⚠️ 事件库中无可用事件，跳过出报")
            store.close()
            return

        # 质量过滤
        before_quality = len(events)
        filtered_events = []
        for event in events:
            tier = event.get('source_tier', 2)
            analysis = event.get('analysis', {})
            if tier == 0:
                filtered_events.append(event)
            elif _has_substance(analysis):
                filtered_events.append(event)
        quality_dropped = before_quality - len(filtered_events)
        if quality_dropped > 0:
            log.info("🧹 质量过滤移除 %d 条（Tier 0 豁免）", quality_dropped)
        events = filtered_events

        all_items = []
        for event in events:
            item = {
                'url': event.get('url', ''),
                'link': event.get('url', ''),
                'title': event.get('title', ''),
                'summary': event.get('summary', ''),
                'full_text': event.get('full_text', ''),
                'published': event.get('published_at') or event.get('published') or event.get('collected_at'),
                'source_name': event.get('source_name', ''),
                'source_icon': event.get('source_icon', ''),
                'source_color': event.get('source_color', ''),
                'source_category': event.get('source_category', ''),
                'source_tier': event.get('source_tier', 2),
                'image': event.get('image_url', ''),
                'analysis': event.get('analysis', {}),
                '_is_new': True,
                '_event_id': event.get('id', ''),
            }
            all_items.append(item)

    if not all_items:
        log.warning("⚠️ 无可渲染的事件，跳过出报")
        store.close()
        return

    # ── 排序 ──
    ranker = SourceRanker(config)
    all_items = ranker.score_and_filter(all_items, min_authority=0)
    all_items = ranker.enrich_cluster_info(all_items)
    all_items = ranker.sort_by_relevance(all_items)

    # ── LLM 升级：对 Level 0 高价值条目尝试 LLM 分析 ──
    # 这些条目来自之前的采集轮次，只有规则分析，缺少中文标题和深度内容
    level0_upgrade_candidates = [
        item for item in all_items
        if item.get('analysis', {}).get('_analysis_level', 0) == 0
        and item.get('analysis', {}).get('importance', 0) >= 2
        and item.get('analysis', {}).get('ai_relevant', False)
    ]
    if level0_upgrade_candidates:
        try:
            analyzer = create_analyzer_from_config(config)
            if analyzer:
                log.info("🔄 尝试升级 %d 条 Level 0 高价值条目...", len(level0_upgrade_candidates))
                upgraded = 0
                for item in level0_upgrade_candidates:
                    try:
                        result = analyzer.analyze_article(
                            title=item.get('title', ''),
                            summary=item.get('summary', ''),
                            full_text=item.get('full_text', ''),
                            source_name=item.get('source_name', ''),
                        )
                        if result and result.get('ai_relevant') and result.get('summary'):
                            result['_analysis_level'] = 1
                            item['analysis'] = result
                            # 同步更新到事件库
                            url = item.get('url') or item.get('link', '')
                            if url:
                                store.save_analysis(url, result)
                            upgraded += 1
                    except Exception as e:
                        log.warning("  ⚠️ 升级失败 (%s): %s",
                                    item.get('title', '')[:30], e)
                if upgraded:
                    store.commit()
                    log.info("  ✅ 成功升级 %d / %d 条", upgraded, len(level0_upgrade_candidates))
        except Exception as e:
            log.warning("⚠️ Level 0 升级跳过: %s", e)

    # ── 质量过滤：排除内容不充分的文章 ──
    # Level 1+（LLM 分析）: 通过（有深度内容）
    # Level 0 + importance >= 2 + summary >= 20字: 通过（虽无深度但有基本信息）
    # Level 0 + importance < 2: 排除（低价值无分析）
    before_count = len(all_items)
    def _quality_check(item):
        a = item.get('analysis', {})
        level = a.get('_analysis_level', 0)
        if level >= 1:
            return _has_substance(a)
        # Level 0: 要求基本质量
        imp = a.get('importance', 0)
        summary = (a.get('summary') or '').strip()
        return imp >= 2 and len(summary) >= 20
    all_items = [item for item in all_items if _quality_check(item)]
    filtered_count = before_count - len(all_items)
    if filtered_count:
        log.info("🔽 质量过滤：移除 %d 条内容不充分的文章", filtered_count)

    # ── 生成今日速览（带缓存） ──
    digest = {"editorial": "", "top_stories": []}
    digest_cache_path = script_dir / 'output' / '.digest_cache.json'

    # 检查是否有 LLM 分析结果（Level 1+），如果全是 Level 0 则跳过
    has_llm_content = any(
        item.get('analysis', {}).get('_analysis_level', -1) >= 1
        for item in all_items
    )

    if has_llm_content:
        try:
            analyzer = create_analyzer_from_config(config)
            if analyzer and len(all_items) >= 3:
                log.info("📝 生成今日速览...")
                digest = analyzer.generate_digest(all_items)
                # 缓存成功的速览，避免重渲染丢失
                if digest.get('editorial'):
                    try:
                        digest_cache_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(digest_cache_path, 'w', encoding='utf-8') as f:
                            json.dump(digest, f, ensure_ascii=False)
                        log.info("  💾 速览已缓存")
                    except Exception:
                        pass
        except Exception as e:
            log.error("❌ 速览生成失败: %s", e)
            # 尝试从缓存恢复
            if digest_cache_path.exists():
                try:
                    with open(digest_cache_path, 'r', encoding='utf-8') as f:
                        digest = json.load(f)
                    log.info("  ♻️ 从缓存恢复速览")
                except Exception:
                    pass

    if not digest.get('editorial'):
        # 尝试从缓存恢复
        if digest_cache_path.exists():
            try:
                with open(digest_cache_path, 'r', encoding='utf-8') as f:
                    digest = json.load(f)
                log.info("  ♻️ 从缓存恢复速览")
            except Exception:
                pass

    editorial = digest.get('editorial', '')
    # 检查速览是否有效（排除失败占位符和过短内容）
    _is_valid_editorial = (
        editorial
        and len(editorial) >= 20
        and '失败' not in editorial
        and '错误' not in editorial
    )

    if not _is_valid_editorial:
        # 从已有分析中提取关键信息，生成规则版速览
        n_items = len(all_items)
        # 收集高重要性事件的中文标题
        top_headlines = []
        for item in all_items:
            a = item.get('analysis', {})
            imp = a.get('importance', 0)
            ct = a.get('chinese_title', '')
            if imp >= 4 and ct and len(ct) >= 5:
                top_headlines.append(ct)
        top_headlines = top_headlines[:3]

        top_sources = set()
        for item in all_items[:10]:
            src = item.get('source_name', '')
            if src:
                top_sources.add(src)

        if top_headlines:
            digest['editorial'] = (
                f"今日共收录 {n_items} 条 AI 资讯。"
                f"重点关注：{'；'.join(top_headlines)}。"
                f"信息源覆盖 {', '.join(list(top_sources)[:4])} 等。"
            )
        else:
            digest['editorial'] = (
                f"今日共收录 {n_items} 条 AI 资讯"
                f"（来源含 {', '.join(list(top_sources)[:3])} 等）。"
            )
        log.info("📝 规则版速览（LLM 速览不可用或失败）")

    # ── 计算 LLM 覆盖率（level >= 1 = 来自 LLM 的深度分析）──
    llm_count = sum(
        1 for item in all_items
        if item.get('analysis', {}).get('_analysis_level', 0) >= 1
    )
    llm_coverage = llm_count / len(all_items) if all_items else 0.0
    log.info("🧠 LLM 覆盖率: %d / %d = %.0f%%",
             llm_count, len(all_items), llm_coverage * 100)

    # 统计多源事件
    multi_source_count = sum(
        1 for item in all_items if item.get('_cluster_size', 1) >= 2
    )

    meta = {
        'llm_coverage': llm_coverage,
        'llm_count': llm_count,
        'multi_source_count': multi_source_count,
    }

    # ── 生成 HTML ──
    log.info("🎨 生成页面（%d 条）...", len(all_items))
    html, modal_js = generate_html(all_items, config, digest, meta=meta)

    output_path = script_dir / settings.get('output_file', 'output/index.html')
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    modal_js_path = output_path.parent / 'modal_data.js'
    with open(modal_js_path, 'w', encoding='utf-8') as f:
        f.write(modal_js)

    html_kb = output_path.stat().st_size / 1024
    modal_kb = modal_js_path.stat().st_size / 1024
    log.info("✅ 页面已生成: %s (%.1f KB + modal %.1f KB)", output_path, html_kb, modal_kb)

    # 复制到上级目录
    parent_copy = script_dir.parent / 'index.html'
    parent_modal = script_dir.parent / 'modal_data.js'
    try:
        with open(parent_copy, 'w', encoding='utf-8') as f:
            f.write(html)
        with open(parent_modal, 'w', encoding='utf-8') as f:
            f.write(modal_js)
    except (PermissionError, OSError):
        pass

    if '--open' in sys.argv:
        import webbrowser
        webbrowser.open(f'file://{output_path.resolve()}')

    # 写 stats.json
    try:
        stats_path = script_dir / 'output' / 'stats.json'
        stats_path.parent.mkdir(parents=True, exist_ok=True)

        event_stats = store.stats()
        # 统计事件状态分布
        status_summary = {}
        for item in all_items:
            status = item.get('_event_status', 'unknown')
            status_summary[status] = status_summary.get(status, 0) + 1

        stats = {
            "article_count": len(all_items),
            "output_file": str(output_path.relative_to(script_dir)),
            "file_size_kb": round(output_path.stat().st_size / 1024, 1),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "event_db_stats": event_stats,
            "event_status_distribution": status_summary,
            "use_canonical_events": use_canonical,
            "llm_coverage": round(llm_coverage, 3),
            "llm_count": llm_count,
            "multi_source_count": multi_source_count,
        }
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        log.info("📊 stats.json: %d 条", stats["article_count"])
    except Exception as e:
        log.warning("⚠️ stats.json 写入失败: %s", e)

    # 标记已渲染
    if use_canonical:
        ce_ids = [item['_canonical_event_id'] for item in all_items
                  if item.get('_canonical_event_id')]
        if ce_ids:
            store.mark_canonical_rendered(ce_ids)

    event_ids = [item['_event_id'] for item in all_items if item.get('_event_id')]
    if event_ids:
        store.mark_rendered(event_ids)

    store.close()

    # 输出事件状态分布
    if use_canonical:
        status_parts = []
        for s in ('official', 'confirmed', 'reported', 'rumor'):
            count = status_summary.get(s, 0)
            if count > 0:
                info = STATUS_DISPLAY.get(s, {})
                status_parts.append(f"{info.get('icon', '')} {info.get('label', s)}: {count}")
        if status_parts:
            log.info("📊 事件状态: %s", ' | '.join(status_parts))

    log.info("=" * 55)
    log.info("☀️  早报生成完毕！共 %d 条 AI 资讯", len(all_items))
    log.info("=" * 55)


if __name__ == '__main__':
    main()
