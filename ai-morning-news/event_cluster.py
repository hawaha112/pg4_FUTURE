#!/usr/bin/env python3
"""
event_cluster.py — 事件聚类引擎：保留证据链，取代 winner-only 去重

核心理念：
  重复报道本身有价值 —— 它代表确认度。
  不再"去重"（丢弃重复），而是"聚类"（合并为同一事件 + 保留证据链）。

工作流程：
  1. 接收去重后的文章列表（DedupEngine 仍做第一层精确去重）
  2. 对已分析文章做语义聚类，找到描述同一事件的多篇报道
  3. 为每个聚类创建/更新 canonical event
  4. 每篇报道作为 evidence 关联到事件，标记角色（first_reporter / follower / confirmer / official）
  5. 根据证据链自动升级事件状态（rumor → reported → confirmed → official）

用法:
    clusterer = EventClusterer(store, dedup_engine)
    stats = clusterer.cluster_and_link(articles)
"""

import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from logger import get_logger
log = get_logger('event_cluster')

from dedup_engine import (
    tokenize, text_hash, MinHashLSH, TFIDFMatcher,
    compute_minhash, minhash_jaccard, _is_mostly_cjk,
)
from event_store import EventStore, _url_hash


# 角色判断规则
_ROLE_PRIORITY = {
    'official': 0,
    'first_reporter': 1,
    'confirmer': 2,
    'follower': 3,
}


def _determine_role(article: dict, is_first: bool, existing_sources: Set[str]) -> str:
    """判断文章在事件中的角色

    Args:
        article: 文章 dict
        is_first: 是否是该事件的第一篇文章
        existing_sources: 已有证据的来源集合

    Returns:
        角色字符串: 'official' | 'first_reporter' | 'confirmer' | 'follower'
    """
    tier = article.get('source_tier', 2)
    source_type = ''
    analysis = article.get('analysis', {})
    if isinstance(analysis, dict):
        source_type = analysis.get('source_type', '')

    # Tier 0 官方源 → official
    if tier == 0:
        return 'official'

    # 来源类型标记为 official/paper → official
    if source_type in ('official', 'paper'):
        return 'official'

    # 第一个报道者 → first_reporter
    if is_first:
        return 'first_reporter'

    # 来源不在已有证据中 → confirmer（新的独立来源确认）
    source = article.get('source_name', '')
    if source and source not in existing_sources:
        return 'confirmer'

    # 同源后续报道 → follower
    return 'follower'


def _pick_canonical(articles: List[dict]) -> dict:
    """从同事件多篇报道中选出 canonical（最佳代表）文章

    优先级：
    1. Tier 最低（官方源优先）
    2. 有完整分析
    3. 有正文内容
    4. 重要性分数最高
    5. 标题最长（通常信息量更大）
    """
    def score(art):
        tier = art.get('source_tier', 2)
        has_analysis = 1 if art.get('analysis') and isinstance(art['analysis'], dict) else 0
        has_body = 1 if art.get('full_text', '') else 0
        importance = 0
        if isinstance(art.get('analysis'), dict):
            importance = art['analysis'].get('importance', 0)
        title_len = min(len(art.get('title', '')), 100) / 100
        # 低 tier 得分高（取负值让排序正确）
        return (-tier * 10, has_analysis * 5, has_body * 3,
                importance * 2, title_len)

    return max(articles, key=score)


def _generate_event_id(articles: List[dict]) -> str:
    """为聚类生成稳定的事件 ID

    基于聚类中最早文章的 URL hash，保证幂等。
    """
    # 按时间排序取最早的
    earliest = min(articles, key=lambda a: a.get('collected_at', '') or '9999')
    url = earliest.get('url', '') or earliest.get('link', '')
    if url:
        return 'evt_' + _url_hash(url)[:24]
    # fallback: 基于标题
    title = earliest.get('title', '')
    return 'evt_' + hashlib.sha256(
        title.encode('utf-8')
    ).hexdigest()[:24]


def _determine_event_type(articles: List[dict]) -> str:
    """根据文章分析结果判断事件类型"""
    type_votes: Dict[str, int] = {}
    for art in articles:
        analysis = art.get('analysis', {})
        if not isinstance(analysis, dict):
            continue
        categories = analysis.get('categories', [])
        source_type = analysis.get('source_type', '')

        if source_type:
            type_votes[source_type] = type_votes.get(source_type, 0) + 1

        for cat in (categories if isinstance(categories, list) else []):
            cat_lower = cat.lower() if isinstance(cat, str) else ''
            if '发布' in cat or 'launch' in cat_lower or 'release' in cat_lower:
                type_votes['product_launch'] = type_votes.get('product_launch', 0) + 2
            elif '论文' in cat or 'paper' in cat_lower or 'research' in cat_lower:
                type_votes['research'] = type_votes.get('research', 0) + 2
            elif '融资' in cat or 'fund' in cat_lower:
                type_votes['funding'] = type_votes.get('funding', 0) + 2
            elif '监管' in cat or 'regulat' in cat_lower or 'policy' in cat_lower:
                type_votes['regulation'] = type_votes.get('regulation', 0) + 2

    if type_votes:
        return max(type_votes, key=type_votes.get)
    return 'news'


def _determine_initial_status(articles: List[dict]) -> str:
    """根据来源层级判断事件初始状态"""
    has_tier0 = any(a.get('source_tier', 2) == 0 for a in articles)
    if has_tier0:
        return 'official'

    unique_sources = set(a.get('source_name', '') for a in articles if a.get('source_name'))
    has_tier1 = any(a.get('source_tier', 2) <= 1 for a in articles)

    if len(unique_sources) >= 3 and has_tier1:
        return 'confirmed'
    if len(unique_sources) >= 2:
        return 'reported'
    return 'reported'  # 单源也标记为 reported（已有报道）


class EventClusterer:
    """事件聚类引擎

    在 DedupEngine 之上运行：
    - DedupEngine 负责精确去重（URL/标题 hash 完全匹配的丢弃）
    - EventClusterer 负责语义聚类（相似报道合并为事件 + 保留证据链）
    """

    def __init__(self, store: EventStore,
                 similarity_threshold: float = 0.55,
                 similarity_threshold_cjk: float = 0.45,
                 merge_window_hours: int = 48):
        """
        Args:
            store: EventStore 实例
            similarity_threshold: 英文聚类阈值
            similarity_threshold_cjk: 中文聚类阈值（中文标题通常更短）
            merge_window_hours: 向前查找已有事件的时间窗口
        """
        self.store = store
        self.threshold = similarity_threshold
        self.threshold_cjk = similarity_threshold_cjk
        self.merge_window_hours = merge_window_hours

    @staticmethod
    def _cluster_text(item: dict) -> str:
        """构造用于聚类匹配的文本。

        优先用 LLM 输出的 event_signature（英文规范化指纹，跨语对齐）；
        没有时退回 title + summary 前 200 字。
        article 端 signature 在 item['analysis']['event_signature']；
        canonical_event 端在 item['analysis'] 或 顶层 'event_signature'（看调用方）。
        """
        a = item.get('analysis', {}) if isinstance(item.get('analysis'), dict) else {}
        sig = (a.get('event_signature') or item.get('event_signature') or '').strip()
        title = item.get('title', '')
        summary = (item.get('summary') or '')[:200]
        # signature 重复 3 遍提高 MinHash 命中（短文本 token 太少 LSH 容易漏）
        if sig:
            return f"{sig} {sig} {sig} {title} {summary}".strip()
        return f"{title} {summary}".strip()

    def cluster_and_link(self, articles: List[dict]) -> Dict[str, int]:
        """对文章列表进行聚类并写入事件库

        流程：
        1. 加载近期已有的 canonical events（用于与新文章匹配）
        2. 对新文章做语义聚类（MinHash/LSH + TF-IDF）
        3. 尝试将新聚类合并到已有事件
        4. 创建新的 canonical events
        5. 写入 evidence 记录
        6. 自动升级事件状态

        Args:
            articles: 已分析的文章列表

        Returns:
            stats dict: {events_created, events_updated, evidence_added}
        """
        if not articles:
            return {'events_created': 0, 'events_updated': 0, 'evidence_added': 0}

        stats = {'events_created': 0, 'events_updated': 0, 'evidence_added': 0}

        # ── 1. 加载近期已有事件 ──
        existing_events = self.store.find_recent_canonical_events(
            hours=self.merge_window_hours
        )
        existing_event_map: Dict[str, dict] = {
            e['event_id']: e for e in existing_events
        }

        # 为已有事件构建 LSH 索引
        lsh = MinHashLSH()
        matcher = TFIDFMatcher()
        idx_to_event_id: Dict[int, str] = {}
        idx_to_article: Dict[int, dict] = {}

        for event in existing_events:
            # 聚类文本必须和文章端的 _cluster_text（title + summary）语言/分布一致。
            # event.summary 是 LLM 生成的中文短摘要（60-70字），article.summary 是英文原文摘要（200字）；
            # 如果用 event.summary 做 MinHash，会和新来文章的英文 summary 词汇不重合，LSH bucket 落不到同一处。
            # 始终优先用 canonical article 的 summary，让两端分布对齐。
            text = self._cluster_text(event)
            if event.get('canonical_article_id'):
                art_row = self.store.db.execute(
                    "SELECT title, summary FROM articles WHERE id = ?",
                    (event['canonical_article_id'],)
                ).fetchone()
                if art_row and art_row[1]:
                    text = f"{event.get('title', '')} {(art_row[1] or '')[:200]}".strip()
            idx = matcher.add(text)
            tokens = tokenize(text)
            lsh.add(idx, tokens)
            idx_to_event_id[idx] = event['event_id']

        # ── 2. 将文章加入索引并查找相似 ──
        article_clusters: Dict[str, List[dict]] = {}  # event_id → [articles]
        new_cluster_groups: List[List[dict]] = []  # 新聚类组
        standalone: List[dict] = []  # 独立文章（不属于任何聚类）

        # 批量索引所有文章
        article_indices: List[Tuple[int, dict]] = []
        for art in articles:
            title = art.get('title', '')
            if not title:
                standalone.append(art)
                continue
            text = self._cluster_text(art)
            idx = matcher.add(text)
            tokens = tokenize(text)
            lsh.add(idx, tokens)
            idx_to_article[idx] = art
            article_indices.append((idx, art))

        # 对每篇文章查找匹配
        matched_to_existing: Dict[int, str] = {}  # art_idx → event_id
        # 文章间聚类：使用 union-find
        parent: Dict[int, int] = {}

        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for art_idx, art in article_indices:
            text = self._cluster_text(art)
            base_threshold = self.threshold_cjk if _is_mostly_cjk(text) else self.threshold

            candidates = lsh.query_candidates(art_idx)
            if not candidates:
                continue

            art_entities = set(art.get('_entities', []))

            best_match_idx = None
            best_sim = 0.0

            for cand_idx in candidates:
                if cand_idx >= art_idx:
                    continue

                # 实体感知阈值：共享实体时降低阈值（同一实体的不同报道更可能是同一事件）
                threshold = base_threshold
                cand_event_id = idx_to_event_id.get(cand_idx)
                cand_art = idx_to_article.get(cand_idx)
                if cand_event_id and art_entities:
                    # 匹配已有 canonical event：检查 entity_tags
                    cand_event = existing_event_map.get(cand_event_id, {})
                    cand_entities = set(cand_event.get('entity_tags', []))
                    if art_entities & cand_entities:
                        threshold *= 0.65  # 共享实体时大幅降低阈值
                elif cand_art and art_entities:
                    # 匹配同批次文章：检查 _entities
                    cand_entities = set(cand_art.get('_entities', []))
                    if art_entities & cand_entities:
                        threshold *= 0.70

                # 聚类用组合相似度：TF-IDF + Jaccard + MinHash 三者取最大
                sim = matcher.similarity(art_idx, cand_idx)
                jac = matcher.jaccard(art_idx, cand_idx)
                sim = max(sim, jac)

                # MinHash Jaccard 估算也作为参考
                sig_a = lsh.get_signature(art_idx)
                sig_b = lsh.get_signature(cand_idx)
                if sig_a and sig_b:
                    mh_jac = minhash_jaccard(sig_a, sig_b)
                    sim = max(sim, mh_jac)

                if sim >= threshold and sim > best_sim:
                    best_sim = sim
                    best_match_idx = cand_idx

            if best_match_idx is None:
                continue

            # 匹配到已有 canonical event
            if best_match_idx in idx_to_event_id:
                matched_to_existing[art_idx] = idx_to_event_id[best_match_idx]
            elif best_match_idx in matched_to_existing:
                # 匹配到另一篇已匹配已有事件的文章
                matched_to_existing[art_idx] = matched_to_existing[best_match_idx]
            else:
                # 两篇新文章互相匹配 → union-find 聚类
                union(art_idx, best_match_idx)

        # ── 3. 整理聚类结果 ──

        # 3a. 匹配到已有事件的文章
        for art_idx, event_id in matched_to_existing.items():
            art = idx_to_article[art_idx]
            article_clusters.setdefault(event_id, []).append(art)

        # 3b. 新文章间的聚类
        uf_groups: Dict[int, List[int]] = {}
        for art_idx, art in article_indices:
            if art_idx in matched_to_existing:
                continue
            root = find(art_idx)
            uf_groups.setdefault(root, []).append(art_idx)

        for root, members in uf_groups.items():
            group_articles = [idx_to_article[m] for m in members]
            if len(group_articles) == 1:
                standalone.append(group_articles[0])
            else:
                new_cluster_groups.append(group_articles)

        # ── 4. 更新已有事件 + 添加证据 ──
        for event_id, new_articles in article_clusters.items():
            existing_ev = existing_event_map.get(event_id, {})
            existing_evidence = self.store.get_evidence_for_event(event_id)
            existing_sources = set(e.get('source_name', '') for e in existing_evidence)

            for art in new_articles:
                art_id = _url_hash(art.get('url', '') or art.get('link', ''))
                role = _determine_role(art, is_first=False,
                                       existing_sources=existing_sources)
                self.store.add_evidence(
                    event_id=event_id,
                    article_id=art_id,
                    role=role,
                    source_name=art.get('source_name', ''),
                    source_tier=art.get('source_tier', 2),
                    reported_at=art.get('collected_at', '') or
                                datetime.now(timezone.utc).isoformat(),
                    url=art.get('url', '') or art.get('link', ''),
                    title=art.get('title', ''),
                )
                existing_sources.add(art.get('source_name', ''))
                stats['evidence_added'] += 1

            # 更新 canonical event
            all_articles = new_articles
            # 如果新文章中有更好的 canonical 候选，更新
            best = _pick_canonical(all_articles)
            best_id = _url_hash(best.get('url', '') or best.get('link', ''))
            best_tier = best.get('source_tier', 2)
            existing_tier = 2
            if existing_ev.get('canonical_article_id'):
                # 查原有 canonical 的 tier
                orig = self.store.db.execute(
                    "SELECT source_tier FROM articles WHERE id = ?",
                    (existing_ev['canonical_article_id'],)
                ).fetchone()
                if orig:
                    existing_tier = orig[0]

            update_kwargs = {
                'cluster_size': (existing_ev.get('cluster_size', 1) +
                                 len(new_articles)),
            }

            # 如果新文章的 tier 更低（更权威），升级 canonical
            if best_tier < existing_tier:
                update_kwargs['canonical_url'] = (best.get('url', '') or
                                                  best.get('link', ''))
                update_kwargs['canonical_source'] = best.get('source_name', '')
                update_kwargs['canonical_article_id'] = best_id
                if isinstance(best.get('analysis'), dict):
                    if best['analysis'].get('importance', 0) > existing_ev.get('importance', 0):
                        update_kwargs['importance'] = best['analysis']['importance']
                    # 始终更新 analysis（切换 canonical 文章时）
                    update_kwargs['analysis'] = best['analysis']
            # 即使 tier 没变，也尝试用更丰富的 analysis 补充
            elif isinstance(best.get('analysis'), dict):
                existing_analysis = existing_ev.get('analysis', {}) or {}
                new_analysis = best['analysis']
                # 如果新分析有 detailed_content 而旧的没有，合并补充
                if (new_analysis.get('detailed_content', '').strip() and
                        not existing_analysis.get('detailed_content', '').strip()):
                    merged = dict(existing_analysis)
                    for k in ['detailed_content', 'background', 'deep_analysis',
                              'chinese_title', 'why_it_matters', 'key_details']:
                        if new_analysis.get(k) and not merged.get(k):
                            merged[k] = new_analysis[k]
                    update_kwargs['analysis'] = merged

            # 合并 entity_tags
            new_entities = set()
            for art in new_articles:
                for eid in art.get('_entities', []):
                    new_entities.add(eid)
            if new_entities:
                old_tags = existing_ev.get('entity_tags', [])
                merged = list(set(old_tags) | new_entities)
                if set(merged) != set(old_tags):
                    update_kwargs['entity_tags'] = merged

            self.store.update_canonical_event(event_id, **update_kwargs)

            # 自动升级状态
            self.store.auto_upgrade_status(event_id)
            stats['events_updated'] += 1

        # ── 5. 创建新事件 ──

        # 5a. 新聚类组 → 新事件
        for group in new_cluster_groups:
            event_id = _generate_event_id(group)
            canonical = _pick_canonical(group)
            canonical_id = _url_hash(canonical.get('url', '') or
                                     canonical.get('link', ''))

            # 收集 entity tags
            entity_tags = set()
            for art in group:
                for eid in art.get('_entities', []):
                    entity_tags.add(eid)

            # 计算重要性（取最高）
            importance = 0
            for art in group:
                if isinstance(art.get('analysis'), dict):
                    importance = max(importance,
                                     art['analysis'].get('importance', 0))

            pub_at = ''
            if canonical.get('published'):
                pub_at = (canonical['published'].isoformat()
                          if isinstance(canonical['published'], datetime)
                          else str(canonical['published']))

            status = _determine_initial_status(group)
            event_type = _determine_event_type(group)

            created = self.store.create_canonical_event(
                event_id=event_id,
                title=canonical.get('title', ''),
                summary=(canonical.get('analysis', {}).get('summary', '')
                         if isinstance(canonical.get('analysis'), dict) else ''),
                canonical_url=canonical.get('url', '') or canonical.get('link', ''),
                canonical_source=canonical.get('source_name', ''),
                canonical_article_id=canonical_id,
                entity_tags=list(entity_tags),
                event_type=event_type,
                status=status,
                importance=importance,
                published_at=pub_at,
            )

            if created:
                stats['events_created'] += 1

                # 按时间排序确定角色
                sorted_group = sorted(
                    group,
                    key=lambda a: a.get('collected_at', '') or '9999'
                )
                existing_sources: Set[str] = set()

                for i, art in enumerate(sorted_group):
                    art_id = _url_hash(art.get('url', '') or art.get('link', ''))
                    role = _determine_role(art, is_first=(i == 0),
                                           existing_sources=existing_sources)
                    self.store.add_evidence(
                        event_id=event_id,
                        article_id=art_id,
                        role=role,
                        source_name=art.get('source_name', ''),
                        source_tier=art.get('source_tier', 2),
                        reported_at=art.get('collected_at', '') or
                                    datetime.now(timezone.utc).isoformat(),
                        url=art.get('url', '') or art.get('link', ''),
                        title=art.get('title', ''),
                    )
                    existing_sources.add(art.get('source_name', ''))
                    stats['evidence_added'] += 1

                # 更新 cluster_size
                self.store.update_canonical_event(
                    event_id, cluster_size=len(group)
                )

                # 如果有 analysis，保存到 canonical event
                if isinstance(canonical.get('analysis'), dict):
                    self.store.update_canonical_event(
                        event_id, analysis=canonical['analysis']
                    )

        # 5b. 独立文章 → 单条事件
        for art in standalone:
            url = art.get('url', '') or art.get('link', '')
            if not url:
                continue

            art_id = _url_hash(url)
            event_id = 'evt_' + art_id[:24]

            # 收集 entity tags
            entity_tags = list(art.get('_entities', []))

            importance = 0
            if isinstance(art.get('analysis'), dict):
                importance = art['analysis'].get('importance', 0)

            pub_at = ''
            if art.get('published'):
                pub_at = (art['published'].isoformat()
                          if isinstance(art['published'], datetime)
                          else str(art['published']))

            status = _determine_initial_status([art])
            event_type = _determine_event_type([art])

            created = self.store.create_canonical_event(
                event_id=event_id,
                title=art.get('title', ''),
                summary=(art.get('analysis', {}).get('summary', '')
                         if isinstance(art.get('analysis'), dict) else ''),
                canonical_url=url,
                canonical_source=art.get('source_name', ''),
                canonical_article_id=art_id,
                entity_tags=entity_tags,
                event_type=event_type,
                status=status,
                importance=importance,
                published_at=pub_at,
            )

            if created:
                stats['events_created'] += 1

                role = _determine_role(art, is_first=True, existing_sources=set())
                self.store.add_evidence(
                    event_id=event_id,
                    article_id=art_id,
                    role=role,
                    source_name=art.get('source_name', ''),
                    source_tier=art.get('source_tier', 2),
                    reported_at=art.get('collected_at', '') or
                                datetime.now(timezone.utc).isoformat(),
                    url=url,
                    title=art.get('title', ''),
                )
                stats['evidence_added'] += 1

                if isinstance(art.get('analysis'), dict):
                    self.store.update_canonical_event(
                        event_id, analysis=art['analysis']
                    )

        self.store.commit()

        log.info("🔗 事件聚类完成: 新建 %d 个事件, 更新 %d 个事件, "
                 "添加 %d 条证据",
                 stats['events_created'], stats['events_updated'],
                 stats['evidence_added'])

        return stats

    def relink_unlinked(self, hours: int = 24) -> Dict[str, int]:
        """将尚未关联到 canonical event 的已分析文章进行聚类

        用于补链：如果有文章在 LLM 分析之后才具备聚类条件，
        可以单独调用此方法。
        """
        unlinked = self.store.get_unlinked_articles(hours=hours)
        if not unlinked:
            log.info("✅ 无需补链：所有文章均已关联到事件")
            return {'events_created': 0, 'events_updated': 0, 'evidence_added': 0}

        log.info("🔗 补链: %d 篇未关联文章", len(unlinked))
        return self.cluster_and_link(unlinked)
