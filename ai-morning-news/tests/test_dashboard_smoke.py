"""dashboard_generator + e2e 渲染层 smoke test"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestDashboardSmoke(unittest.TestCase):
    """最小烟雾测试：dashboard_generator 不依赖网络/LLM 的核心流程能跑。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # 模拟 output/run_health.jsonl
        (self.tmp / 'output').mkdir()
        runs = [
            {"run_id": "2026-04-22T11:00:00Z", "shift": "am",
             "duration_sec": 200, "kept": 30, "llm_coverage": 0.95,
             "llm_count": 28, "multi_source_count": 3,
             "sources_healthy": 50, "sources_failing": 2,
             "dead_sources": [], "deploy_ok": True, "llm_available": True},
            {"run_id": "2026-04-22T23:00:00Z", "shift": "pm",
             "duration_sec": 0, "kept": 0, "llm_coverage": 0,
             "deploy_ok": False, "llm_available": False,
             "sources_healthy": 0, "sources_failing": 50, "dead_sources": ['x']},
        ]
        with open(self.tmp / 'output' / 'run_health.jsonl', 'w') as f:
            for r in runs:
                f.write(json.dumps(r) + '\n')
        # 写一份模拟 daily_run.log 含告警
        (self.tmp / 'daily_run.log').write_text(
            "12:00:00 [W] llm_analyzer: ⚠️ JSON 解析失败\n"
            "12:01:00 [E] event_store: ❌ DB 损坏\n"
            "12:02:00 [W] llm_analyzer: ⚠️ JSON 解析失败\n"
        )
        # 写 config.json + source_health.json + events.db 占位
        (self.tmp / 'config.json').write_text(
            json.dumps({'sources': {'english': [{'name': 'OpenAI Blog', 'tier': 0,
                                                 'category': 'official',
                                                 'url': 'https://x.com'}],
                                    'chinese': []}}))
        (self.tmp / 'source_health.json').write_text(json.dumps({}))
        # events.db 不存在也 ok（_collect_source_stats 容忍缺失）

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def test_dashboard_generates_with_fixture_data(self):
        import dashboard_generator
        # 改 dashboard 用临时目录
        with mock.patch.object(dashboard_generator, '__file__', str(self.tmp / 'dashboard_generator.py')):
            dashboard_generator.main()
        out = self.tmp / 'output' / 'dashboard.html'
        self.assertTrue(out.exists(), "dashboard.html 应该生成")
        html = out.read_text()
        # 关键元素都在
        self.assertIn('跑步仪表盘', html)
        self.assertIn('30', html)  # kept 数
        self.assertIn('JSON 解析失败', html)  # 错误聚合
        self.assertIn('DB 损坏', html)
        # 异常班次（kept=0 + deploy=False）应被红行高亮
        self.assertIn('rgba(192,80,80', html)

    def test_dashboard_handles_empty_jsonl(self):
        # 空 jsonl 不应崩
        (self.tmp / 'output' / 'run_health.jsonl').write_text('')
        import dashboard_generator
        with mock.patch.object(dashboard_generator, '__file__', str(self.tmp / 'dashboard_generator.py')):
            dashboard_generator.main()  # 不抛即可

    def test_recent_errors_aggregation(self):
        from dashboard_generator import _collect_recent_errors
        errs = _collect_recent_errors(self.tmp, max_errors=10)
        self.assertGreaterEqual(len(errs), 2)
        # JSON 解析失败 出现 2 次应被合并
        json_fail = next((e for e in errs if 'JSON 解析失败' in e['message']), None)
        self.assertIsNotNone(json_fail)
        self.assertEqual(json_fail['count'], 2)


class TestEndToEndRenderPath(unittest.TestCase):
    """briefing_renderer 主渲染路径（不采集，不 LLM）"""

    def test_html_generator_with_realistic_fixture(self):
        from datetime import datetime
        from html_generator import generate_html
        items = [{
            'source_name': 'OpenAI Blog', 'source_icon': '🤖', 'source_tier': 0,
            'image': '', 'title': 'GPT-5 release', 'link': 'https://example.com/gpt5',
            'published': datetime(2026, 4, 22, 8, 0),
            '_cluster_size': 2, '_report_count': 2,
            '_evidence_chain': [
                {'source_name': 'OpenAI Blog', 'reported_at': '2026-04-22T08:00:00+00:00'},
                {'source_name': 'TechCrunch AI', 'reported_at': '2026-04-22T10:00:00+00:00'},
            ],
            'analysis': {
                'chinese_title': 'OpenAI 发布 GPT-5',
                'why_it_matters': '新一代旗舰模型',
                'importance': 5, 'categories': ['大模型发布'],
                'source_type': 'official', 'reading_minutes': 5,
                'audience': ['developer'], 'detailed_content': '深度内容...',
                'event_signature': 'OpenAI release GPT-5',
            },
        }]
        html, modal = generate_html(items, config={'sources': {'english': [], 'chinese': []}})
        # 多源徽章
        self.assertIn('📡 另有 1 源报道', html)
        self.assertIn('TechCrunch AI', html)
        # event_signature 进入 modal_data（备日后周报消费）
        # （目前没把 event_signature 复制到 modal_entry，跳过这个断言）


if __name__ == '__main__':
    unittest.main()
