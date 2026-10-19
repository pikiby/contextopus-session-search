import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_search.session_management import inventory, preview, apply

SID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class SessionManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.write(SID)

    def write(self, sid, state='sessions'):
        path = self.home / state / '2026' / f'rollout-{sid}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [dict(type='session_meta', payload=dict(id=sid, cwd='/work/project', timestamp='2026-01-01T00:00:00Z')),
                   dict(type='turn_context', payload=dict(model='exact-model')),
                   dict(type='response_item', timestamp='2026-01-03T00:00:00Z', payload=dict(type='message', role='user', content=[]))]
        path.write_text(''.join(json.dumps(row) + '\n' for row in records))
        return path

    def test_metadata_filters(self):
        self.assertEqual(inventory(self.home, {'models': ['exact-model'], 'cwd_prefix': '/work',
                                              'created_before': '2026-01-02', 'last_message_after': '2026-01-02'})['count'], 1)
        for filters in ({'models': ['provider']}, {'cwd_prefix': '/wor'}, {'max_bytes': 0}, {'state': 'archived'}):
            self.assertEqual(inventory(self.home, filters)['count'], 0)
        with self.assertRaises(ValueError):
            inventory(self.home, {'unknown': True})
        self.assertEqual(inventory(self.home)['sessions'][0]['last_message_at'], '2026-01-03T00:00:00Z')

    def test_confirmation_and_stale_plan_never_invoke(self):
        plan = preview(self.home, 'delete')
        calls = []
        with self.assertRaises(ValueError):
            apply(self.home, plan, 'yes', runner=lambda *a, **k: calls.append(a))
        self.path.write_text(self.path.read_text() + '\n')
        with self.assertRaises(ValueError):
            apply(self.home, plan, plan['plan_id'], runner=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_native_archive_and_callback(self):
        plan = preview(self.home, 'archive')
        callbacks = []
        def runner(command, **kwargs):
            self.assertEqual(command, ['codex', 'archive', SID])
            self.assertEqual(kwargs['env']['CODEX_HOME'], str(self.home))
            target = self.home / 'archived_sessions' / self.path.name
            target.parent.mkdir()
            self.path.rename(target)
            return subprocess.CompletedProcess(command, 0, '', '')
        result = apply(self.home, plan, plan['plan_id'], lambda *args: callbacks.append(args), runner=runner)
        self.assertTrue(result['ok'])
        self.assertEqual(callbacks[0][1], str(self.home / 'archived_sessions' / self.path.name))

    def test_native_delete_and_failure(self):
        plan = preview(self.home, 'delete')
        def runner(command, **kwargs):
            self.assertEqual(command, ['codex', 'delete', '--force', SID])
            self.path.unlink()
            return subprocess.CompletedProcess(command, 0, '', '')
        self.assertTrue(apply(self.home, plan, plan['plan_id'], runner=runner)['ok'])
        self.write(SID)
        plan = preview(self.home, 'delete')
        failure = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, '', 'native failure')
        result = apply(self.home, plan, plan['plan_id'], runner=failure)
        self.assertFalse(result['ok'])
        self.assertTrue(self.path.exists())

    def test_symlinks_and_tampering(self):
        alias = self.home / 'sessions' / 'rollout-alias.jsonl'
        alias.symlink_to(self.path)
        self.assertEqual(inventory(self.home)['count'], 1)
        plan = preview(self.home, 'delete')
        plan['items'][0]['id'] = OTHER
        with self.assertRaises(ValueError):
            apply(self.home, plan, plan['plan_id'])

    def test_reject_native_cascade(self):
        with sqlite3.connect(self.home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT)')
            db.execute('INSERT INTO thread_spawn_edges VALUES (?,?)', (SID, OTHER))
        plan = preview(self.home, 'delete')
        self.assertEqual(plan['blocked_ids'], [OTHER])
        with self.assertRaisesRegex(ValueError, 'cascades'):
            apply(self.home, plan, plan['plan_id'])

    def test_changed_native_postcondition_is_failure(self):
        plan = preview(self.home, 'archive')
        result = apply(self.home, plan, plan['plan_id'], runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, '', ''))
        self.assertFalse(result['ok'])
        self.assertIn('expected session state', result['error']['message'])

    def test_selected_children_are_deleted_first(self):
        child_path = self.write(OTHER)
        with sqlite3.connect(self.home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT)')
            db.execute('INSERT INTO thread_spawn_edges VALUES (?,?)', (SID, OTHER))
        plan = preview(self.home, 'delete')
        self.assertEqual([item['id'] for item in plan['items']], [OTHER, SID])
        self.assertEqual(plan['blocked_count'], 0)
        called = []
        def runner(command, **kwargs):
            sid = command[-1]
            called.append(sid)
            (child_path if sid == OTHER else self.path).unlink()
            with sqlite3.connect(self.home / 'state_5.sqlite') as db:
                db.execute('DELETE FROM thread_spawn_edges WHERE child_thread_id=? OR parent_thread_id=?', (sid, sid))
            return subprocess.CompletedProcess(command, 0, '', '')
        self.assertTrue(apply(self.home, plan, plan['plan_id'], runner=runner)['ok'])
        self.assertEqual(called, [OTHER, SID])

    def test_protected_session_rejected(self):
        plan = preview(self.home, 'archive')
        with self.assertRaisesRegex(ValueError, 'current'):
            apply(self.home, plan, plan['plan_id'], protected_ids=[SID])

    def test_invalid_filters(self):
        for filters in ([], False, {'ids': ['not-a-uuid']}, {'created_after': '2026-02-01', 'created_before': '2026-01-01'}):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                inventory(self.home, filters)

    def test_missing_dates_and_corrupted_lines(self):
        rows = [dict(type='session_meta', payload=dict(id=SID, cwd='/work/project')),
                dict(type='turn_context', payload=dict(model='one')),
                dict(type='turn_context', payload=dict(model='two'))]
        self.path.write_bytes(b'\xff\n' + b'\n'.join(json.dumps(row).encode() for row in rows) + b'\n')
        self.assertEqual(inventory(self.home, {'created_before': '2027-01-01'})['count'], 0)
        self.assertEqual(inventory(self.home, {'last_message_before': '2027-01-01'})['count'], 0)
        self.assertEqual(inventory(self.home, {'models': ['absent', 'two']})['count'], 1)

    def test_graph_changed_since_preview(self):
        plan = preview(self.home, 'delete')
        with sqlite3.connect(self.home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT)')
            db.execute('INSERT INTO thread_spawn_edges VALUES (?,?)', (SID, OTHER))
        with self.assertRaisesRegex(ValueError, 'cascade changed'):
            apply(self.home, plan, plan['plan_id'])

    def test_duplicate_id_refused(self):
        self.write(SID, 'archived_sessions')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            preview(self.home, 'delete')

    def test_partial_native_failure_reports_completed(self):
        self.write(OTHER)
        plan = preview(self.home, 'delete')
        calls = []
        def runner(command, **kwargs):
            calls.append(command[-1])
            if len(calls) == 2:
                return subprocess.CompletedProcess(command, 1, '', 'second failed')
            Path(plan['items'][0]['path']).unlink()
            return subprocess.CompletedProcess(command, 0, '', '')
        result = apply(self.home, plan, plan['plan_id'], runner=runner)
        self.assertEqual(result['count'], 1)
        self.assertFalse(result['ok'])
        self.assertFalse(result['error']['native_completed'])

    def test_native_database_redirect_rejected_before_mutation(self):
        plan = preview(self.home, 'delete')
        with sqlite3.connect(self.home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads(id TEXT, archived INTEGER, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES (?,0,?)', (SID, str(self.home / 'elsewhere.jsonl')))
        with self.assertRaisesRegex(ValueError, 'differs'):
            apply(self.home, plan, plan['plan_id'])
        self.assertTrue(self.path.exists())

    def test_invalid_sqlite_reports_value_error(self):
        (self.home / 'state_5.sqlite').write_bytes(b'invalid database')
        with self.assertRaisesRegex(ValueError, 'Cannot inspect'):
            preview(self.home, 'delete')
