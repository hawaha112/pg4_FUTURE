"""最近修复的回归测试 — 防止已修过的 bug 再回来。

每条用例都对应一个具体的 commit。未来改这块代码时，看到这些用例
就能知道"这里曾经踩过什么坑"。
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import tempfile
from pathlib import Path

from briefing_renderer import (
    _already_rendered_in_shift, _load_prev_briefing, _save_last_briefing,
)
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


# ════════════════════════════════════════════════════════════════════
# 跨班次"上一班记忆" — 2026-06-14
#
#   用户反馈"今天的早报和昨天的晚报都说的是一件事"。根因: 出报时 LLM 不知
#   道上一班讲了什么, 延续性大事(如美国对 Anthropic 出口管制)每班都从头重讲。
#   修复: last_briefing.json 存上一班判断/头条, 下一班读回注入连续性约束
#   (延续事件写"进展/跟进"、没新进展让位、开场不雷同)。
# ════════════════════════════════════════════════════════════════════


class TestContinuityBlock(unittest.TestCase):
    PC = {
        'label': '6月13日晚报',
        'judgments': ['Anthropic IPO与出口管制同日落地', '合规团队规模将成竞争壁垒'],
        'headlines': ['美国封禁Fable 5'],
    }

    def test_digest_block_has_prev_and_rule(self):
        b = LLMAnalyzer._build_continuity_block(self.PC)
        self.assertIn('6月13日晚报', b)
        self.assertIn('Anthropic IPO与出口管制同日落地', b)
        self.assertIn('进展/跟进', b)
        self.assertIn('没有实质新进展', b)

    def test_digest_block_empty_when_no_prev(self):
        self.assertEqual(LLMAnalyzer._build_continuity_block(None), '')
        self.assertEqual(LLMAnalyzer._build_continuity_block({}), '')
        self.assertEqual(
            LLMAnalyzer._build_continuity_block({'judgments': [], 'headlines': []}), '')

    def test_broadcast_note(self):
        n = LLMAnalyzer._broadcast_continuity_note(self.PC)
        self.assertIn('进展式', n)
        self.assertIn('6月13日晚报', n)
        self.assertEqual(LLMAnalyzer._broadcast_continuity_note(None), '')


class TestLastBriefingMemory(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.dir = Path(self._dir)
        self.today = datetime.now().astimezone().date()
        self.yesterday = self.today - timedelta(days=1)

    def test_roundtrip_prev_shift_loads(self):
        # 昨晚 pm 存 → 今早 am 读, 应取回
        _save_last_briefing(self.dir, 'pm', self.yesterday,
                            ['判断A', '判断B'], ['头条1', '头条2'])
        prev = _load_prev_briefing(self.dir, 'am', self.today)
        self.assertIsNotNone(prev)
        self.assertEqual(prev['judgments'], ['判断A', '判断B'])
        self.assertIn('晚报', prev['label'])

    def test_same_shift_same_day_is_self_rerun(self):
        # 同班次同一天 = 重跑自己, 不当上一班(防自我重复)
        _save_last_briefing(self.dir, 'am', self.today, ['判断A'], ['头条1'])
        self.assertIsNone(_load_prev_briefing(self.dir, 'am', self.today))

    def test_stale_memory_ignored(self):
        # ts 太旧(>22h)不用
        _save_last_briefing(self.dir, 'pm', self.yesterday, ['判断A'], ['头条1'])
        path = self.dir / 'last_briefing.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        old = datetime.now().astimezone() - timedelta(hours=30)
        data['ts'] = old.isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.assertIsNone(_load_prev_briefing(self.dir, 'am', self.today))

    def test_missing_file_safe(self):
        self.assertIsNone(_load_prev_briefing(self.dir, 'am', self.today))


# ════════════════════════════════════════════════════════════════════
# 多音字读音修正 — 2026-06-14
#
#   用户反馈口播多音字读错。Kokoro/misaki 定音 = jieba 分词 + pypinyin(TONE3)。
#   实测真错: 微调(→diào应tiáo)、重置(→zhòng应chóng)、切换(→qiè应qiē)、
#   长上下文(jieba 切碎"长"→默认 zhǎng 应 cháng)。修复: tts_pronounce 用
#   jieba.add_word + pypinyin.load_phrases_dict 注入读音, ZHG2P 调用前生效。
# ════════════════════════════════════════════════════════════════════


class TestPronunciationFix(unittest.TestCase):
    @staticmethod
    def _rd(text):
        import jieba
        from pypinyin import lazy_pinyin, Style
        return ' '.join(
            p for w in jieba.lcut(text)
            for p in lazy_pinyin(w, style=Style.TONE3, neutral_tone_with_five=True))

    def test_readings_corrected_after_apply(self):
        from tts_pronounce import apply_pronunciation_fixes
        apply_pronunciation_fixes()
        self.assertIn('tiao2', self._rd('微调模型'))   # 微调 = wēi tiáo
        self.assertIn('chong2', self._rd('模型重置'))  # 重置 = chóng zhì
        self.assertIn('qie1', self._rd('切换模型'))    # 切换 = qiē huàn
        self.assertTrue(self._rd('长上下文窗口').startswith('chang2'))  # 长 = cháng

    def test_idempotent(self):
        from tts_pronounce import apply_pronunciation_fixes
        apply_pronunciation_fixes()
        self.assertEqual(apply_pronunciation_fixes(), 0)  # 已加载, 第二次不重复

    def test_to_phrase_entry_format(self):
        from tts_pronounce import _to_phrase_entry
        self.assertEqual(_to_phrase_entry('wēi tiáo'), [['wēi'], ['tiáo']])
        self.assertIsNone(_to_phrase_entry(''))
        self.assertIsNone(_to_phrase_entry('   '))


# ════════════════════════════════════════════════════════════════════
# 配图清洗 + 占位图 — 2026-06-14
#
#   用户反馈"有些卡片图一点都不好" + "没有的能否补图"。源 og:image 常是通用
#   栏目封面/logo。修复: image_utils 剔烂图(主图也过噪声 + 同图被≥3条共用判通用
#   封面), 能替补就用 extra_images; 无图卡片渲染设计感占位图(域配色+图标+关键词)。
#   (AI 出图因免 key 服务转付费暂不可靠, 先做这条 100% 可靠的。)
# ════════════════════════════════════════════════════════════════════


class TestImageCleanup(unittest.TestCase):
    def test_is_bad_image(self):
        from image_utils import is_bad_image
        for u in ['', 'https://s3.ifanr.com/x/dao_li_cover.jpg', 'https://a.com/logo.png',
                  'data:image/png;base64,zzz', 'https://a.com/favicon.ico',
                  'https://a.com/icons/share.svg']:
            self.assertTrue(is_bad_image(u), f"应判烂图: {u!r}")
        for u in ['https://a.com/news/photo-123.jpg', 'https://cdn.b.com/2026/hero.jpeg']:
            self.assertFalse(is_bad_image(u), f"误伤好图: {u!r}")

    def test_generic_cover_dropped_and_replaced(self):
        from image_utils import clean_card_images
        items = [
            {'image': 'https://s/generic.jpg', 'extra_images': ['https://s/real-a.jpg']},
            {'image': 'https://s/generic.jpg', 'extra_images': []},
            {'image': 'https://s/generic.jpg', 'extra_images': []},  # 3 条共用 = 通用封面
            {'image': 'https://s/logo.png', 'extra_images': ['https://s/real-b.jpg']},
            {'image': 'https://s/unique-good.jpg', 'extra_images': []},
        ]
        emptied = clean_card_images(items)
        self.assertEqual(items[0]['image'], 'https://s/real-a.jpg')  # 有替补→换
        self.assertEqual(items[1]['image'], '')                     # 无替补→空
        self.assertEqual(items[3]['image'], 'https://s/real-b.jpg')  # logo→换
        self.assertEqual(items[4]['image'], 'https://s/unique-good.jpg')  # 独有好图→留
        self.assertEqual(emptied, 2)

    def test_hotlink_blocked_images_are_bad(self):
        from image_utils import is_bad_image
        # 微信图床/代理: 浏览器里 403 防盗链 → 判烂图, 走占位图
        for u in ['https://mmbiz.qpic.cn/mmbiz_jpg/abc/640',
                  'https://wechat2rss.xlab.app/img-proxy/?k=x&u=https%3A%2F%2Fmmbiz.qpic.cn%2Fa',
                  'http://img2.jintiankansha.me/get?src=http://mmbiz.qpic.cn/b']:
            self.assertTrue(is_bad_image(u), f"防盗链图应判烂: {u!r}")

    def test_placeholder_base_html(self):
        from html_generator import _card_img_html
        it = {'analysis': {'topic_domain': '治理与安全', 'categories': ['政策·监管·法律']}}
        ph = _card_img_html(it, 'card-img')
        self.assertIn('ph-gov', ph)
        self.assertIn('📜', ph)
        self.assertIn('card-img-ph', ph)
        self.assertNotIn('cimg-real', ph)   # 无图 → 不叠真图层

    def test_real_image_overlay_with_onerror(self):
        from html_generator import _card_img_html
        it = {'image': 'https://cdn.x.com/news/hero.jpg',
              'analysis': {'topic_domain': '模型与算法'}}
        html = _card_img_html(it, 'featured-img')
        self.assertIn('card-img-ph', html)          # 占位图仍打底
        self.assertIn('class="cimg-real"', html)    # 叠加真图
        self.assertIn('hero.jpg', html)
        self.assertIn('onerror="this.remove()"', html)  # 失败自动露占位图


# ════════════════════════════════════════════════════════════════════
# "与新闻不符"判断不发布 — 2026-06-14
#
#   用户质疑判断卡顶部「⚠ N 处与新闻不符」标签。结论: 在旗舰判断上贴自家核查的
#   矛盾警告是自我拆台。改为上游直接不发布 contradicted_count>0 的判断(宁缺毋滥),
#   读者只看干净判断, contradicted 仅写内部日志。本测试钉住"被剔除、不上页"。
# ════════════════════════════════════════════════════════════════════


class TestContradictedJudgmentDropped(unittest.TestCase):
    def test_contradicted_judgment_not_published(self):
        import json as _json
        calls = {'n': 0}

        def fake_call(_self, messages, **kw):
            calls['n'] += 1
            n = calls['n']
            if n == 1:   # Stage A 主编: 出 2 条判断
                return _json.dumps({"headline": "今日主旋律", "judgments": [
                    {"emoji": "🏢", "title": "判断一标题够长能过校验",
                     "body": "足够长的判断正文内容用来通过二十字以上的清洗阈值确保不被丢弃。",
                     "evidence_ids": [0]},
                    {"emoji": "📈", "title": "判断二标题也够长能过",
                     "body": "另一条足够长的判断正文同样用于通过校验阈值不被清洗掉处理。",
                     "evidence_ids": [1]},
                ], "outro": ""}, ensure_ascii=False)
            if n == 2:   # Stage B 编辑: 空 patch → 回退 draft
                return "{}"
            # n==3 Stage C 校对: 判断二被判定与新闻不符
            return _json.dumps({"fact_checks": [
                {"idx": 0, "verified_count": 1, "unverified_count": 0,
                 "contradicted_count": 0, "confidence": "high", "warnings": [], "claims_found": []},
                {"idx": 1, "verified_count": 0, "unverified_count": 0,
                 "contradicted_count": 1, "confidence": "low",
                 "warnings": ["数字与事件不符"], "claims_found": []},
            ]}, ensure_ascii=False)

        orig = LLMAnalyzer._call_api
        LLMAnalyzer._call_api = fake_call
        try:
            a = LLMAnalyzer.__new__(LLMAnalyzer)
            a.model = 'x'
            analyses = [
                {"analysis": {"ai_relevant": True, "summary": "事件一", "importance": 5},
                 "source_name": "S", "title": "t1"},
                {"analysis": {"ai_relevant": True, "summary": "事件二", "importance": 4},
                 "source_name": "S", "title": "t2"},
                {"analysis": {"ai_relevant": True, "summary": "事件三", "importance": 3},
                 "source_name": "S", "title": "t3"},
            ]
            d = a.generate_digest(analyses)
        finally:
            LLMAnalyzer._call_api = orig

        titles = [j['title'] for j in d['judgments']]
        self.assertEqual(len(titles), 1, f"应只剩 1 条(剔除与新闻不符的): {titles}")
        self.assertIn("判断一", titles[0])
        # 被剔除的判断不应出现在任何输出(含兜底 editorial)
        self.assertNotIn("判断二", d.get('editorial', ''))


# ════════════════════════════════════════════════════════════════════
# 跨天故事线去重 — 2026-06-14
#
#   用户反馈"出口管制/Fable5下线"那条连着好几班当头条。根因: 聚类只合并近乎同文的
#   报道, 跨天/跨语言/换措辞的同一故事线聚不到一起 → 每篇各成新事件、每班各上一次。
#   修复: suppress_recurring_storylines 对照"最近已渲染标题", LLM 丢掉无新进展的旧线重复。
# ════════════════════════════════════════════════════════════════════


class TestStorylineSuppression(unittest.TestCase):
    RECENT = ['美国出口管制直指AI模型, Anthropic下线Fable 5和Mythos 5',
              'Fable 5被禁, Anthropic开始退钱']

    @staticmethod
    def _items():
        def it(t):
            return {'analysis': {'chinese_title': t, 'summary': t + '的摘要', 'importance': 4}}
        return [it('出口管制后续: Fable 5仍未恢复访问'),   # 0 旧线重复
                it('美国封禁Fable 5引欧洲监管讨论'),        # 1 旧线重复
                it('OpenAI发布GPT-5.6新模型'),             # 2 新事
                it('英伟达发布新一代GPU')]                  # 3 新事

    def _run_with(self, drop_ret):
        items = self._items()
        orig = LLMAnalyzer._call_api
        LLMAnalyzer._call_api = lambda _self, msgs, **k: '{"drop": %s}' % drop_ret
        try:
            a = LLMAnalyzer.__new__(LLMAnalyzer); a.model = 'x'
            return a.suppress_recurring_storylines(items, self.RECENT)
        finally:
            LLMAnalyzer._call_api = orig

    def test_drops_recurring_keeps_new(self):
        kept = self._run_with('[0, 1]')
        titles = [i['analysis']['chinese_title'] for i in kept]
        self.assertEqual(len(kept), 2)
        self.assertTrue(all('GPT-5.6' in t or 'GPU' in t for t in titles))

    def test_safety_valve_no_overdrop(self):
        # 丢 3/4 过多 → 跳过, 原样返回
        kept = self._run_with('[0, 1, 2]')
        self.assertEqual(len(kept), 4)

    def test_off_switch(self):
        items = self._items()
        os.environ['STORYLINE_DEDUP'] = 'off'
        try:
            a = LLMAnalyzer.__new__(LLMAnalyzer); a.model = 'x'
            self.assertEqual(len(a.suppress_recurring_storylines(items, self.RECENT)), 4)
        finally:
            os.environ.pop('STORYLINE_DEDUP', None)

    def test_empty_recent_no_llm_call(self):
        items = self._items()
        called = {'n': 0}
        orig = LLMAnalyzer._call_api
        LLMAnalyzer._call_api = lambda _self, msgs, **k: called.__setitem__('n', called['n'] + 1) or '{}'
        try:
            a = LLMAnalyzer.__new__(LLMAnalyzer); a.model = 'x'
            kept = a.suppress_recurring_storylines(items, [])
            self.assertEqual(len(kept), 4)
            self.assertEqual(called['n'], 0)   # 无近期标题 → 不调 LLM
        finally:
            LLMAnalyzer._call_api = orig


# ════════════════════════════════════════════════════════════════════
# 早晚报分工 + 看点预告 — 2026-06-14
#   早报=信息准备(不发判断, 加「今日议程预告」前瞻); 晚报=复盘收束(判断=定论 + 「明日预告」)。
#   前瞻只从素材真实信号提炼, 不编日程。更多资讯默认折叠、早全晚精。
# ════════════════════════════════════════════════════════════════════


class TestLookahead(unittest.TestCase):
    def test_parse_and_cap(self):
        orig = LLMAnalyzer._call_api
        LLMAnalyzer._call_api = lambda s, m, **k: (
            '{"lookahead":[{"point":"看点A","because":"依据A"},{"point":"看点B","because":""}]}')
        try:
            a = LLMAnalyzer.__new__(LLMAnalyzer); a.model = 'x'
            out = a.generate_lookahead(
                [{'analysis': {'chinese_title': '某新闻', 'summary': '计划下周发布X'}}], mode='today')
        finally:
            LLMAnalyzer._call_api = orig
        self.assertEqual([x['point'] for x in out], ['看点A', '看点B'])

    def test_empty_items_no_llm(self):
        a = LLMAnalyzer.__new__(LLMAnalyzer); a.model = 'x'
        self.assertEqual(a.generate_lookahead([], mode='today'), [])


class TestAmPmEditions(unittest.TestCase):
    def _render(self, shift, mode):
        from html_generator import generate_html
        cfg = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'config.json'),
                             encoding='utf-8'))
        items = [{'title': 't%d' % i, 'source_name': 'S', 'link': '#', 'image': '',
                  'extra_images': [], 'analysis': {'ai_relevant': True, 'chinese_title': '标题%d' % i,
                  'summary': '摘要够长用于渲染展示内容。', 'importance': 4, 'categories': ['x'],
                  'why_it_matters': 'w', 'detailed_content': 'c' * 40, 'reading_minutes': 2,
                  'topic_domain': '模型与算法'}} for i in range(4)]
        digest = {'headline': 'h', 'judgments': [{'emoji': '🎯', 'title': '判断标题够长能过',
                  'body': '判断正文足够长用于展示内容。', 'evidence_ids': [0],
                  'fact_check': {'confidence': 'high', 'contradicted_count': 0}}],
                  'editorial': 'e', 'outro': ''}
        meta = {'broadcast_script': 'x', 'broadcast_audio': 'audio/x.mp3', 'llm_coverage': 0.8,
                'entity_timelines': [], 'total': 4, 'sources_count': 9,
                'lookahead': {'mode': mode, 'items': [{'point': 'p', 'because': 'b'}]}}
        old = os.environ.get('BRIEFING_SHIFT')
        os.environ['BRIEFING_SHIFT'] = shift
        try:
            h, _ = generate_html(items, cfg, digest, meta=meta)
        finally:
            if old is None:
                os.environ.pop('BRIEFING_SHIFT', None)
            else:
                os.environ['BRIEFING_SHIFT'] = old
        return h

    def test_am_no_judgment_has_agenda(self):
        h = self._render('am', 'today')
        self.assertNotIn('<section class="briefing">', h)        # 早报不发判断
        self.assertIn('la-title">📅 今日议程预告', h)             # 有今日议程预告
        self.assertIn('more-collapse', h)                        # 更多折叠

    def test_pm_has_judgment_and_tomorrow(self):
        h = self._render('pm', 'tomorrow')
        self.assertIn('<section class="briefing">', h)           # 晚报有判断
        self.assertIn('la-title">🔭 明日预告', h)                # 有明日预告
        self.assertNotIn('la-title">📅 今日议程预告', h)          # 晚报无今日议程


# (已移除 TestGlancePZXYYMapping —— 2026-06-27 速览改回 6 域统一, _pzxyy_of 已删。)


if __name__ == '__main__':
    unittest.main()
