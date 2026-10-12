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
        self.actor = Indexer(self.home, self.store, Embedder())

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
        self.assertEqual(self.store.stats()['vectors'], 2)
        self.actor = Indexer(self.home, self.store, Embedder())
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 2)
        archived = self.home / 'archived_sessions' / self.path.name
        self.path.rename(archived)
        self.actor.reconcile()
        self.drain()
        matches = self.actor.handle_request(dict(op='search', query='question', state='archived'))['matches']
        self.assertEqual(len(matches), 2)
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
        self.assertEqual(matches[0]['text'], 'different')

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
        self.assertEqual(self.store.stats()['chunks'], 2)
        self.path.write_bytes(record('session_meta', id='sid'))
        self.actor.mark(self.path)
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 0)

    def test_bounded_file_continues_and_unchanged_skips(self):
        with self.path.open('ab') as stream:
            stream.write(record('response_item', type='function_call_output', output='x' * (1024 * 1024)))
            stream.write(message('assistant', 'last answer'))
        self.drain()
        self.assertEqual(self.store.stats()['chunks'], 2)
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
        self.actor = Indexer(self.home, self.store, Embedder())
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
