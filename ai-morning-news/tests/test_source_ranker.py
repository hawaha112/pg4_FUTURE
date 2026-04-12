"""source_ranker 单元测试"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from source_ranker import SourceRanker


class TestSourceRanker(unittest.TestCase):
    def setUp(self):
        self.config = {
            'source_authority': {
                'OpenAI Blog': 5,
                'Hacker News AI': 2,
            }
        }
        self.ranker = SourceRanker(self.config)

    def test_authority_from_config(self):
        self.assertEqual(self.ranker.authority.get('OpenAI Blog'), 5)
        self.assertEqual(self.ranker.authority.get('Hacker News AI'), 2)

    def test_default_authority(self):
        # Sources not in config should use defaults
        self.assertIn('ArXiv AI', self.ranker.authority)

    def test_score_and_filter(self):
        items = [
            {'source_name': 'OpenAI Blog', 'analysis': {'importance': 4}},
            {'source_name': 'Hacker News AI', 'analysis': {'importance': 2}},
            {'source_name': 'Unknown Source', 'analysis': {'importance': 3}},
        ]
        result = self.ranker.score_and_filter(items, min_authority=0)
        self.assertEqual(len(result), 3)
        # All items should have authority score (field name: _source_authority)
        for item in result:
            self.assertIn('_source_authority', item)

    def test_sort_by_relevance(self):
        items = [
            {'source_name': 'Hacker News AI', 'analysis': {'importance': 2}, '_authority': 2},
            {'source_name': 'OpenAI Blog', 'analysis': {'importance': 5}, '_authority': 5},
        ]
        sorted_items = self.ranker.sort_by_relevance(items)
        # Higher importance+authority should come first
        self.assertEqual(sorted_items[0]['source_name'], 'OpenAI Blog')


if __name__ == '__main__':
    unittest.main()
