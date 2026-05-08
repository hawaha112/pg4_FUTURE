"""html_generator 集成测试 — fixture items → generate_html → 产物断言

覆盖 briefing_renderer 下游最关键一环：模板占位符替换、安全转义、SEO meta 注入。
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from html_generator import generate_html


def _fixture_item(idx, importance=3, audience=None, cluster_size=1, image=''):
    return {
        'source_name': f'Source{idx}',
        'source_icon': '📰',
        'source_tier': 0 if idx == 0 else 2,
        'image': image,
        'title': f'Original title {idx} about GPT-{idx}',
        'link': f'https://example.com/a{idx}',
        'published': datetime(2026, 4, 17, 8, 0),
        '_cluster_size': cluster_size,
        '_report_count': cluster_size,
        'analysis': {
            'chinese_title': f'中文标题{idx}：AI 新进展',
            'summary': f'摘要{idx}',
            'why_it_matters': f'这条新闻很重要，原因{idx}',
            'importance': importance,
            'categories': ['大模型发布'],
            'source_type': 'news',
            'reading_minutes': 2,
            'audience': audience or ['general'],
            'key_details': [],
            'detailed_content': '',
        },
    }


class TestHtmlGeneratorIntegration(unittest.TestCase):

    def test_basic_render_returns_html_and_modal(self):
        items = [_fixture_item(i, importance=2) for i in range(3)]
        html, modal = generate_html(items, config={}, digest=None, meta=None)
        self.assertIsInstance(html, str)
        self.assertIsInstance(modal, str)
        self.assertIn('<!DOCTYPE html>', html)
        self.assertIn('const __data = [', modal)

    def test_featured_section_for_importance_ge_4(self):
        items = [_fixture_item(0, importance=5), _fixture_item(1, importance=2)]
        html, _ = generate_html(items, config={})
        self.assertIn('今日必读', html)
        self.assertIn('featured-card', html)

    def test_meta_description_from_digest(self):
        items = [_fixture_item(0)]
        digest = {'editorial': '今日共收录 1 条 AI 资讯，重点是 OpenAI 发布新模型。'}
        html, _ = generate_html(items, config={}, digest=digest)
        self.assertIn('<meta name="description"', html)
        self.assertIn('OpenAI 发布新模型', html)
        self.assertIn('<meta property="og:title"', html)
        self.assertIn('<meta name="twitter:card"', html)

    def test_meta_description_fallback_to_top3(self):
        items = [_fixture_item(0, importance=5, cluster_size=2)]
        html, _ = generate_html(items, config={}, digest=None)
        # top3 分支应注入包含"共 N 条"的 description
        self.assertIn('<meta name="description"', html)
        self.assertIn('重点', html)

    def test_escape_xss_in_titles(self):
        # 页面已简化：只渲染 featured（importance≥4），所以 fixture 要过这个门槛
        item = _fixture_item(0, importance=5)
        item['analysis']['chinese_title'] = '<script>alert(1)</script>'
        item['source_name'] = '"><img src=x>'
        html, _ = generate_html([item], config={})
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('"><img src=x>', html)

    def test_llm_coverage_banner_below_50pct(self):
        items = [_fixture_item(i) for i in range(10)]
        meta = {'llm_coverage': 0.3, 'llm_count': 3}
        html, _ = generate_html(items, config={}, meta=meta)
        self.assertIn('llm-banner', html)
        self.assertIn('30%', html)

    def test_no_banner_when_coverage_adequate(self):
        items = [_fixture_item(i) for i in range(10)]
        meta = {'llm_coverage': 0.8, 'llm_count': 8}
        html, _ = generate_html(items, config={}, meta=meta)
        # 中文 banner 文案只在实际渲染时出现（CSS 中不含）
        self.assertNotIn('LLM 深度分析覆盖率', html)

    def test_audience_metadata_on_featured_cards(self):
        # 页面已简化无过滤条，改为断言 audience 写入 featured-card 的 data-aud 属性
        items = [
            _fixture_item(0, importance=5, audience=['researcher']),
            _fixture_item(1, importance=5, audience=['developer']),
        ]
        html, _ = generate_html(items, config={})
        self.assertIn('data-aud="researcher"', html)
        self.assertIn('data-aud="developer"', html)

    def test_modal_json_contains_item_ids(self):
        items = [_fixture_item(0), _fixture_item(1)]
        _, modal = generate_html(items, config={})
        self.assertIn('"item_id":', modal)
        self.assertIn('https://example.com/a0', modal)


if __name__ == '__main__':
    unittest.main()
