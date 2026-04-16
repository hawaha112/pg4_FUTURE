"""
source_ranker.py — 来源可靠性筛选与综合排序 v2

排序公式四维模型：
  - first_hand_score: 一手性（官方源/研究者原创 > 媒体解读 > 社区转发）
  - freshness_score:  时效性（12h 内强加权，连续衰减，不再用阶梯函数）
  - novelty_score:    新颖性（新条目 > 旧条目，旧事新炒惩罚）
  - confirmation_score: 交叉验证（多源报道加成，但不压过一手性）

设计原则：
  - 官方新发布在 12h 内拥有绝对优先级
  - 一手性 > 时效性 > 新颖性 > 重要性摘要评分
  - importance（LLM 给的 1-5 分）降为辅助权重，不再主导排序

用法:
    ranker = SourceRanker(config)
    items = ranker.score_and_filter(items)
    items = ranker.sort_by_relevance(items)
"""

import json
import math
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from logger import get_logger
log = get_logger('source_ranker')


# ---------------------------------------------------------------------------
# 默认来源信誉分（可被 config 覆盖）
# ---------------------------------------------------------------------------

DEFAULT_AUTHORITY = {
    "OpenAI Blog": 5, "Anthropic News": 5, "Google AI Blog": 5,
    "DeepMind Blog": 5, "Microsoft AI Blog": 5,
    "X-OpenAI": 5, "X-AnthropicAI": 5, "X-GoogleDeepMind": 5,
    "ArXiv AI": 4, "ArXiv ML": 4, "MIT Tech Review": 4,
    "Hugging Face Blog": 4,
    "X-Yann LeCun": 4, "X-Andrew Ng": 4, "X-Andrej Karpathy": 4, "X-Jim Fan": 4,
    "TechCrunch AI": 3, "The Verge AI": 3, "Ars Technica AI": 3,
    "VentureBeat AI": 3, "Wired AI": 3,
    "AI Explained": 3, "Two Minute Papers": 3, "Yannic Kilcher": 3,
    "X-Sam Altman": 3, "量子位": 3, "AI前线": 3, "知乎日报": 3,
    "Matthew Berman": 2, "Hacker News AI": 2,
    "36氪": 2, "36氪 AI搜索": 2, "少数派": 2, "虎嗅科技": 2,
    "掘金 AI": 2, "知乎热榜": 2, "X-Elon Musk (xAI)": 2,
    "小红书热门": 1, "机器之心": 4,
}

# 来源类型 → 基准信誉分
SOURCE_TYPE_BASE = {
    "official": 4, "paper": 4, "research": 4, "researcher": 4,
    "media": 3, "industry": 2, "community": 2, "video": 2, "tech": 2,
}

# Tier → 一手性基础分
_TIER_FIRST_HAND = {0: 1.0, 1: 0.7, 2: 0.3}

# source_type (LLM 判断) → 一手性加成
_SOURCE_TYPE_FIRST_HAND = {
    "official": 0.3,  # 官方公告
    "paper": 0.3,     # 原创论文
    "video": 0.1,     # 原创视频
    "opinion": 0.0,   # 观点评论
    "news": -0.1,     # 媒体转述
    "community": -0.1,
}


class SourceRanker:
    """来源可靠性评估与排序"""

    def __init__(self, config: dict):
        self.authority: Dict[str, int] = {**DEFAULT_AUTHORITY}
        config_authority = config.get('source_authority', {})
        for k, v in config_authority.items():
            if k.startswith('_'):
                continue
            self.authority[k] = v

        # 来源类别 + Tier 映射
        self.source_category: Dict[str, str] = {}
        self.source_tier: Dict[str, int] = {}
        for lang in ('english', 'chinese'):
            for src in config.get('sources', {}).get(lang, []):
                name = src.get('name', '')
                if name:
                    self.source_category[name] = src.get('category', '')
                    self.source_tier[name] = src.get('tier', 2)

        self._llm_cache: Dict[str, int] = {}

    def get_authority(self, source_name: str) -> int:
        if source_name in self.authority:
            return self.authority[source_name]
        if source_name in self._llm_cache:
            return self._llm_cache[source_name]
        cat = self.source_category.get(source_name, '')
        return SOURCE_TYPE_BASE.get(cat, 2)

    def get_tier(self, source_name: str) -> int:
        return self.source_tier.get(source_name, 2)

    def evaluate_unknown_source(self, source_name: str, sample_title: str,
                                llm_analyzer=None) -> int:
        if source_name in self.authority or source_name in self._llm_cache:
            return self.get_authority(source_name)
        if llm_analyzer is None:
            return self.get_authority(source_name)

        prompt = f"""请评估以下新闻来源的可信度（1-5分）：
来源名称：{source_name}
示例标题：{sample_title}

5=官方一手 4=权威研究/专业媒体 3=知名科技媒体 2=一般资讯/社区 1=不可靠
只返回一个数字（1-5）。"""

        try:
            response = llm_analyzer._call_api([
                {"role": "user", "content": prompt}
            ])
            score = int(re.search(r'[1-5]', response.strip()).group())
            self._llm_cache[source_name] = score
            log.info("🔍 来源评估 [%s]: %d/5", source_name, score)
            return score
        except Exception:
            return self.get_authority(source_name)

    def score_and_filter(self, items: List[dict],
                         min_authority: int = 0,
                         llm_analyzer=None) -> List[dict]:
        """给每篇文章添加来源信誉分，可选过滤低分来源"""
        result = []
        filtered = 0

        for item in items:
            source = item.get('source_name', '')
            if source not in self.authority and llm_analyzer:
                self.evaluate_unknown_source(
                    source, item.get('title', ''), llm_analyzer
                )

            authority = self.get_authority(source)
            item['_source_authority'] = authority

            if min_authority > 0 and authority < min_authority:
                filtered += 1
                continue
            result.append(item)

        if filtered > 0:
            log.info("🛡️ 来源筛选移除 %d 条低信誉来源", filtered)
        return result

    def enrich_cluster_info(self, items: List[dict]) -> List[dict]:
        """同事件多源报道标注"""
        for item in items:
            # 如果 _enrich_item_with_event_info 已经设置了，保留它
            if item.get('_also_reported_by'):
                continue
            cluster_sources = item.get('_cluster_sources', [])
            cluster_size = item.get('_cluster_size', 0)
            if cluster_size > 1 and cluster_sources:
                canonical_source = item.get('source_name', '')
                unique = list(set(s for s in cluster_sources if s and s != canonical_source))
                if unique:
                    item['_also_reported_by'] = unique
                    item['_report_count'] = cluster_size
        return items

    # ─── 四维排序公式 ────────────────────────────────────────

    def sort_by_relevance(self, items: List[dict]) -> List[dict]:
        """综合排序 v2：一手性 × 时效性 × 新颖性 × 交叉验证 + 重要性辅助

        权重分配（满分约 5.0）：
          first_hand   0-1.3  ×1.8  → 最高 2.34  (一手性主导)
          freshness    0-1.0  ×1.5  → 最高 1.50  (时效性次之)
          novelty      0-1.0  ×0.8  → 最高 0.80
          confirmation 0-1.0  ×0.5  → 最高 0.50
          importance   0-1.0  ×0.8  → 最高 0.80  (LLM 评分降为辅助)
          follow_up    -2.0         (旧事新炒强惩罚)
        """
        now = datetime.now(timezone.utc)

        def sort_score(item):
            analysis = item.get('analysis', {})
            source = item.get('source_name', '')
            tier = item.get('source_tier', self.get_tier(source))

            # ── 一手性 (0 ~ 1.3) ──
            # 基础分来自 tier
            first_hand = _TIER_FIRST_HAND.get(tier, 0.3)
            # LLM 判断的 source_type 做微调
            stype = analysis.get('source_type', 'news')
            first_hand += _SOURCE_TYPE_FIRST_HAND.get(stype, 0)
            first_hand = max(0, min(1.3, first_hand))

            # ── 时效性 (0 ~ 1.0) ──
            # 连续衰减：12h 内 = 1.0, 之后按指数衰减
            freshness = 0.3  # 无日期的默认值
            if item.get('published'):
                try:
                    pub = item['published']
                    if pub.tzinfo is None:
                        pub = pub.replace(tzinfo=timezone.utc)
                    age_hours = (now - pub).total_seconds() / 3600
                    if age_hours <= 0:
                        freshness = 1.0
                    elif age_hours <= 12:
                        freshness = 1.0
                    elif age_hours <= 24:
                        # 12h-24h: 线性衰减 1.0 → 0.6
                        freshness = 1.0 - 0.4 * ((age_hours - 12) / 12)
                    else:
                        # 24h+: 指数衰减，48h≈0.3, 72h≈0.15
                        freshness = 0.6 * math.exp(-0.03 * (age_hours - 24))
                        freshness = max(0.05, freshness)
                except Exception:
                    pass

            # Tier 0 + 12h 内 = 绝对优先（强加权叠加）
            tier0_fresh_boost = 0.0
            if tier == 0 and freshness >= 0.95:
                tier0_fresh_boost = 0.5

            # ── 新颖性 (0 ~ 1.0) ──
            novelty = 0.5
            if item.get('_is_new', False):
                novelty = 1.0
            if analysis.get('is_follow_up', False):
                novelty = 0.0  # follow_up 惩罚在后面单独加

            # ── 交叉验证 (0 ~ 1.0) ──
            report_count = item.get('_report_count', 0)
            if report_count >= 4:
                confirmation = 1.0
            elif report_count >= 3:
                confirmation = 0.7
            elif report_count >= 2:
                confirmation = 0.4
            else:
                confirmation = 0.0

            # ── 重要性辅助 (0 ~ 1.0) ──
            # LLM 给的 1-5 分归一化到 0-1
            importance_raw = analysis.get('importance', 1)
            importance = (importance_raw - 1) / 4.0  # 1→0, 5→1

            # ── 旧事新炒惩罚 ──
            follow_up_penalty = -2.0 if analysis.get('is_follow_up', False) else 0

            # ── 综合评分 ──
            score = (
                first_hand * 1.8 +
                freshness * 1.5 +
                novelty * 0.8 +
                confirmation * 0.5 +
                importance * 0.8 +
                tier0_fresh_boost +
                follow_up_penalty
            )

            return -score  # 降序

        items.sort(key=sort_score)
        return items
