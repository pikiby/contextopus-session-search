import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from session_search.tui_backend import Backend, UNIT

class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = Backend(self.tmp.name, self.tmp.name)

    def run_result(self, args, **kwargs):
        if args[0] == 'systemctl':
            return subprocess.CompletedProcess(args, 0, 'ActiveState=active\nMainPID=42\nUnitFileState=enabled\n', '')
        return subprocess.CompletedProcess(args, 0, 'install ok installed\n0.1.0', '')

    def test_status_missing_activity_not_idle_and_counts(self):
        with patch.object(self.backend, '_run', side_effect=self.run_result), patch('session_search.tui_backend.indexer.request_index', return_value={'ok': True, 'index_pending': True, 'stats': {'sessions': 2, 'jobs': {'pending': 3, 'running': 1, 'retry': 2}}, 'errors': {}}):
            result = self.backend.status()
        self.assertEqual(result['pending'], 6)
        self.assertIs(result['index_pending'], True)
        self.assertEqual(result['operation'], 'unknown')
        self.assertIsNone(result['chunks'])
        self.assertEqual(result['pid'], '42')

    def test_unavailable_and_config_does_not_expose_secret(self):
        (Path(self.tmp.name)/'config.toml').write_text('[mcp_servers.session_search]\ncommand="/usr/bin/session-search"\nargs=["mcp"]\n[mcp_servers.session_search.env]\nSECRET="sensitive"\n')
        with patch.object(self.backend, '_run', side_effect=self.run_result), patch('session_search.tui_backend.indexer.request_index', side_effect=OSError('socket missing')):
            result = self.backend.status()
        self.assertTrue(result['mcp_configured'])
        self.assertNotIn('sensitive', str(result))
        self.assertIsNone(result['pending'])
        self.assertEqual(result['index_error'], 'socket missing')

    def test_actions_are_explicit_and_bounded(self):
        with patch.object(self.backend, '_run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertTrue(self.backend.action('restart')['ok'])
            run.assert_called_once_with(['systemctl', '--user', 'restart', UNIT])
        with self.assertRaises(ValueError):
            self.backend.action('delete')

    def test_mcp_check(self):
        messages = [{'id': 1, 'result': {'serverInfo': {'name': 'session-search'}}}, {'id': 2, 'result': {'tools': [{'name': x} for x in ('session_search', 'session_info', 'session_resume_command', 'index_status')]}}]
        with patch.object(self.backend, '_run', return_value=subprocess.CompletedProcess([], 0, '\n'.join(map(json.dumps, messages)), '')) as run:
            self.assertTrue(self.backend.check_mcp()['ok'])
            self.assertEqual(run.call_args.args[0], ['/usr/bin/session-search', 'mcp'])

    def test_search_and_journal(self):
        with patch('session_search.tui_backend.server.tool_session_search', return_value=[{'text': '{"sessions": []}'}]) as search:
            self.assertEqual(self.backend.search('query', 'active'), {'sessions': []})
            self.assertTrue(search.call_args.args[0]['include_snippets'])
        with patch.object(self.backend, '_run', return_value=subprocess.CompletedProcess([], 0, 'journal', '')):
            self.assertEqual(self.backend.journal(), 'journal')
        self.assertIn('args = ["mcp"]', self.backend.connection_config())


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.process = self.root / '42'
        self.process.mkdir()
        self.now = 10.0
        self.backend = Backend(self.tmp.name, self.tmp.name, proc_root=self.root, clock=lambda: self.now)
        self.backend._ticks_per_second = 100
        self.backend._page_size = 4096
        (self.root/'stat').write_text(''.join(f'cpu{i} 0 0 0 0 0 0 0 0\n' for i in range(8)))

    def write_proc(self, ticks=100, start=50, rss=3):
        fields = ['0'] * 22
        fields[0] = 'R'
        fields[11] = str(ticks)
        fields[12] = '20'
        fields[19] = str(start)
        (self.process / 'stat').write_text('42 (worker (CPU) process) ' + ' '.join(fields))
        (self.process / 'statm').write_text(f'100 {rss} 0')

    def test_indexer_cpu_share_of_whole_machine_and_first_unknown(self):
        self.write_proc()
        first = self.backend._resources('42')
        self.assertIsNone(first['cpu_percent'])
        self.assertEqual(first['memory_bytes'], 12288)
        self.assertEqual(first['process_identity'], '42:50')
        self.now += 2
        self.write_proc(ticks=500)
        self.assertEqual(self.backend._resources('42')['cpu_percent'], 25.0)

    def test_pid_reuse_missing_and_zero_are_distinct(self):
        self.write_proc()
        self.backend._resources('42')
        self.now += 1
        self.write_proc(ticks=200, start=60, rss=0)
        result = self.backend._resources('42')
        self.assertIsNone(result['cpu_percent'])
        self.assertEqual(result['memory_bytes'], 0)
        (self.process / 'stat').unlink()
        result = self.backend._resources('42')
        self.assertEqual(result, dict(cpu_percent=None, memory_bytes=None, process_identity=None))
        self.write_proc(ticks=300, start=60)
        self.now += 1
        self.assertIsNone(self.backend._resources('42')['cpu_percent'])
        self.assertIsNone(self.backend._resources('0')['process_identity'])

    def test_topology_and_allocation_do_not_determine_cpu_percent(self):
        with patch('session_search.tui_backend.core_groups', return_value=[[0, 2], [1, 3]]), patch('session_search.tui_backend.read_cores', return_value=1):
            result = self.backend._host_cpu()
            self.assertEqual(result['cpu_total'], 2)
            self.assertEqual(result['cpu_configured'], 1)
            self.assertNotIn('cpu_average', result)
            self.write_proc()
            self.backend._resources('42')
            self.now += 2
            # Other programs fully occupy the machine; this process is idle.
            (self.root/'stat').write_text(''.join(f'cpu{i} 10000 0 0 0 0 0 0 0\n' for i in range(8)))
            self.assertEqual(self.backend._resources('42')['cpu_percent'], 0)

    def test_cpu_hotplug_invalidates_interval(self):
        self.write_proc()
        self.backend._resources('42')
        self.now += 1
        self.write_proc(ticks=200)
        (self.root/'stat').write_text('cpu0 0 0 0 0 0 0 0 0\n')
        self.assertIsNone(self.backend._resources('42')['cpu_percent'])
        self.now += 1
        self.write_proc(ticks=300)
        self.assertEqual(self.backend._resources('42')['cpu_percent'], 100)

    def test_mcp_counts_only_matching_processes(self):
        (self.process / 'cmdline').write_bytes(b'python\0-m\0session_search.server\0')
        self.assertEqual(self.backend._mcp_processes(), 1)
        (self.process / 'cmdline').write_bytes(b'python\0-c\0session_search.server\0')
        self.assertEqual(self.backend._mcp_processes(), 0)
        (self.process / 'cmdline').write_bytes(b'python\0/path/session_search/server.py\0')
        self.assertEqual(self.backend._mcp_processes(), 1)

    def test_core_setting_saved_and_only_running_service_restarted(self):
        self.backend.cpu_config = self.root / 'cpu.json'
        self.backend.systemd_root = self.root / 'systemd'
        with patch('session_search.tui_backend.core_groups', return_value=[[0, 2], [1, 3]]), patch.object(self.backend, '_run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertTrue(self.backend.set_cores(2)['ok'])
            self.assertEqual(json.loads(self.backend.cpu_config.read_text()), {'cores': 2})
            self.assertEqual(run.call_args_list[-1].args[0], ['systemctl', '--user', 'try-restart', UNIT])
            self.assertIn('CPUQuota=\n', (self.backend.systemd_root / (UNIT+'.d') / '90-session-search-cpu.conf').read_text())
            self.assertFalse(self.backend.set_cores(3)['ok'])
            self.assertEqual(run.call_count, 2)

    def test_missing_rss_and_counter_regression(self):
        self.write_proc(ticks=200)
        self.backend._resources('42')
        self.now += 1
        self.write_proc(ticks=100)
        (self.process / 'statm').unlink()
        result = self.backend._resources('42')
        self.assertIsNone(result['cpu_percent'])
        self.assertIsNone(result['memory_bytes'])
        self.assertEqual(result['process_identity'], '42:50')
