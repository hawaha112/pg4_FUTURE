#!/usr/bin/env python3
"""
entity_coverage.py — 重要实体覆盖矩阵

围绕关键实体（OpenAI、Anthropic、Google DeepMind 等）建立监控：
- 检查每个实体的主源/备用源是否健康
- 在采集结果中检测实体覆盖率
- 对长时间未出现的关键实体发出告警
- 为采集器提供"保护名额"：确保实体相关条目不被截断

用法:
    matrix = EntityCoverageMatrix('entity_registry.json')
    matrix.check_source_health(source_health)     # 检查源可用性
    matrix.tag_items(items)                        # 给条目打实体标记
    matrix.check_coverage(store)                   # 检查事件库中的覆盖率
    alerts = matrix.get_alerts()                   # 获取告警
"""

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from logger import get_logger
log = get_logger('entity_coverage')


class EntityCoverageMatrix:
    """实体覆盖矩阵：确保关键主体不被遗漏"""

    def __init__(self, registry_path: str = None):
        if registry_path is None:
            registry_path = str(Path(__file__).parent / 'entity_registry.json')

        with open(registry_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        self.entities = data.get('entities', [])
        self._alerts: List[str] = []

        # 预编译每个实体的关键词正则
        self._entity_patterns: Dict[str, re.Pattern] = {}
        for entity in self.entities:
            kws = entity.get('keywords', [])
            if kws:
                # 构建 OR 正则，不区分大小写
                pattern_str = '|'.join(re.escape(kw) for kw in kws)
                self._entity_patterns[entity['id']] = re.compile(
                    pattern_str, re.IGNORECASE
                )

    # ─── 源健康检查 ──────────────────────────────────────────

    def check_source_health(self, source_health: dict):
        """检查每个实体的主源是否健康

        Args:
            source_health: source_health.json 的内容
        """
        for entity in self.entities:
            entity_name = entity['name']
            primary = entity.get('primary_sources', [])
            backup = entity.get('backup_sources', [])

            primary_ok = 0
            primary_down = []
            for src in primary:
                health = source_health.get(src, {})
                status = health.get('status', 'unknown')
                if status == 'ok':
                    primary_ok += 1
                elif status == 'failing':
                    primary_down.append(src)

            if primary and primary_ok == 0:
                # 所有主源失效 — 若备用源兜住则降级为 info（不告警）
                backup_ok = sum(
                    1 for src in backup
                    if source_health.get(src, {}).get('status') == 'ok'
                )
                if backup_ok >= 1:
                    log.info(
                        "ℹ️ [%s] 主源失效但备用源接管: %s → backup %d/%d OK",
                        entity_name, primary_down, backup_ok, len(backup)
                    )
                else:
                    msg = (
                        f"🚨 [{entity_name}] 所有主源 + 备用源均失效: {primary_down}. "
                        f"backup {backup_ok}/{len(backup)}"
                    )
                    self._alerts.append(msg)
                    log.warning(msg)
            elif primary_down:
                msg = f"⚠️ [{entity_name}] 部分主源失效: {primary_down}"
                self._alerts.append(msg)
                log.warning(msg)

    # ─── 条目实体标记 ──────────────────────────────────────────

    def tag_items(self, items: List[dict], require_title: bool = False) -> List[dict]:
        """给每条 item 打实体标记

        在 item 上添加 _entities 字段（匹配到的实体 ID 列表）
        和 _entity_protected 字段（是否受实体保护不被截断）

        require_title=True（实体时间线用）：只认"标题里出现的实体"（= 这条的主语），
        丢掉仅在正文顺带提及的实体 —— 否则"OpenAI 回应 Anthropic"会因正文含
        anthropic 而错挂进 Anthropic 时间线。采集/覆盖默认 False（行为不变，不影响聚类）。
        """
        for item in items:
            title_l = (item.get('title') or '').lower()
            full_l = (
                (item.get('title') or '') + ' ' +
                (item.get('summary') or '')[:500]
            ).lower()
            # 主语判定文本：严格模式只看标题，否则看 标题+摘要
            match_text = title_l if require_title else full_l
            source = item.get('source_name', '')

            matched_entities = []
            for entity in self.entities:
                eid = entity['id']
                # 来源匹配（官方源直挂，强信号）
                is_primary = source in entity.get('primary_sources', [])
                # 关键词匹配（严格模式仅标题）
                pattern = self._entity_patterns.get(eid)
                keyword_match = pattern.search(match_text) if pattern else False

                if is_primary or keyword_match:
                    matched_entities.append(eid)

            if matched_entities:
                item['_entities'] = matched_entities
                # 如果匹配到 Tier 0 实体，标记为受保护
                protected_entities = [
                    e for e in self.entities
                    if e['id'] in matched_entities and e.get('tier', 2) == 0
                ]
                item['_entity_protected'] = bool(protected_entities)

        return items

    # ─── 覆盖率检查 ──────────────────────────────────────────

    def check_coverage(self, store, hours: int = 24):
        """检查事件库中各实体的覆盖情况

        Args:
            store: EventStore 实例
            hours: 检查时间窗口
        """
        events = store.get_events_for_briefing(hours=hours)

        # 统计每个实体在事件库中出现的次数
        entity_counts: Dict[str, int] = {e['id']: 0 for e in self.entities}
        for event in events:
            text = (
                (event.get('title') or '') + ' ' +
                (event.get('summary') or '')[:500]
            ).lower()
            source = event.get('source_name', '')

            for entity in self.entities:
                eid = entity['id']
                is_primary = source in entity.get('primary_sources', [])
                pattern = self._entity_patterns.get(eid)
                keyword_match = pattern.search(text) if pattern else False
                if is_primary or keyword_match:
                    entity_counts[eid] += 1

        # 检查长时间未覆盖的实体
        for entity in self.entities:
            eid = entity['id']
            if entity_counts[eid] == 0:
                alert_hours = entity.get('alert_if_missing_hours', 168)
                if hours >= alert_hours:
                    msg = (
                        f"📭 [{entity['name']}] 过去 {hours} 小时无任何覆盖 "
                        f"（告警阈值: {alert_hours}h）"
                    )
                    self._alerts.append(msg)
                    log.warning(msg)

        return entity_counts

    # ─── 同实体刷屏限流 ──────────────────────────────────────

    def apply_entity_rate_limit(
        self,
        items: List[dict],
        max_per_entity: int = 5,
        protect_tier0: bool = True,
    ) -> List[dict]:
        """限制同一实体的条目数量，防止单一实体刷屏

        Tier 0 实体来自主源的条目受保护不受限流。
        同一实体超过 max_per_entity 条时，按时间保留最新的。

        Args:
            items: 已打过 _entities 标记的条目列表
            max_per_entity: 每个实体的最大条目数
            protect_tier0: 是否保护 Tier 0 实体主源的条目

        Returns:
            限流后的条目列表
        """
        entity_counts: Dict[str, int] = {}
        result = []
        throttled = 0

        for item in items:
            entities = item.get('_entities', [])
            protected = item.get('_entity_protected', False)

            if not entities:
                # 无实体标记的条目直接保留
                result.append(item)
                continue

            if protect_tier0 and protected:
                # Tier 0 实体主源条目受保护
                for eid in entities:
                    entity_counts[eid] = entity_counts.get(eid, 0) + 1
                result.append(item)
                continue

            # 检查是否有实体超过配额
            over_limit = False
            for eid in entities:
                if entity_counts.get(eid, 0) >= max_per_entity:
                    over_limit = True
                    break

            if over_limit:
                throttled += 1
                continue

            for eid in entities:
                entity_counts[eid] = entity_counts.get(eid, 0) + 1
            result.append(item)

        if throttled > 0:
            log.info("🚦 实体限流移除 %d 条刷屏内容", throttled)

        return result

    # ─── 报告 ────────────────────────────────────────────────

    def get_alerts(self) -> List[str]:
        return self._alerts

    def print_report(self, entity_counts: Dict[str, int] = None):
        """打印覆盖率报告"""
        if entity_counts:
            log.info("📊 实体覆盖率:")
            for entity in self.entities:
                eid = entity['id']
                count = entity_counts.get(eid, 0)
                status = "✅" if count > 0 else "❌"
                log.info("  %s %s: %d 条", status, entity['name'], count)

        if self._alerts:
            log.warning("🚨 实体覆盖告警 (%d 条):", len(self._alerts))
            for alert in self._alerts:
                log.warning("  %s", alert)
