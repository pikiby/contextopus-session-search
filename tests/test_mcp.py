"""Real stdio protocol round trips and semantic-client contract checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from session_search import server

SID = '12345678-1234-1234-1234-123456789abc'

class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        environment = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.home / 'config'))
        environment.start()
        self.addCleanup(environment.stop)
        folder = self.home / 'sessions'
        folder.mkdir()
        self.path = folder / f'rollout-2026-10-12T01-00-00-{SID}.jsonl'
        records = [
            {'type': 'session_meta', 'payload': {'id': SID, 'cwd': '/tmp/project', 'timestamp': '2026-10-12'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Найти задачу search'}},
        ]
        self.path.write_text('\n'.join(json.dumps(x) for x in records)+'\n')
        (self.home / 'session_index.jsonl').write_text(json.dumps({'id': SID, 'thread_name': 'Search task'})+'\n')

    def stdio(self, messages):
        env = dict(os.environ, CODEX_HOME=str(self.home))
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        p = subprocess.run([sys.executable, '-m', 'session_search.server'], input='\n'.join(json.dumps(x) if not isinstance(x, str) else x for x in messages)+'\n', capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(line) for line in p.stdout.splitlines()]

    def call(self, name, arguments, id=1):
        return {'jsonrpc': '2.0', 'id': id, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}

    def test_stdio_initialize_list_literal_info_resume(self):
        out = self.stdio([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            self.call('session_search', {'query': 'задачу', 'mode': 'literal', 'include_snippets': True}, 3),
            self.call('session_info', {'session': SID}, 4),
            self.call('session_resume_command', {'session': SID}, 5),
        ])
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]['result']['capabilities'], {'tools': {}})
        self.assertEqual({t['name'] for t in out[1]['result']['tools']}, {'session_search', 'session_info', 'session_resume_command', 'index_status'})
        search = json.loads(out[2]['result']['content'][0]['text'])
        self.assertEqual(search['total'], 1)
        self.assertEqual(search['sessions'][0]['snippets'][0]['field'], 'user_message')
        info = json.loads(out[3]['result']['content'][0]['text'])
        self.assertEqual(info['name'], 'Search task')
        self.assertEqual(info['first_user_message'], 'Найти задачу search')
        resume = json.loads(out[4]['result']['content'][0]['text'])
        self.assertEqual(resume['command'], f'codex resume {SID}')

    def test_stdio_errors(self):
        out = self.stdio(['bad json', [], {'jsonrpc': '2.0', 'id': 1, 'method': 'missing'}, self.call('session_archive', {}), self.call('session_search', {'mode': 'literal'}), self.call('session_info', {'session': '/tmp/outside'})])
        self.assertEqual([x['error']['code'] for x in out[:4]], [-32700, -32600, -32601, -32602])
        self.assertTrue(out[4]['result']['isError'])
        self.assertTrue(out[5]['result']['isError'])

    def test_semantic_default_grouping_and_offsets(self):
        match = {'session': {'id': SID, 'cwd': '/tmp/project', 'path': str(self.path)}, 'state': 'active', 'distance': .2, 'text': 'Найти задачу', 'id': 4, 'start_offset': 10, 'end_offset': 50, 'role': 'user', 'turn_id': 'turn', 'part': 0, 'question_start_offset': 10, 'sources': [{'role': 'user', 'turn_id': 'turn', 'start_offset': 10, 'end_offset': 30, 'char_start': 0, 'char_end': 12}, {'role': 'assistant', 'turn_id': 'turn', 'start_offset': 30, 'end_offset': 50, 'char_start': 0, 'char_end': 8}]}
        with patch.object(server.indexer, 'request_index', return_value={'ok': True, 'matches': [match, dict(match, id=5)], 'stats': {'pending': 2}, 'index_pending': True}) as client:
            response = server.handle_request(self.call('session_search', {'codex_home': str(self.home), 'query': 'task', 'include_snippets': True}))
        client.assert_any_call({'op': 'search', 'query': 'task', 'state': 'all', 'limit': 20, 'codex_home': str(self.home)})
        result = json.loads(response['result']['content'][0]['text'])
        self.assertEqual(result['total'], 1)
        self.assertTrue(result['index_pending'])
        self.assertEqual(result['sessions'][0]['name'], 'Search task')
        self.assertEqual(len(result['sessions'][0]['snippets']), 2)
        self.assertEqual(result['sessions'][0]['snippets'][0]['start_offset'], 10)
        self.assertEqual(result['sessions'][0]['snippets'][0]['source']['sources'], match['sources'])

    def test_disabled_search_groups_cannot_be_overridden_and_info_readable(self):
        from session_search.settings import save_selection
        archived = self.home / 'archived_sessions'
        archived.mkdir()
        self.path.rename(archived / self.path.name)
        arguments = {'codex_home': str(self.home), 'query': 'задачу', 'mode': 'literal', 'state': 'archived'}
        result = json.loads(server._tool_session_search(arguments)[0]['text'])
        self.assertEqual(result['total'], 0)
        info = json.loads(server.tool_session_info({'codex_home': str(self.home), 'session': SID})[0]['text'])
        self.assertEqual(info['state'], 'archived')
        save_selection('archived', True)
        self.assertEqual(json.loads(server._tool_session_search(arguments)[0]['text'])['total'], 1)
        save_selection('active', False)
        save_selection('archived', False)
        self.assertEqual(json.loads(server._tool_session_search(dict(arguments, state='all'))[0]['text'])['total'], 0)

    def test_semantic_response_respects_disabled_group(self):
        match = {'session': {'id': SID}, 'state': 'archived'}
        with patch.object(server.indexer, 'request_index', return_value={'ok': True, 'matches': [match]}):
            result = json.loads(server.tool_session_semantic_search({'codex_home': str(self.home), 'query': 'task', 'state': 'archived'})[0]['text'])
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['session_selection'], {'active': True, 'archived': False})

    def test_index_status_and_unavailable(self):
        with patch.object(server.indexer, 'request_index', return_value={'ok': True, 'stats': {'pending': 0}}) as client:
            response = server.handle_request(self.call('index_status', {'codex_home': str(self.home)}))
        self.assertFalse(response['result']['isError'])
        client.assert_any_call({'op': 'status', 'codex_home': str(self.home)})
        with patch.object(server.indexer, 'request_index', side_effect=OSError('unavailable')):
            response = server.handle_request(self.call('session_search', {'codex_home': str(self.home), 'query': 'task'}))
        self.assertTrue(response['result']['isError'])
        self.assertIn('Session indexer unavailable', response['result']['content'][0]['text'])

if __name__ == '__main__':
    unittest.main()
