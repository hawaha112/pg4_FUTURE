"""
causal_engine.py — 因果分析知识库匹配引擎

加载 causal_kb/ 下的 JSON 知识库，将 LLM 分析结果与因果链匹配，
返回受影响的资产、传导机制、命中率等结构化信息。

用法:
    kb = CausalKB()
    matches = kb.match_article(analysis_dict)
    summary = kb.format_impact_summary(matches)
"""

import json
import os
from typing import Any, Dict, List, Optional

from logger import get_logger
log = get_logger('causal_engine')


# ---------------------------------------------------------------------------
# 知识库加载
# ---------------------------------------------------------------------------

def _load_json(path: str) -> dict:
    """加载 JSON 文件，失败时返回空 dict。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("[CausalKB] failed to load %s: %s", path, e)
        return {}


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class CausalKB:
    """因果分析知识库：加载、查询、匹配。"""

    def __init__(self, kb_dir: Optional[str] = None):
        if kb_dir is None:
            kb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "causal_kb")
        self.kb_dir = kb_dir

        self.event_taxonomy = _load_json(os.path.join(kb_dir, "event_taxonomy.json"))
        self.asset_taxonomy = _load_json(os.path.join(kb_dir, "asset_taxonomy.json"))
        self.chains_data = _load_json(os.path.join(kb_dir, "causal_chains.json"))
        self.params = _load_json(os.path.join(kb_dir, "transmission_params.json"))

        # 构建索引：event_type -> [chain]
        self._event_to_chains: Dict[str, List[dict]] = {}
        for chain in self.chains_data.get("chains", []):
            for evt in chain.get("trigger_events", []):
                self._event_to_chains.setdefault(evt, []).append(chain)

        # 构建事件类型全集（用于验证）
        self._valid_events = set()
        for cat_id, cat in self.event_taxonomy.get("taxonomy", {}).items():
            for sub_id in cat.get("subtypes", {}):
                self._valid_events.add(f"{cat_id}.{sub_id}")

        # 构建资产类型全集
        self._valid_assets = set()
        for cls_id, cls_data in self.asset_taxonomy.get("asset_classes", {}).items():
            sub_key = "sectors" if "sectors" in cls_data else "subtypes"
            for sub_id in cls_data.get(sub_key, {}):
                self._valid_assets.add(f"{cls_id}.{sub_id}")

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    @property
    def chain_count(self) -> int:
        return len(self.chains_data.get("chains", []))

    @property
    def event_type_count(self) -> int:
        return len(self._valid_events)

    @property
    def asset_type_count(self) -> int:
        return len(self._valid_assets)

    def match_article(self, analysis: dict) -> List[dict]:
        """
        将 LLM 分析结果与因果链匹配。

        analysis 应包含:
          - causal_events: List[str]  e.g. ["geopolitical.sanctions"]
          - affected_assets: List[str] e.g. ["equity.semiconductor"]（可选）
          - impact_direction: str ("positive"|"negative"|"mixed"|"neutral")（可选）
          - impact_confidence: str ("high"|"medium"|"low")（可选）

        返回匹配的因果链列表，每条包含:
          - chain_id, chain_name
          - matched_events: 匹配到的事件类型
          - impacts: 传导路径上的资产影响
          - empirical: 实证参数
          - llm_confirmed_assets: LLM 也识别到的资产（交叉验证）
        """
        causal_events = analysis.get("causal_events", [])
        if not causal_events:
            return []

        # 验证事件类型
        valid_events = [e for e in causal_events if e in self._valid_events]
        if not valid_events:
            return []

        llm_assets = set(analysis.get("affected_assets", []))
        llm_direction = analysis.get("impact_direction", "")
        llm_confidence = analysis.get("impact_confidence", "")

        # 匹配因果链
        seen_chains = set()
        matches = []
        for evt in valid_events:
            for chain in self._event_to_chains.get(evt, []):
                if chain["id"] in seen_chains:
                    continue
                seen_chains.add(chain["id"])

                # 找出本链中被当前事件触发的传导路径
                impacts = []
                chain_assets = set()
                for step in chain.get("transmission_path", []):
                    target = step.get("to", "")
                    chain_assets.add(target)
                    impacts.append({
                        "from": step.get("from", ""),
                        "to": target,
                        "direction": step.get("direction", "variable"),
                        "mechanism": step.get("mechanism_zh", step.get("mechanism", "")),
                        "lag_hours": step.get("lag_hours", []),
                        "conditional": step.get("conditional", ""),
                    })

                # 交叉验证：LLM 识别的资产与链上资产的交集
                confirmed = chain_assets & llm_assets

                empirical = chain.get("empirical", {})
                match_entry = {
                    "chain_id": chain["id"],
                    "chain_name": chain.get("name", ""),
                    "chain_number": chain.get("chain_number", 0),
                    "matched_events": [e for e in valid_events if e in chain.get("trigger_events", [])],
                    "impacts": impacts,
                    "empirical": {
                        "hit_rate": empirical.get("hit_rate", 0),
                        "avg_recovery_days": empirical.get("avg_recovery_days"),
                        "typical_magnitude_pct": empirical.get("typical_magnitude_pct"),
                        "key_finding": empirical.get("key_finding", ""),
                    },
                    "llm_confirmed_assets": sorted(confirmed),
                    "llm_direction": llm_direction,
                    "llm_confidence": llm_confidence,
                    "confidence_score": self._compute_confidence(
                        empirical.get("hit_rate", 0),
                        len(confirmed),
                        llm_confidence,
                    ),
                }
                matches.append(match_entry)

        # 按置信度降序排列
        matches.sort(key=lambda m: m["confidence_score"], reverse=True)
        return matches

    def get_chain(self, chain_id: str) -> Optional[dict]:
        """根据 ID 获取完整因果链信息。"""
        for chain in self.chains_data.get("chains", []):
            if chain["id"] == chain_id:
                return chain
        return None

    def get_chains_for_event(self, event_type: str) -> List[dict]:
        """获取某事件类型触发的所有因果链。"""
        return self._event_to_chains.get(event_type, [])

    def get_event_info(self, event_type: str) -> Optional[dict]:
        """获取事件类型的详细信息（标签、关键词）。"""
        parts = event_type.split(".", 1)
        if len(parts) != 2:
            return None
        category, subtype = parts
        cat_data = self.event_taxonomy.get("taxonomy", {}).get(category, {})
        return cat_data.get("subtypes", {}).get(subtype)

    def get_asset_info(self, asset_type: str) -> Optional[dict]:
        """获取资产类型的详细信息。"""
        parts = asset_type.split(".", 1)
        if len(parts) != 2:
            return None
        asset_class, sub = parts
        cls_data = self.asset_taxonomy.get("asset_classes", {}).get(asset_class, {})
        sub_key = "sectors" if "sectors" in cls_data else "subtypes"
        return cls_data.get(sub_key, {}).get(sub)

    def list_event_types(self) -> List[str]:
        """列出所有有效的事件类型。"""
        return sorted(self._valid_events)

    def list_asset_types(self) -> List[str]:
        """列出所有有效的资产类型。"""
        return sorted(self._valid_assets)

    def format_impact_summary(self, matches: List[dict], max_chains: int = 2) -> str:
        """
        将匹配结果格式化为中文摘要，用于文章卡片展示。

        示例输出:
          "因果链: 地缘政治→大宗商品→行业股票 (命中率65%)
           影响: 原油↑, 黄金↑, 航空股↓, 国防股↑ | 置信度: 0.72"
        """
        if not matches:
            return ""

        lines = []
        for m in matches[:max_chains]:
            # 链名和命中率
            hit_rate = m["empirical"].get("hit_rate", 0)
            header = f"[{m['chain_name']}] (历史命中率{hit_rate:.0%})"

            # 资产影响
            impact_parts = []
            for imp in m["impacts"]:
                target = imp["to"]
                # 尝试获取中文标签
                asset_info = self.get_asset_info(target)
                label = asset_info.get("label", target) if asset_info else target
                direction = imp["direction"]
                arrow = {"up": "↑", "down": "↓", "variable": "↕", "inverse": "↕"}.get(direction, "?")
                impact_parts.append(f"{label}{arrow}")

            impacts_str = ", ".join(impact_parts[:6])  # 最多显示6个

            # 置信度
            conf = m["confidence_score"]
            conf_label = "高" if conf >= 0.7 else "中" if conf >= 0.5 else "低"

            lines.append(f"{header}\n  影响: {impacts_str} | 综合置信度: {conf_label}")

            # 关键发现
            finding = m["empirical"].get("key_finding", "")
            if finding:
                lines.append(f"  参考: {finding[:60]}...")

        return "\n".join(lines)

    def format_impact_json(self, matches: List[dict], max_chains: int = 3) -> List[dict]:
        """
        将匹配结果格式化为精简 JSON，用于前端展示或 API 输出。
        """
        result = []
        for m in matches[:max_chains]:
            impacts = []
            for imp in m["impacts"]:
                asset_info = self.get_asset_info(imp["to"])
                label = asset_info.get("label", imp["to"]) if asset_info else imp["to"]
                impacts.append({
                    "asset": imp["to"],
                    "asset_label": label,
                    "direction": imp["direction"],
                    "mechanism": imp["mechanism"],
                    "lag_hours": imp["lag_hours"],
                })
            result.append({
                "chain_id": m["chain_id"],
                "chain_name": m["chain_name"],
                "hit_rate": m["empirical"].get("hit_rate", 0),
                "confidence_score": m["confidence_score"],
                "impacts": impacts,
                "matched_events": m["matched_events"],
                "llm_confirmed_assets": m["llm_confirmed_assets"],
            })
        return result

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        base_hit_rate: float,
        confirmed_count: int,
        llm_confidence: str,
    ) -> float:
        """
        综合置信度计算：
          - 基础命中率权重 50%
          - LLM 确认资产数量加成 25%（每个确认 +0.1，上限 0.25）
          - LLM 置信度加成 25%
        """
        # 基础
        score = base_hit_rate * 0.5

        # 资产交叉验证加成
        confirm_bonus = min(confirmed_count * 0.1, 0.25)
        score += confirm_bonus

        # LLM 置信度
        llm_map = {"high": 0.25, "medium": 0.15, "low": 0.05}
        score += llm_map.get(llm_confidence, 0.10)

        return round(min(score, 1.0), 3)


# ---------------------------------------------------------------------------
# CLI 测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    kb = CausalKB()
    print(f"CausalKB loaded: {kb.chain_count} chains, "
          f"{kb.event_type_count} event types, {kb.asset_type_count} asset types")
    print()

    # 测试用例 1：地缘政治事件
    test1 = {
        "causal_events": ["geopolitical.military_conflict"],
        "affected_assets": ["commodity.crude_oil", "commodity.gold", "equity.defense"],
        "impact_direction": "mixed",
        "impact_confidence": "high",
    }
    matches1 = kb.match_article(test1)
    print("=== Test 1: Military conflict ===")
    print(kb.format_impact_summary(matches1))
    print()

    # 测试用例 2：AI 模型发布
    test2 = {
        "causal_events": ["ai_industry.model_release"],
        "affected_assets": ["equity.ai_pure_play", "equity.semiconductor"],
        "impact_direction": "positive",
        "impact_confidence": "medium",
    }
    matches2 = kb.match_article(test2)
    print("=== Test 2: AI model release ===")
    print(kb.format_impact_summary(matches2))
    print()

    # 测试用例 3：关税升级
    test3 = {
        "causal_events": ["trade_policy.tariff_increase", "trade_policy.export_control"],
        "affected_assets": ["equity.semiconductor", "commodity.soybean"],
        "impact_direction": "negative",
        "impact_confidence": "high",
    }
    matches3 = kb.match_article(test3)
    print("=== Test 3: Tariff + export control ===")
    print(kb.format_impact_summary(matches3))
    print()

    # 测试用例 4：无因果事件的普通新闻
    test4 = {
        "causal_events": [],
        "affected_assets": [],
        "impact_direction": "neutral",
        "impact_confidence": "low",
    }
    matches4 = kb.match_article(test4)
    print("=== Test 4: No causal events (should be empty) ===")
    print(f"Matches: {len(matches4)}")
