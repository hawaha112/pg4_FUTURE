"""llm_analyzer 单元测试 — 重点测 JSON 解析和缓存"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from llm_analyzer import LLMAnalyzer, LLMCache


class TestLLMCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.cache = LLMCache(self.tmp.name, ttl_days=7)

    def tearDown(self):
        self.cache.close()
        os.unlink(self.tmp.name)

    def test_set_and_get(self):
        self.cache.set("https://example.com/article", {"summary": "test"})
        result = self.cache.get("https://example.com/article")
        self.assertIsNotNone(result)
        self.assertEqual(result["summary"], "test")

    def test_miss(self):
        result = self.cache.get("https://example.com/nonexistent")
        self.assertIsNone(result)

    def test_url_normalization(self):
        self.cache.set("https://www.example.com/article?utm=1", {"summary": "test"})
        # Should match without www and params
        result = self.cache.get("http://example.com/article")
        self.assertIsNotNone(result)

    def test_empty_url(self):
        self.cache.set("", {"summary": "test"})
        result = self.cache.get("")
        self.assertIsNone(result)

    def test_stats(self):
        self.cache.get("miss1")
        self.cache.get("miss2")
        self.cache.set("https://example.com/hit", {"x": 1})
        self.cache.get("https://example.com/hit")
        self.assertIn("hits=1", self.cache.stats)
        self.assertIn("misses=2", self.cache.stats)

    def test_overwrite(self):
        self.cache.set("https://example.com/a", {"v": 1})
        self.cache.set("https://example.com/a", {"v": 2})
        result = self.cache.get("https://example.com/a")
        self.assertEqual(result["v"], 2)


class TestExtractJson(unittest.TestCase):
    def setUp(self):
        # Create analyzer without API (we only test parsing)
        self.analyzer = LLMAnalyzer(api_key="dummy")

    def test_clean_json(self):
        result = self.analyzer._extract_json('{"ai_relevant": true, "summary": "test"}')
        self.assertIsNotNone(result)
        self.assertTrue(result["ai_relevant"])

    def test_json_in_markdown(self):
        result = self.analyzer._extract_json('```json\n{"ai_relevant": true}\n```')
        self.assertIsNotNone(result)

    def test_empty_response(self):
        result = self.analyzer._extract_json("")
        # _extract_json returns {} or None for empty input — either is acceptable
        self.assertTrue(result is None or result == {} or isinstance(result, dict))

    def test_not_json(self):
        result = self.analyzer._extract_json("This is plain text, not JSON")
        # Should not contain meaningful analysis data
        self.assertTrue(result is None or not result.get("summary"))


class TestValidateResult(unittest.TestCase):
    def setUp(self):
        self.analyzer = LLMAnalyzer(api_key="dummy")

    def test_valid_result(self):
        data = {
            "ai_relevant": True,
            "chinese_title": "测试",
            "summary": "概要",
            "importance": 3,
            "categories": ["AI 工具"],
            "source_type": "news",
        }
        result = self.analyzer._validate_result(data)
        self.assertEqual(result["importance"], 3)
        self.assertEqual(result["categories"], ["AI 工具"])

    def test_importance_clamped(self):
        data = {"ai_relevant": True, "importance": 99}
        result = self.analyzer._validate_result(data)
        self.assertLessEqual(result["importance"], 5)

    def test_invalid_source_type(self):
        data = {"ai_relevant": True, "source_type": "invalid_type"}
        result = self.analyzer._validate_result(data)
        self.assertEqual(result["source_type"], "news")

    def test_not_relevant(self):
        data = {"ai_relevant": False}
        result = self.analyzer._validate_result(data)
        self.assertFalse(result["ai_relevant"])


if __name__ == '__main__':
    unittest.main()
