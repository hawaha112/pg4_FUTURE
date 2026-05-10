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

import concurrent.futures
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
    item['_entities'] = event.get('entity_tags', []) or []

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


def _already_rendered_in_shift(event: dict, window_start) -> bool:
    """事件是否已在本班次窗口起点之后渲染过 — 用于跨 am/pm 班次去重。

    若事件在 window_start 之后已被渲染过 (rendered_at)，默认跳过；
    但若渲染之后又有新 evidence 进来 (last_updated_at > rendered_at)，
    视为"重新激活"，仍然允许再次出现。

    背景: commit 093475c 修复——_event_in_window 放行带新跟进的旧事件后，
    导致同一事件同时出现在早晚两班；本函数是其后置过滤器。
    """
    rendered = event.get('rendered_at')
    if not rendered:
        return False
    try:
        r_dt = datetime.fromisoformat(rendered.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        return False
    if r_dt.astimezone() < window_start:
        return False  # 早于本班次窗口起点，可以重渲
    last_upd = event.get('last_updated_at') or ''
    try:
        u_dt = datetime.fromisoformat(last_upd.replace('Z', '+00:00'))
        if u_dt > r_dt:
            return False  # 渲染后又被更新过，重新激活
    except (ValueError, TypeError, AttributeError):
        pass
    return True


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

    # ── 班次固定日历窗口（按发布时间过滤，而非被采集时间） ──
    # 早班 am：昨天 18:00 → 今天 06:00（12h 夜间）
    # 晚班 pm：今天 06:00 → 今天 18:00（12h 白天）
    # 没 shift 时走"滚动 N 小时"兼容手动跑
    import os as _os
    from datetime import time as _dtime, timedelta as _td, timezone as _tz
    _shift = _os.environ.get('BRIEFING_SHIFT', '').lower()
    window_start = window_end = None
    if _shift in ('am', 'pm'):
        _now = datetime.now().astimezone()
        _today = _now.date()
        if _shift == 'am':
            window_start = datetime.combine(_today - _td(days=1), _dtime(18, 0)).astimezone()
            window_end = datetime.combine(_today, _dtime(6, 0)).astimezone()
        else:
            window_start = datetime.combine(_today, _dtime(6, 0)).astimezone()
            window_end = datetime.combine(_today, _dtime(18, 0)).astimezone()
        log.info("🕘 发布时间窗口 [%s]: %s ~ %s",
                 _shift, window_start.strftime('%Y-%m-%d %H:%M'),
                 window_end.strftime('%Y-%m-%d %H:%M'))
        # 用一个宽松点的 first_seen_at 窗口（24h）把近期建库事件都捞进来，
        # 再在下面按 published_at 精筛
        hours = max(hours, 24)

    def _in_window(pub_raw) -> bool:
        """pub_raw 可以是 datetime、str（ISO） 或 None"""
        if window_start is None:
            return True  # 无 shift 时放行所有（hours 滚动窗模式）
        if not pub_raw:
            return False  # 无发布时间的文章排除（保守）
        try:
            if isinstance(pub_raw, str):
                pub_dt = datetime.fromisoformat(pub_raw.replace('Z', '+00:00'))
            else:
                pub_dt = pub_raw
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=_tz.utc)
            pub_local = pub_dt.astimezone()
            return window_start <= pub_local < window_end
        except (ValueError, TypeError, AttributeError):
            return False

    def _event_in_window(event) -> bool:
        """事件视角的窗口判断：以 canonical event 的 published_at 为准（严格）。

        曾试过放行任一 evidence 在窗口内的事件，意图是捕捉"昨天首发、今天跟进"
        的多源场景；但用户实测看到 04-28 文章出现在 04-29 晚报里，体验崩了。
        现在收窄回严格语义：只展示窗口内首次发布的事件。

        副作用：跨日多源跟进的事件不会再次进入新班次的早报，
        multi_source_count 因此可能偏低 —— 是有意识的取舍：用户对"今天的
        早报必须真是当天发布的内容"的预期 > 多源指标完整度。
        """
        if window_start is None:
            return True
        return _in_window(event.get('published_at'))

    # ── 尝试使用 canonical events（新模式） ──
    canonical_events = store.get_canonical_events_for_briefing(
        hours=hours, min_importance=0
    )

    # 按发布时间精筛到班次日历窗口（有 shift 时）
    # 用 _event_in_window：事件 evidence_chain 里有任一条目在班次窗口内即放行，
    # 这样旧事件的新源跟进也能进入对应班次（多源事件统计才会非零）
    if window_start is not None:
        before = len(canonical_events)
        canonical_events = [e for e in canonical_events if _event_in_window(e)]
        log.info("🕘 按发布时间过滤：%d → %d 条（窗口外 %d 条被排除）",
                 before, len(canonical_events), before - len(canonical_events))

        # 已在本班次窗口开始之后渲染过的事件，跳过 — 防止早班/晚班重复展示同一事件
        before_dedup = len(canonical_events)
        canonical_events = [
            e for e in canonical_events
            if not _already_rendered_in_shift(e, window_start)
        ]
        dedup_dropped = before_dedup - len(canonical_events)
        if dedup_dropped > 0:
            log.info("🔁 跳过本班次窗口内已渲染过的 %d 个事件（去重，避免早晚报重复）",
                     dedup_dropped)

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
        # 触发场景：① 事件库还没积累出 canonical_events（冷启动）；
        # ② 本班次窗口内的 canonical_events 全在早些时候渲染过（再 dispatch
        # 同班次时去重逻辑过滤掉）。两种情况都不是 bug，只是改走 article 路径。
        log.info("🔁 canonical events 为空（可能本班已渲染过），改用文章模式")
        events = store.get_events_for_briefing(
            hours=hours, min_importance=0,
            include_unanalyzed_tier0=True,
        )
        log.info("📦 从事件库读取 %d 条文章（最近 %d 小时）", len(events), hours)

        # 班次窗口过滤：早晚班严格按 published_at 落在 12h 班次窗口
        # （否则 fallback 会把过期班次的文章混进来，比如早报里出现 5-6 的内容）
        if window_start is not None:
            before_window = len(events)
            events = [e for e in events if _in_window(
                e.get('published_at') or e.get('published') or e.get('collected_at')
            )]
            dropped = before_window - len(events)
            if dropped > 0:
                log.info("🕘 fallback 按班次窗口过滤：%d → %d 条（窗口外 %d 条排除）",
                         before_window, len(events), dropped)

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
                max_workers = max(1, int(config.get('llm', {}).get('max_workers', 4)))
                log.info("🔄 尝试升级 %d 条 Level 0 高价值条目（并发 %d）...",
                         len(level0_upgrade_candidates), max_workers)

                def _is_rate_limit_err(msg: str) -> bool:
                    em = (msg or '').lower()
                    return any(k in em for k in (
                        '429', 'rate_limit', 'rate limit', 'overloaded',
                        'quota', 'too many requests', 'usage limit',
                    ))

                def _upgrade_one(item):
                    try:
                        result = analyzer.analyze_article(
                            title=item.get('title', ''),
                            summary=item.get('summary', ''),
                            full_text=item.get('full_text', ''),
                            source_name=item.get('source_name', ''),
                        )
                        if result and result.get('ai_relevant') and result.get('summary'):
                            result['_analysis_level'] = 1
                            return item, result, None
                    except Exception as e:
                        return item, None, str(e)
                    return item, None, None

                upgraded = 0
                rate_limit_hit = False
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = [pool.submit(_upgrade_one, it) for it in level0_upgrade_candidates]
                    for fut in concurrent.futures.as_completed(futures):
                        try:
                            item, result, err = fut.result()
                        except concurrent.futures.CancelledError:
                            continue
                        if err and _is_rate_limit_err(err):
                            if not rate_limit_hit:
                                rate_limit_hit = True
                                log.warning("🚨 Level 0 升级命中限流（%s），剩余条目跳过，"
                                            "直接用规则分析结果", err[:80])
                                for f in futures:
                                    if not f.done():
                                        f.cancel()
                            continue
                        if err:
                            log.warning("  ⚠️ 升级失败 (%s): %s",
                                        item.get('title', '')[:30], err)
                            continue
                        if result is None:
                            continue
                        item['analysis'] = result
                        url = item.get('url') or item.get('link', '')
                        if url:
                            store.save_analysis(url, result)
                        upgraded += 1

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
    # 检查速览是否有效（排除 fallback 占位符与过短内容）
    # 只匹配完整 fallback 短语 — 旧版黑名单里单独列"失败"/"错误"，但这俩
    # 在正常 AI 新闻里太常见（"创业失败"/"训练错误"），会误杀 LLM 速览。
    _FALLBACK_PHRASES = ('速览生成失败', '速览生成错误', '今天暂无重要')
    _is_valid_editorial = (
        editorial
        and len(editorial) >= 20
        and not any(p in editorial for p in _FALLBACK_PHRASES)
    )
    log.info("📝 editorial 校验: valid=%s, len=%d, head=%r",
             _is_valid_editorial, len(editorial or ''), (editorial or '')[:120])

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

    # ── Tier 1：当期内容价值指标（用于 dashboard）──
    # 重要事件：importance >= 4
    important_count = sum(
        1 for item in all_items
        if (item.get('analysis', {}) or {}).get('importance', 0) >= 4
    )
    # 官方/原厂事件：event_status == 'official'
    official_count = sum(
        1 for item in all_items if item.get('_event_status', '') == 'official'
    )
    # 深度分析：detailed_content 和 background 都有内容
    def _has_depth(item) -> bool:
        a = item.get('analysis', {}) or {}
        return bool((a.get('detailed_content') or '').strip()
                    and (a.get('background') or '').strip())
    depth_count = sum(1 for item in all_items if _has_depth(item))
    # 覆盖实体：所有 item 的 _entities 去重
    entity_set = set()
    for item in all_items:
        for eid in item.get('_entities', []) or []:
            if eid:
                entity_set.add(eid)
    entity_count = len(entity_set)

    meta = {
        'llm_coverage': llm_coverage,
        'llm_count': llm_count,
        'multi_source_count': multi_source_count,
        'important_count': important_count,
        'official_count': official_count,
        'depth_count': depth_count,
        'entity_count': entity_count,
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

        # ── 重要事件清单（importance ≥ 4）—— dashboard 渲染可点击列表用
        # 链接到本期归档页（archive/YYYY-MM-DD-<shift>.html）
        import os as _os_iev
        _today_iev = datetime.now().strftime('%Y-%m-%d')
        _shift_iev = _os_iev.environ.get('BRIEFING_SHIFT', '')
        _archive_url = (
            f"archive/{_today_iev}-{_shift_iev}.html"
            if _shift_iev in ('am', 'pm') else 'index.html'
        )
        important_events_list = []
        for item in all_items:
            a = item.get('analysis', {}) or {}
            if a.get('importance', 0) >= 4:
                important_events_list.append({
                    'title': a.get('chinese_title') or item.get('title') or '',
                    'event_id': item.get('_canonical_event_id', '') or item.get('link', ''),
                    'importance': int(a.get('importance', 0)),
                    'source_name': item.get('source_name', ''),
                    'archive_url': _archive_url,
                })

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
            "important_count": important_count,
            "important_events": important_events_list,
            "official_count": official_count,
            "depth_count": depth_count,
            "entity_count": entity_count,
        }
        # 合并 renderer 端的 LLM 用量（generate_digest 用了 LLM）
        try:
            if 'analyzer' in locals() and analyzer is not None:
                stats.update(analyzer.usage_stats())
        except Exception:
            pass
        # 合并 collector 阶段的 LLM 用量 — 上一步写了 collector_llm_usage.json
        # 文件 (collector.py 跑完写)。renderer 阶段读, 累加
        try:
            collector_usage_path = script_dir / 'output' / '.collector_llm_usage.json'
            if collector_usage_path.exists():
                cu = json.loads(collector_usage_path.read_text(encoding='utf-8'))
                for k in ('llm_call_count', 'llm_prompt_tokens',
                          'llm_completion_tokens', 'llm_total_tokens',
                          'llm_parse_fallback'):
                    stats[k] = stats.get(k, 0) + int(cu.get(k, 0) or 0)
        except Exception as e:
            log.warning("⚠️ 读 collector usage 失败: %s", e)
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        log.info("📊 stats.json: %d 条", stats["article_count"])
    except Exception as e:
        log.warning("⚠️ stats.json 写入失败: %s", e)

    # ══════════════════════════════════════════════════════════
    # 每日重要事项归档（JSON + MD，供未来周报项目消费）
    # 位置：ai-morning-news/archive/daily_digest/YYYY-MM-DD.{json,md}
    # 不在 output/ 下 → 不会被 run_daily.sh 推到公开部署仓库
    # ══════════════════════════════════════════════════════════
    try:
        import os as _os
        today = datetime.now().strftime('%Y-%m-%d')
        shift = _os.environ.get('BRIEFING_SHIFT', '')
        suffix = f'-{shift}' if shift in ('am', 'pm') else ''
        digest_dir = script_dir / 'archive' / 'daily_digest'
        digest_dir.mkdir(parents=True, exist_ok=True)

        archive_items = []
        for item in all_items:
            a = item.get('analysis', {}) or {}
            if a.get('ai_relevant') is False:
                continue
            pub = item.get('published')
            pub_iso = pub.isoformat() if hasattr(pub, 'isoformat') else (str(pub) if pub else '')
            archive_items.append({
                'importance':     a.get('importance', 0),
                'chinese_title':  a.get('chinese_title') or item.get('title', '')[:80],
                'title':          item.get('title', ''),
                'summary':        a.get('summary', ''),
                'why_it_matters': a.get('why_it_matters', ''),
                'detailed_content': a.get('detailed_content', ''),
                'deep_analysis':  a.get('deep_analysis', ''),
                'background':     a.get('background', ''),
                'categories':     a.get('categories', []),
                'audience':       a.get('audience', []),
                'source_name':    item.get('source_name', ''),
                'source_tier':    item.get('source_tier', 2),
                'source_type':    a.get('source_type', ''),
                'link':           item.get('link', ''),
                'published':      pub_iso,
                'cluster_size':   item.get('_cluster_size', 1),
                'event_status':   item.get('_event_status', 'unknown'),
                'event_id':       item.get('_event_id', ''),
                '_analysis_level': a.get('_analysis_level', 0),
            })
        archive_items.sort(key=lambda x: (-x['importance'], -x['cluster_size']))

        archive_json = {
            'date': today,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'total_items': len(archive_items),
            'digest_editorial': digest.get('editorial', '') if digest else '',
            'big_event_count': sum(1 for x in archive_items if x['importance'] >= 4),
            'items': archive_items,
        }
        with open(digest_dir / f'{today}{suffix}.json', 'w', encoding='utf-8') as f:
            json.dump(archive_json, f, ensure_ascii=False, indent=2)

        # Markdown 人读版
        md = [f'# AI 早报 · {today}', '']
        if archive_json['digest_editorial']:
            md.extend(['## 今日速览', '', archive_json['digest_editorial'], ''])
        big_ones = [x for x in archive_items if x['importance'] >= 4]
        if big_ones:
            md.append(f'## 重大事件（{len(big_ones)} 条）')
            md.append('')
            for i, it in enumerate(big_ones, 1):
                md.append(f"### {i}. {it['chinese_title']}")
                md.append('')
                md.append(
                    f"**重要性**: {'⭐' * it['importance']} · "
                    f"**来源**: {it['source_name']} (tier {it['source_tier']}) · "
                    f"**多源**: {it['cluster_size']}"
                )
                md.append('')
                if it['why_it_matters']:
                    md.extend([f"**为什么重要**: {it['why_it_matters']}", ''])
                if it['deep_analysis']:
                    md.extend(['**深度解读**:', '', it['deep_analysis'][:1500], ''])
                if it['link']:
                    md.extend([f"[阅读原文]({it['link']})", ''])
                md.extend(['---', ''])
        minor = [x for x in archive_items if x['importance'] < 4]
        if minor:
            md.extend([f'## 次要条目（{len(minor)} 条，供周报检索）', ''])
            for i, it in enumerate(minor, 1):
                title = it['chinese_title'] or it['title'][:80]
                why = it['why_it_matters'] or it['summary'][:100]
                md.append(f"- **[{i}]** {title} · *{it['source_name']}* (importance={it['importance']})")
                if why:
                    md.append(f"  - {why}")
            md.append('')
        with open(digest_dir / f'{today}{suffix}.md', 'w', encoding='utf-8') as f:
            f.write('\n'.join(md))

        log.info("📦 每日归档: %s%s（总 %d 条，重大 %d 条）",
                 today, suffix, len(archive_items), len(big_ones))
    except Exception as e:
        log.warning("⚠️ 每日归档写入失败: %s", e)

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
