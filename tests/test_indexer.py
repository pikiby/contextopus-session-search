import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from session_search.indexer import Indexer, serve, request_index
from session_search.store import Store


def record(kind, **payload):
    return (json.dumps(dict(type=kind, payload=payload)) + '\n').encode()


def message(role, text):
    return record('response_item', type='message', role=role,
                  content=[dict(type='input_text' if role == 'user' else 'output_text', text=text)])


class Embedder:
    count_tokens = staticmethod(lambda text: len(text) // 4)
    encode = staticmethod(lambda texts: [[1.0, 0.0] for _ in texts])
    encode_query = staticmethod(lambda text: [1.0, 0.0])


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'sessions').mkdir()
        (self.home / 'archived_sessions').mkdir()
        self.path = self.home / 'sessions/rollout-example.jsonl'
        self.path.write_bytes(record('session_meta', id='sid', cwd='/example') + message('user', 'question'))
        self.store = Store(self.home / 'index.sqlite3', 'fake-test', 2)
        self.addCleanup(self.store.close)
        self.actor = Indexer(self.home, self.store, Embedder(), selection_path=self.home / 'selection.json')

    def drain(self):
        for _ in range(100):
            if not self.actor.step():
                return
        self.fail('Did not become idle')

    def test_append_restart_archive_delete(self):
        self.drain()
        self.assertEqual(self.store.stats()['vectors'], 1)
        with self.path.open('ab') as stream:
            stream.write(message('assistant', 'answer'))
        self.actor.mark(self.path)
        self.drain()
        self.assertEqual(self.store.stats()['vectors'], 1)
        self.actor = Indexer(self.home, self.store, Embedder(), selection_path=self.home / 'selection.json')
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 1)
        archived = self.home / 'archived_sessions' / self.path.name
        self.path.rename(archived)
        from session_search.settings import save_selection
        save_selection('archived', True, self.home / 'selection.json')
        self.actor.selection['archived'] = True
        self.actor.reconcile()
        self.drain()
        matches = self.actor.handle_request(dict(op='search', query='question', state='archived'))['matches']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['session']['path'], str(archived))
        archived.unlink()
        self.actor.reconcile()
        self.assertEqual(self.store.stats()['sessions'], 0)

    def test_same_size_rewrite_resets(self):
        self.drain()
        before = self.path.stat()
        self.path.write_bytes(self.path.read_bytes().replace(b'question', b'different'))
        import os
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        self.actor.mark(self.path)
        self.drain()
        matches = self.actor.handle_request(dict(op='search', query='new'))['matches']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['text'], 'User: different')

    def test_replacement_new_identity_without_sweep(self):
        self.drain()
        self.path.write_bytes(record('session_meta', id='new-sid') + message('user', 'replacement'))
        self.actor.mark(self.path)
        self.drain()
        matches = self.actor.handle_request(dict(op='search', query='new'))['matches']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['session']['id'], 'new-sid')
        self.assertEqual(self.store.stats()['sessions'], 1)

    def test_failed_enumeration_never_removes_sessions(self):
        self.drain()
        with patch('session_search.indexer.os.walk', side_effect=PermissionError):
            self.actor.reconcile()
        self.assertEqual(self.store.stats()['sessions'], 1)
        self.assertTrue(self.actor.status()['errors'])

    def test_request_priority_and_home_validation(self):
        import queue
        response = queue.Queue()
        self.actor.requests.put((dict(op='status'), response))
        self.actor.step()
        self.assertTrue(response.get()['ok'])
        self.assertEqual(self.store.stats()['sessions'], 0)
        with self.assertRaises(ValueError):
            self.actor.handle_request(dict(op='status', codex_home='/unrelated'))

    def test_truncation_and_incomplete_tail(self):
        self.drain()
        answer = message('assistant', 'answer')
        with self.path.open('ab') as stream:
            stream.write(answer[:15])
        self.actor.mark(self.path)
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 1)
        with self.path.open('ab') as stream:
            stream.write(answer[15:])
        self.actor.mark(self.path)
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 1)
        self.path.write_bytes(record('session_meta', id='sid'))
        self.actor.mark(self.path)
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 0)

    def test_bounded_file_continues_and_unchanged_skips(self):
        with self.path.open('ab') as stream:
            stream.write(record('response_item', type='function_call_output', output='x' * (1024 * 1024)))
            stream.write(message('assistant', 'last answer'))
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 1)
        with patch('session_search.reader.read_increment', side_effect=AssertionError('unchanged read')):
            self.actor.process_file(str(self.path))
        self.assertFalse(self.actor.errors)

    def test_malformed_file_does_not_block_other_files(self):
        (self.home / 'sessions/rollout-bad.jsonl').write_bytes(b'not metadata\n')
        self.drain()
        self.assertEqual(self.store.stats()['sessions'], 1)
        self.assertTrue(self.actor.errors)

    def test_empty_unknown_does_not_block_deletion(self):
        self.drain()
        empty = self.home / 'sessions/rollout-empty.jsonl'
        empty.write_bytes(b'')
        self.path.unlink()
        self.actor.reconcile()
        self.assertEqual(self.store.stats()['sessions'], 0)
        self.actor.process_file(str(empty))
        self.assertFalse(self.actor.errors)

    def test_transient_empty_known_preserves_until_header_written(self):
        self.drain()
        self.path.write_bytes(b'')
        self.actor = Indexer(self.home, self.store, Embedder(), selection_path=self.home / 'selection.json')
        self.drain()
        self.assertEqual(self.store.stats()['sessions'], 1)
        self.assertEqual(self.store.stats()['chunks'], 1)
        self.assertFalse(self.actor.errors)
        header = record('session_meta', id='replacement')
        self.path.write_bytes(header[:20])
        self.actor.reconcile()
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 1)
        self.assertFalse(self.actor.errors)
        with self.path.open('ab') as stream:
            stream.write(header[20:] + message('user', 'new content'))
        self.actor.mark(self.path)
        self.drain()
        matches = self.actor.handle_request(dict(op='search', query='new'))['matches']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['session']['id'], 'replacement')

    def test_search_arriving_during_read_sees_pending_new_jobs(self):
        import queue
        response = queue.Queue()
        original = self.actor.process_file
        def read_then_request(path):
            original(path)
            self.actor.requests.put((dict(op='search', query='question'), response))
        with patch.object(self.actor, 'process_file', side_effect=read_then_request):
            self.actor.step()
        self.assertTrue(self.actor.cached_status()['index_pending'])
        self.assertEqual(self.actor.cached_status()['stats']['jobs']['pending'], 1)
        self.actor.step()
        result = response.get_nowait()
        self.assertTrue(result['ok'])
        self.assertEqual(result['matches'], [])
        self.assertTrue(result['index_pending'])
        self.drain()
        self.assertEqual(self.actor.cached_status()['stats']['vectors'], 1)
        self.assertFalse(self.actor.cached_status()['index_pending'])

    def test_status_counts_are_rate_limited_but_activity_is_immediate(self):
        with patch.object(self.store, 'stats', wraps=self.store.stats) as stats:
            with patch('session_search.indexer.time.monotonic', return_value=self.actor._stats_refreshed_at + 1):
                for _ in range(20):
                    self.actor.status()
                with self.actor._working('embedding', 'sid', self.path):
                    self.assertEqual(self.actor.cached_status()['activity']['session_id'], 'sid')
                self.assertIsNone(self.actor.cached_status()['activity'])
                self.assertEqual(stats.call_count, 0)
            with patch('session_search.indexer.time.monotonic', return_value=self.actor._stats_refreshed_at + 2):
                self.actor.status()
                self.actor.status()
                self.assertEqual(stats.call_count, 1)
            self.actor.status(force=True)
            self.assertEqual(stats.call_count, 2)
        self.drain()
        self.assertEqual(self.actor.cached_status()['stats']['vectors'], 1)
        self.assertFalse(self.actor.cached_status()['index_pending'])

    def test_status_remains_live_during_inference(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        results, failures = [], []
        original = self.actor.embedder.encode
        def blocked(texts):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Status client did not release inference')
            return original(texts)
        self.actor.embedder.encode = blocked
        sock = self.home / 'live-status.sock'
        server = serve(self.actor, sock)
        def client():
            try:
                self.assertTrue(entered.wait(3))
                # Reading SQLite from this handler thread would fail thread affinity.
                result = request_index(dict(op='status'), sock, timeout=1)
                results.append(result)
                result['stats']['sessions'] = -1
                results.append(request_index(dict(op='status'), sock, timeout=1))
            except Exception as exc:
                failures.append(exc)
            finally:
                release.set()
        thread = threading.Thread(target=client)
        thread.start()
        try:
            self.actor.step()
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertNotEqual(results[1]['stats']['sessions'], -1)
            self.assertEqual(results[1]['activity']['operation'], 'embedding')
            self.assertEqual(results[1]['activity']['session_id'], 'sid')
            self.assertEqual(results[1]['activity']['path'], str(self.path))
            self.assertTrue(results[1]['activity']['started_at'])
            self.assertTrue(results[1]['index_pending'])
            self.assertEqual(results[1]['pid'], __import__('os').getpid())
            self.assertTrue(results[1]['started_at'])
            self.assertTrue(self.actor.cached_status()['last_completed_at'])
            self.assertIsNone(self.actor.cached_status()['activity'])
            self.assertTrue(self.actor.requests.empty())
            with self.assertRaises(RuntimeError):
                request_index(dict(op='status', codex_home=str(self.home / 'other')), sock, timeout=1)
        finally:
            release.set()
            thread.join(4)
            server.shutdown()
            server.server_close()
            server.socket_lock.close()

    def test_mcp_search_history_real_socket(self):
        import os
        import threading
        import time
        from session_search import server as mcp
        self.drain()
        sock = self.home / 'history.sock'
        rpc = serve(self.actor, sock)
        results, errors = [], []
        def client():
            try:
                with patch.dict(os.environ, CODEX_SESSION_INDEX_SOCKET=str(sock)):
                    results.append(mcp.tool_session_search(dict(codex_home=str(self.home), query='Найти задачу')))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=client)
        thread.start()
        try:
            deadline = time.monotonic()+5
            while thread.is_alive() and time.monotonic()<deadline:
                self.actor.step()
                time.sleep(.005)
            thread.join(.1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            payload = json.loads(results[0][0]['text'])
            row = self.store.db.execute('SELECT * FROM search_requests').fetchone()
            self.assertEqual(row['state'], 'completed')
            self.assertEqual(row['result_count'], len(payload['sessions']))
            self.assertIn('Найти задачу', row['parameters'])
            self.assertGreater(row['duration_seconds'], 0)
            found = self.store.db.execute('SELECT session_id,chunk_id FROM search_results').fetchall()
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]['session_id'], 'sid')
            self.assertTrue(found[0]['chunk_id'])
            status = self.actor.status(force=True)
            self.assertEqual(status['search_metrics']['calls'], 1)
            self.assertEqual(status['embedding_metrics']['count'], 1)
            self.assertGreater(status['database_bytes'], 0)
        finally:
            rpc.shutdown()
            rpc.server_close()
            rpc.socket_lock.close()

    def test_socket_rpc_and_ownership(self):
        import os
        import threading
        import time
        sock = self.home / 'rpc.sock'
        server = serve(self.actor, sock)
        try:
            self.assertEqual(sock.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(BlockingIOError):
                serve(self.actor, sock)
            results = []
            def client():
                with patch.dict(os.environ, CODEX_SESSION_INDEX_SOCKET=str(sock)):
                    results.append(request_index(dict(op='status'), timeout=3))
            thread = threading.Thread(target=client)
            thread.start()
            deadline = time.monotonic() + 3
            while thread.is_alive() and time.monotonic() < deadline:
                if not self.actor.requests.empty():
                    self.actor.step()
                time.sleep(0.005)
            thread.join(timeout=1)
            self.assertTrue(results[0]['ok'])
            self.assertEqual(results[0]['codex_home'], str(self.home))
        finally:
            server.shutdown()
            server.server_close()
            server.socket_lock.close()


if __name__ == '__main__':
    unittest.main()


class CoreConfigurationTests(unittest.TestCase):
    def test_affinity_limits_and_rejects_invalid_count(self):
        from session_search.indexer import configure_cores
        with patch('session_search.indexer.core_groups', return_value=[[2, 10], [4, 12], [6, 14], [8, 16]]), patch('session_search.indexer.os.sched_setaffinity') as set_affinity:
            self.assertEqual(configure_cores(2), 2)
            set_affinity.assert_called_once_with(0, [2, 4])
            for value in (0, -1, 5, True):
                with self.assertRaises(ValueError):
                    configure_cores(value)
