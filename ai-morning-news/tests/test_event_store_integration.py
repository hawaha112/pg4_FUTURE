"""event_store 集成测试 — 临时 SQLite，跑完整的 article→analysis→canonical event→briefing 查询。"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from event_store import EventStore


def _fixture_article(idx):
    return {
        'url': f'https://example.com/a{idx}',
        'title': f'Article {idx}',
        'summary': f'Summary {idx}',
        'full_text': f'Full text of article {idx}',
        'published': datetime(2026, 4, 17, 8, 0, tzinfo=timezone.utc),
        'source_name': f'Source{idx}',
        'source_icon': '📰',
        'source_tier': 0 if idx == 0 else 2,
        'image': f'https://example.com/img{idx}.jpg',
    }


class TestEventStoreIntegration(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        os.remove(self.db_path)
        self.store = EventStore(self.db_path, auto_recover=False)

    def tearDown(self):
        self.store.close()
        for suffix in ('', '-wal', '-shm'):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_upsert_new_article_returns_true(self):
        is_new = self.store.upsert_event(_fixture_article(0))
        self.assertTrue(is_new)

    def test_upsert_existing_article_returns_false(self):
        self.store.upsert_event(_fixture_article(0))
        is_new = self.store.upsert_event(_fixture_article(0))
        self.assertFalse(is_new)

    def test_save_analysis_marks_article_analyzed(self):
        item = _fixture_article(0)
        self.store.upsert_event(item)
        self.store.save_analysis(item['url'], {
            'importance': 5,
            'chinese_title': '中文标题',
            'why_it_matters': '原因',
            'categories': ['大模型发布'],
            'ai_relevant': True,
        })
        # 查回去断言 status / importance 已更新
        row = self.store.db.execute(
            "SELECT status, importance, analysis FROM articles WHERE url=?",
            (item['url'],)
        ).fetchone()
        self.assertEqual(row[0], 'analyzed')
        self.assertEqual(row[1], 5)
        analysis = json.loads(row[2])
        self.assertEqual(analysis['chinese_title'], '中文标题')

    def test_create_and_retrieve_canonical_event(self):
        self.store.create_canonical_event(
            event_id='evt_test_1',
            title='OpenAI 发布 GPT-5',
            summary='summary',
            canonical_url='https://example.com/a0',
            canonical_source='OpenAI Blog',
            canonical_article_id='art_0',
            entity_tags=['OpenAI', 'GPT-5'],
            event_type='model_release',
            status='reported',
            importance=5,
            published_at=datetime(2026, 4, 17, tzinfo=timezone.utc).isoformat(),
        )
        ev = self.store.get_canonical_event('evt_test_1')
        self.assertIsNotNone(ev)
        self.assertEqual(ev['title'], 'OpenAI 发布 GPT-5')
        self.assertEqual(ev['status'], 'reported')
        self.assertEqual(ev['importance'], 5)

    def test_evidence_chain_and_status_upgrade(self):
        self.store.create_canonical_event(
            event_id='evt_test_2',
            title='Event 2',
            canonical_url='https://example.com/a0',
            canonical_source='Source0',
            canonical_article_id='art_0',
            status='rumor',
            importance=3,
            published_at=datetime(2026, 4, 17, tzinfo=timezone.utc).isoformat(),
        )
        for i in range(1, 4):
            self.store.add_evidence(
                event_id='evt_test_2',
                article_id=f'art_{i}',
                role='follower',
                source_name=f'Source{i}',
            )
        evidence = self.store.get_evidence_for_event('evt_test_2')
        self.assertGreaterEqual(len(evidence), 3)

        # 手动升级状态并断言持久化
        self.store.upgrade_event_status('evt_test_2', 'confirmed')
        ev = self.store.get_canonical_event('evt_test_2')
        self.assertEqual(ev['status'], 'confirmed')

    def test_stats_returns_counts(self):
        for i in range(3):
            self.store.upsert_event(_fixture_article(i))
        stats = self.store.stats()
        self.assertIn('total_articles', stats)
        self.assertGreaterEqual(stats['total_articles'], 3)

    def test_missing_url_upsert_returns_false(self):
        is_new = self.store.upsert_event({'title': 'no url'})
        self.assertFalse(is_new)


if __name__ == '__main__':
    unittest.main()
