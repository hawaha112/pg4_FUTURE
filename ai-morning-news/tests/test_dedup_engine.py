"""dedup_engine 单元测试"""

import os
import sys
import tempfile
import unittest

# 确保能导入上级目录的模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dedup_engine import tokenize, text_hash, url_hash, TFIDFMatcher, MinHashLSH


class TestTokenize(unittest.TestCase):
    def test_english(self):
        tokens = tokenize("Hello World")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)

    def test_chinese(self):
        tokens = tokenize("人工智能")
        # 逐字 + bigram
        self.assertIn("人", tokens)
        self.assertIn("工", tokens)
        self.assertIn("人工", tokens)
        self.assertIn("智能", tokens)

    def test_mixed(self):
        tokens = tokenize("AI 人工智能 2024")
        self.assertIn("ai", tokens)
        self.assertIn("人工", tokens)
        self.assertIn("2024", tokens)

    def test_empty(self):
        self.assertEqual(tokenize(""), [])

    def test_single_char_filtered(self):
        tokens = tokenize("a b c")
        # 单字母应该被过滤
        self.assertEqual(tokens, [])


class TestHashing(unittest.TestCase):
    def test_text_hash_consistent(self):
        h1 = text_hash("Hello World")
        h2 = text_hash("Hello World")
        self.assertEqual(h1, h2)

    def test_text_hash_ignores_whitespace(self):
        h1 = text_hash("Hello World")
        h2 = text_hash("Hello  World")
        self.assertEqual(h1, h2)

    def test_text_hash_case_insensitive(self):
        h1 = text_hash("Hello")
        h2 = text_hash("hello")
        self.assertEqual(h1, h2)

    def test_url_hash_strips_protocol(self):
        h1 = url_hash("https://example.com/page")
        h2 = url_hash("http://example.com/page")
        self.assertEqual(h1, h2)

    def test_url_hash_strips_www(self):
        h1 = url_hash("https://www.example.com/page")
        h2 = url_hash("https://example.com/page")
        self.assertEqual(h1, h2)

    def test_url_hash_strips_params(self):
        h1 = url_hash("https://example.com/page?utm_source=test")
        h2 = url_hash("https://example.com/page")
        self.assertEqual(h1, h2)

    def test_url_hash_empty(self):
        self.assertEqual(url_hash(""), "")


class TestTFIDFMatcher(unittest.TestCase):
    def test_identical_texts(self):
        m = TFIDFMatcher()
        m.add("OpenAI releases GPT-5")
        m.add("OpenAI releases GPT-5")
        sim = m.similarity(0, 1)
        self.assertAlmostEqual(sim, 1.0, places=2)

    def test_different_texts(self):
        m = TFIDFMatcher()
        m.add("OpenAI releases GPT-5")
        m.add("Apple launches new iPhone")
        sim = m.similarity(0, 1)
        self.assertLess(sim, 0.3)

    def test_similar_texts(self):
        m = TFIDFMatcher()
        m.add("OpenAI announces GPT-5 with reasoning")
        m.add("GPT-5 announced by OpenAI for reasoning tasks")
        sim = m.similarity(0, 1)
        self.assertGreater(sim, 0.4)


class TestMinHashLSH(unittest.TestCase):
    def test_add_and_query(self):
        lsh = MinHashLSH()
        tokens1 = tokenize("OpenAI releases GPT-5 model")
        tokens2 = tokenize("OpenAI releases GPT-5 model today")
        tokens3 = tokenize("Apple launches new iPhone 16")
        lsh.add(0, tokens1)
        lsh.add(1, tokens2)
        lsh.add(2, tokens3)

        # Query candidates for idx=1 should include idx=0 (similar)
        candidates = lsh.query_candidates(1)
        self.assertIn(0, candidates)

    def test_signature_caching(self):
        lsh = MinHashLSH()
        tokens = tokenize("test document")
        lsh.add(0, tokens)
        sig = lsh.get_signature(0)
        self.assertIsNotNone(sig)
        self.assertGreater(len(sig), 0)


if __name__ == '__main__':
    unittest.main()
