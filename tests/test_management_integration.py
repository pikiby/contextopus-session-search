"""Cleanup API routing and serialized search-store consistency."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from session_search import server, session_management
from session_search.indexer import Indexer
from session_search.store import Store

SID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class Embedder:
    count_tokens = staticmethod(lambda text: len(text) // 4)
    encode = staticmethod(lambda texts: [[1., 0.] for _ in texts])
    encode_query = staticmethod(lambda text: [1., 0.])


class ManagementIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.home / 'config'))
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, name, arguments):
        result = server.handle_request(dict(jsonrpc='2.0', id=1, method='tools/call',
                                           params=dict(name=name, arguments=arguments)))
        self.assertNotIn('error', result, result)
        self.assertFalse(result['result'].get('isError'), result)
        return json.loads(result['result']['content'][0]['text'])

    def test_mcp_tools_registered_with_required_confirmation(self):
        response = server.handle_request(dict(jsonrpc='2.0', id=1, method='tools/list'))
        tools = {item['name']: item['inputSchema'] for item in response['result']['tools']}
        for name in ('session_list', 'session_cleanup_preview', 'session_cleanup_apply'):
            self.assertIn(name, tools)
        self.assertIn('filters', tools['session_list']['properties'])
        self.assertIn('action', tools['session_cleanup_preview']['required'])
        self.assertEqual(set(tools['session_cleanup_apply']['required']), {'plan', 'confirmation'})

    def test_inventory_and_preview_route_filters_without_index_mutation(self):
        filters = {'cwd_prefix': '/work', 'models': ['sample-model'], 'min_bytes': 10}
        home = str(self.home)
        with patch.object(session_management, 'inventory', return_value={'count': 0, 'sessions': []}) as inventory:
            self.assertEqual(self.call('session_list', dict(codex_home=home, filters=filters))['count'], 0)
            self.assertEqual(inventory.call_args.args[1], filters)
            self.assertEqual(Path(inventory.call_args.args[0]), self.home)
        with patch.object(session_management, 'preview', return_value={'plan_id': 'token', 'items': []}) as preview:
            self.assertEqual(self.call('session_cleanup_preview', dict(codex_home=home, action='archive', filters=filters))['plan_id'], 'token')
            self.assertEqual(preview.call_args.args[1:], ('archive', filters))

    def test_apply_routes_exact_plan_to_serialized_actor(self):
        plan = {'plan_id': 'token', 'items': [{'id': SID}]}
        with patch.dict(os.environ, CODEX_THREAD_ID=OTHER), patch.object(server.indexer, 'request_index', return_value={'ok': True, 'completed': []}) as request:
            self.assertTrue(self.call('session_cleanup_apply', dict(codex_home=str(self.home), plan=plan, confirmation='token'))['ok'])
        sent = request.call_args.args[0]
        self.assertEqual(sent, dict(op='session_cleanup_apply', codex_home=str(self.home), plan=plan, confirmation='token', protected_ids=[OTHER]))
        self.assertEqual(request.call_args.kwargs['timeout'], server.indexer.cleanup_timeout(plan))

    def test_wrong_confirmation_never_contacts_actor(self):
        with patch.object(server.indexer, 'request_index') as request:
            response = server.handle_request(dict(jsonrpc='2.0', id=1, method='tools/call',
                params=dict(name='session_cleanup_apply', arguments=dict(codex_home=str(self.home),
                    plan={'plan_id': 'expected', 'items': []}, confirmation='wrong'))))
        self.assertTrue(response['result']['isError'])
        request.assert_not_called()

    def test_actor_callback_preserves_shared_vectors_and_unrelated_sessions(self):
        for sid in (SID, OTHER):
            path = self.home / 'sessions' / f'rollout-{sid}.jsonl'
            path.parent.mkdir(exist_ok=True)
            records = [dict(type='session_meta', payload=dict(id=sid, cwd='/work')),
                       dict(type='response_item', payload=dict(type='message', role='user',
                            content=[dict(type='input_text', text='identical shared question')]))]
            path.write_text(''.join(json.dumps(row) + '\n' for row in records))
        store = Store(self.home / 'index.sqlite3', 'fixture-model', 2)
        self.addCleanup(store.close)
        actor = Indexer(self.home, store, Embedder(), selection_path=self.home / 'selection.json')
        for _ in range(100):
            if not actor.step():
                break
        self.assertEqual(store.stats()['sessions'], 2)
        self.assertEqual(store.stats()['vectors'], 1)
        original_vectors = store.db.execute('SELECT rowid FROM vectors').fetchall()
        original = self.home / 'sessions' / f'rollout-{SID}.jsonl'
        target = self.home / 'archived_sessions' / original.name
        def archive(home, plan, confirmation, on_change, protected_ids):
            target.parent.mkdir()
            original.rename(target)
            on_change({'id': SID, 'path': str(original)}, str(target))
            return {'ok': True}
        with patch.object(session_management, 'apply', side_effect=archive):
            actor.handle_request(dict(op='session_cleanup_apply', plan={}, confirmation='token'))
        sessions = {item['id']: item for item in store.sessions()}
        self.assertEqual(sessions[SID]['state'], 'archived')
        self.assertEqual(sessions[SID]['path'], str(target))
        self.assertEqual(sessions[OTHER]['state'], 'active')
        def delete(home, plan, confirmation, on_change, protected_ids):
            target.unlink()
            on_change({'id': SID, 'path': str(target)}, None)
            return {'ok': True}
        with patch.object(session_management, 'apply', side_effect=delete):
            actor.handle_request(dict(op='session_cleanup_apply', plan={}, confirmation='token'))
        self.assertEqual([item['id'] for item in store.sessions()], [OTHER])
        self.assertEqual(store.stats()['vectors'], 1)
        self.assertEqual(store.db.execute('SELECT rowid FROM vectors').fetchall(), original_vectors)
        self.assertEqual(len(store.search([1., 0.], selection={'active': True, 'archived': True})), 1)
