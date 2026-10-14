import importlib.util
import tempfile
import unittest
from pathlib import Path
from session_search.store import Store


@unittest.skipUnless(importlib.util.find_spec('sqlite_vec'), 'sqlite-vec not installed; real extension tests require project dependencies')
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'index.sqlite'
        self.store = Store(self.path, 'test-model', 2, max_attempts=2)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, text='one', state='active'):
        self.store.ingest({'id': 's', 'path': '/tmp/session.jsonl', 'state': state}, {'size': 20},
                          [{'id': 's:0', 'text': text, 'start_offset': 0, 'end_offset': 20, 'turn_id': 3, 'role': 'assistant', 'part': 1, 'question_start_offset': 0}])

    def embed(self, vector=(1, 0)):
        job = self.store.claim_job()
        self.assertTrue(self.store.complete_job(job['id'], job['content_hash'], vector))

    def test_search_filter_checkpoint_and_no_duplicate_jobs(self):
        self.ingest()
        self.embed()
        self.ingest()
        self.assertIsNone(self.store.claim_job())
        self.assertEqual(self.store.checkpoint('s'), {'size': 20})
        result = self.store.search((1, 0), state='active')
        self.assertEqual(result[0]['text'], 'one')
        self.assertEqual(result[0]['turn_id'], 3)
        self.assertEqual(result[0]['role'], 'assistant')
        self.assertEqual(result[0]['question_start_offset'], 0)
        self.assertEqual(result[0]['distance'], 0)
        self.assertEqual(self.store.search((1, 0), state='archived'), [])
        self.ingest(state='archived')
        self.assertEqual(len(self.store.search((1, 0), state='archived')), 1)

    def test_restart_recovers_running_job(self):
        self.ingest()
        self.store.claim_job()
        self.store.close()
        self.store = Store(self.path, 'test-model', 2)
        job = self.store.claim_job()
        self.assertEqual(job['attempts'], 2)
        self.assertTrue(self.store.complete_job(job['id'], job['content_hash'], (1, 0)))

    def test_interrupted_final_attempt_is_retained_without_busy_retry(self):
        self.ingest()
        first = self.store.claim_job(now=100)
        self.store.fail_job(first['id'], 'offline', now=100)
        self.store.claim_job(now=102)
        self.store.close()
        self.store = Store(self.path, 'test-model', 2, max_attempts=2)
        self.assertIsNone(self.store.claim_job(now=10000))
        self.assertEqual(self.store.stats()['jobs'], {'failed': 1})

    def test_stale_job_cannot_overwrite_changed_or_reintroduced_chunk(self):
        self.ingest()
        old = self.store.claim_job()
        self.ingest('two')
        self.ingest('one')
        new = self.store.claim_job()
        self.assertNotEqual(old['id'], new['id'])
        self.assertFalse(self.store.complete_job(old['id'], old['content_hash'], (0, 1)))
        self.assertTrue(self.store.complete_job(new['id'], new['content_hash'], (1, 0)))

    def test_bounded_retry_and_backoff(self):
        self.ingest()
        first = self.store.claim_job(now=100)
        self.store.fail_job(first['id'], 'offline', now=100)
        self.assertIsNone(self.store.claim_job(now=101))
        second = self.store.claim_job(now=102)
        self.store.fail_job(second['id'], 'offline', now=102)
        self.assertIsNone(self.store.claim_job(now=10000))
        self.assertEqual(self.store.stats()['jobs'], {'failed': 1})

    def test_deletion_and_reset_clear_vectors(self):
        self.ingest()
        self.embed()
        self.store.reset_session('s')
        self.assertEqual(self.store.stats()['vectors'], 0)
        self.ingest()
        self.embed()
        self.store.reconcile_sessions([])
        self.assertEqual(self.store.stats(), {'sessions': 0, 'chunks': 0, 'vectors': 0, 'jobs': {}})

    def test_append_preserves_existing_vectors(self):
        self.ingest()
        self.embed()
        self.store.ingest({'id': 's'}, {'size': 40},
                          [{'id': 's:1', 'text': 'two', 'start_offset': 20, 'end_offset': 40}])
        self.assertEqual(self.store.stats()['chunks'], 2)
        self.assertEqual(self.store.search((1, 0))[0]['text'], 'one')
        self.embed((0, 1))
        self.assertEqual(len(self.store.search((1, 0))), 2)

    def test_knn_filters_before_limit_and_matches_exact_distance(self):
        self.ingest(state='archived')
        self.embed((1, 0))
        self.store.ingest({'id': 'active', 'state': 'active'}, {},
                          [{'id': 'active:0', 'text': 'active text', 'start_offset': 0, 'end_offset': 11}])
        self.embed((0, 1))
        result = self.store.search((1, 0), limit=1, state='active')
        self.assertEqual(result[0]['id'], 'active:0')
        self.assertAlmostEqual(result[0]['distance'], 2 ** 0.5, places=5)
        all_results = self.store.search((1, 0), limit=200)
        self.assertEqual([r['id'] for r in all_results], ['s:0', 'active:0'])
        self.assertEqual(self.store.search((1, 0), limit=0), [])

    def test_newest_session_then_newest_fragment_and_existing_queue_migration(self):
        def add(sid, modified, offsets):
            self.store.ingest({'id': sid}, {'mtime_ns': modified},
                [{'id': f'{sid}:{offset}', 'text': 'text', 'start_offset': offset, 'end_offset': offset+1} for offset in offsets])
        add('old', 10, [0, 10])
        add('new', 20, [0, 10])
        # Simulate the previous queue schema; reopening must migrate pending jobs.
        self.store.db.execute('DROP INDEX jobs_newest')
        self.store.db.execute('ALTER TABLE jobs DROP COLUMN source_offset')
        self.store.db.execute('ALTER TABLE jobs DROP COLUMN priority_ns')
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path, 'test-model', 2)
        self.assertEqual(self.store.claim_job()['chunk_id'], 'new:10')
        self.assertEqual(self.store.claim_job()['chunk_id'], 'new:0')
        # Updating an older session reprioritizes its already queued fragments.
        add('old', 30, [20])
        self.assertEqual(self.store.claim_job()['chunk_id'], 'old:20')
        self.assertEqual(self.store.claim_job()['chunk_id'], 'old:10')
        self.assertEqual(self.store.claim_job()['chunk_id'], 'old:0')

    def test_identity_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            Store(self.path, 'different-model', 2)

    def test_invalid_snapshot_rolls_back_checkpoint(self):
        self.ingest()
        with self.assertRaises(KeyError):
            self.store.ingest({'id': 's'}, {'size': 99}, [{'id': 's:1', 'text': 'invalid'}])
        self.assertEqual(self.store.checkpoint('s'), {'size': 20})
        self.assertEqual(self.store.stats()['chunks'], 1)
