"""最近修复的回归测试 — 防止已修过的 bug 再回来。

每条用例都对应一个具体的 commit。未来改这块代码时，看到这些用例
就能知道"这里曾经踩过什么坑"。
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from briefing_renderer import _already_rendered_in_shift
from llm_analyzer import LLMAnalyzer, _smart_truncate
from collector import _is_roundup_title, _drop_roundup_posts


# ════════════════════════════════════════════════════════════════════
# 跨 am/pm 班次去重 — commit 093475c
#
#   早先的 _event_in_window 会放行 evidence_chain 上有任一条目落在
#   班次窗口内的事件，这能把"昨天首发、今天跟进"的多源事件捞进当天
#   班次；但副作用是同一事件可能在早晚两班都出现。本函数是其后置
#   过滤器：本班次窗口起点之后已经渲染过的事件直接跳过；除非渲染
#   后又有新 evidence。
# ════════════════════════════════════════════════════════════════════


class TestAlreadyRenderedInShift(unittest.TestCase):
    def setUp(self):
        # 模拟早班窗口起点：今天本地时间 06:00
        today = datetime.now().astimezone().replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        self.window_start = today

    def _iso(self, dt):
        return dt.isoformat()

    def test_no_rendered_at_means_first_time(self):
        """事件从未渲染过 → 不去重。"""
        event = {'event_id': 'e1'}
        self.assertFalse(_already_rendered_in_shift(event, self.window_start))

    def test_rendered_before_window_can_re_render(self):
        """渲染发生在本班次窗口起点之前 → 允许在本班次出现。"""
        rendered = self.window_start - timedelta(hours=12)
        event = {'event_id': 'e1', 'rendered_at': self._iso(rendered)}
        self.assertFalse(_already_rendered_in_shift(event, self.window_start))

    def test_rendered_inside_window_dedups(self):
        """渲染发生在本班次窗口内、且没有更新 → 跳过。这是核心 bug 场景。"""
        rendered = self.window_start + timedelta(minutes=10)
        event = {'event_id': 'e1', 'rendered_at': self._iso(rendered)}
        self.assertTrue(_already_rendered_in_shift(event, self.window_start))

    def test_updated_after_render_reactivates(self):
        """渲染后又有新 evidence (last_updated_at > rendered_at) → 重新放行。"""
        rendered = self.window_start + timedelta(minutes=10)
        updated = rendered + timedelta(hours=2)
        event = {
            'event_id': 'e1',
            'rendered_at': self._iso(rendered),
            'last_updated_at': self._iso(updated),
        }
        self.assertFalse(_already_rendered_in_shift(event, self.window_start))

    def test_update_before_render_does_not_reactivate(self):
        """last_updated_at 早于 rendered_at → 不算激活，仍去重。"""
        rendered = self.window_start + timedelta(minutes=30)
        updated = rendered - timedelta(hours=1)
        event = {
            'event_id': 'e1',
            'rendered_at': self._iso(rendered),
            'last_updated_at': self._iso(updated),
        }
        self.assertTrue(_already_rendered_in_shift(event, self.window_start))

    def test_malformed_rendered_at_is_safe(self):
        """rendered_at 字段格式坏掉 → 当作"未渲染"处理（保守放行）。"""
        event = {'event_id': 'e1', 'rendered_at': 'not-a-date'}
        self.assertFalse(_already_rendered_in_shift(event, self.window_start))

    def test_z_suffix_iso_is_parsed(self):
        """rendered_at 用 Z 后缀（UTC）格式 → 应正确解析。"""
        # 本班次起点之前的 UTC 时间
        utc_before = (self.window_start - timedelta(hours=24)).astimezone(timezone.utc)
        rendered_z = utc_before.strftime('%Y-%m-%dT%H:%M:%SZ')
        event = {'event_id': 'e1', 'rendered_at': rendered_z}
        self.assertFalse(_already_rendered_in_shift(event, self.window_start))


# ════════════════════════════════════════════════════════════════════
# chinese_title 长度上限 60 — commit d5f3765
#
#   生产 events.db 里 8.3% 的卡片 chinese_title 恰好被截到 40 字
#   （"…30B 混合 MoE 多模态开源模" 这种半截词）。把上限从 40 提到
#   60 之后，中英混合技术标题（30-50 字）能完整出来。这条用例是
#   防回滚 — 任何把上限改回 40 或更小的改动都会撞到。
# ════════════════════════════════════════════════════════════════════


class TestChineseTitleCap(unittest.TestCase):
    def setUp(self):
        self.analyzer = LLMAnalyzer(api_key="dummy")

    def test_title_at_50_chars_kept(self):
        """50 字标题（典型中英混合）应完整保留。"""
        title = "英伟达发布 Nemotron 3 Nano Omni：30B 混合 MoE 多模态开源模型"
        self.assertGreaterEqual(len(title), 41)
        self.assertLessEqual(len(title), 60)
        data = {"ai_relevant": True, "chinese_title": title}
        result = self.analyzer._validate_result(data)
        self.assertEqual(result["chinese_title"], title)

    def test_title_over_60_chars_capped(self):
        """超过 60 字的标题截到 60，确保上限本身没被改回 40。"""
        title = "x" * 80
        data = {"ai_relevant": True, "chinese_title": title}
        result = self.analyzer._validate_result(data)
        self.assertEqual(len(result["chinese_title"]), 60)


# ════════════════════════════════════════════════════════════════════
# _smart_truncate 按句末截断 — commit d927763
#
#   早期 detailed_content 用硬截断 [:3000]，把读者扔在半句话里。
#   现在按句末（。！？\n. !）回退寻找最近的句末再截。
# ════════════════════════════════════════════════════════════════════


class TestSmartTruncate(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(_smart_truncate("短文本", 100), "短文本")

    def test_truncate_at_chinese_period(self):
        # 关键：句末 "。" 要落在 [search_limit, limit) 区间内才会被采用
        # 这里 limit=4，search_limit=max(3,2)=3，句末位于 index 3，刚好命中
        text = "短句一。后面还有很长很长很长很长很长。"
        out = _smart_truncate(text, 4, note="…")
        body = out.replace("…", "").strip()
        self.assertTrue(body.endswith("。"), f"未截在句末: {out!r}")
        self.assertIn("…", out)

    def test_no_sentence_end_falls_back_to_hard(self):
        """完全找不到句末时退化为硬截断（不应崩）。"""
        text = "x" * 200
        out = _smart_truncate(text, 50, note="…")
        # 至少 note 要追加上
        self.assertTrue(out.endswith("…"))


# ════════════════════════════════════════════════════════════════════
# 合订本/聚合帖过滤 — 2026-06-13
#
#   爱范儿 RSS 每天混一条「早报｜SpaceX上市/苹果Siri/华为盘古」式聚合帖：
#   一条 item 塞多条不相关新闻。采集器旧逻辑当成单一事件 → LLM 把里面的
#   华为盘古 + Kimi K2.7（+ Genspark 融资）硬揉进一张卡，用户看到"两件不
#   相关的事粘在一起"。修复：采集端用 _is_roundup_title 检出并丢弃。
#   正则收紧（栏目标签领头+分隔符 / AI·科技+早晚周报），避免误伤单事件文章。
# ════════════════════════════════════════════════════════════════════


class TestRoundupFilter(unittest.TestCase):
    # 触发本次修复的真实标题
    REAL_OFFENDER = (
        "早报｜SpaceX上市首日暴涨/苹果高管：Siri不想做用户的情感伴侣/"
        "华为余承东：要带盘古大模型从中国第一走向世界第一"
    )

    DROP = [
        REAL_OFFENDER,
        "晚报｜OpenAI发布新模型/谷歌回应Gemini争议",
        "AI早报：DeepSeek开源新版本",
        "科技早报 6月13日：苹果、华为齐发新品",
        "36氪：早报｜今日多家公司财报",
        "一周AI大事盘点",
        "大模型周报",
        "本周AI融资速览",
        "每日资讯汇总",
    ]
    KEEP = [
        # 聚合帖里被误揉的单事件，单独出现时必须保留
        "华为余承东：要带盘古大模型从中国第一走向世界第一",
        "Kimi K2.7 Code 正式开源，长程编程 token 消耗降三成",
        "OpenAI发布GPT-5",
        "Anthropic发布每日简报功能",        # "每日"+"简报"但非 roundup
        "苹果发布会要闻：Siri重大更新",      # 单事件 recap 含"要闻"
        "AI日历应用上线",                   # 含"日"非"日报"
        "OpenAI发布会日程公布",
        "DeepSeek-V3技术报告解读",
        "美国政府对Anthropic最新模型实施出口管制",
    ]

    def test_roundups_detected(self):
        for t in self.DROP:
            self.assertTrue(_is_roundup_title(t), f"漏判合订本: {t!r}")

    def test_single_events_kept(self):
        for t in self.KEEP:
            self.assertFalse(_is_roundup_title(t), f"误伤单事件: {t!r}")

    def test_drop_helper_removes_only_roundups(self):
        items = (
            [{'title': t} for t in self.DROP]
            + [{'title': t} for t in self.KEEP]
        )
        kept, dropped = _drop_roundup_posts(items)
        self.assertEqual(len(dropped), len(self.DROP))
        self.assertEqual(len(kept), len(self.KEEP))
        self.assertIn(self.REAL_OFFENDER[:80], dropped)

    def test_empty_and_missing_title_safe(self):
        self.assertFalse(_is_roundup_title(""))
        self.assertFalse(_is_roundup_title(None))
        kept, dropped = _drop_roundup_posts([{'summary': 'x'}, {'title': ''}])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])


if __name__ == '__main__':
    unittest.main()
