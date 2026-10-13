import unittest
from unittest.mock import patch
from textual.widgets import Sparkline, Static
from session_search.tui import SessionSearchApp, main

class Backend:
    def __init__(self):
        self.snapshot = dict(service='active', index_available=True, index_pending=True, sessions=0,
                             chunks=3, vectors=2, pending=1, cpu_percent=0.0,
                             memory_bytes=0, process_identity='123:1')
        self.calls = []
    def status(self):
        return dict(self.snapshot)
    def action(self, name):
        self.calls.append(name)
        self.snapshot['service'] = 'inactive' if name == 'stop' else 'active'
        return {'ok': True}

class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
    def text(self, app, name):
        return str(app.query_one('#' + name, Static).render())

    async def test_keyboard_toggle_and_exit_leaves_service(self):
        backend = Backend()
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            self.assertIn('RUNNING (processing)', self.text(app, 'state'))
            await pilot.press('s')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop'])
            self.assertIn('STOPPED', self.text(app, 'state'))
            self.assertFalse(app.query_one('#cpu-row').display)
            await pilot.press('s')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop', 'start'])
            await pilot.press('q')
        self.assertEqual(backend.calls, ['stop', 'start'])

    async def test_arrow_selection_enter_and_visible_commands(self):
        backend = Backend()
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(70, 12)) as pilot:
            await self.settle(app, pilot)
            for name in ('state', 'cpu-row', 'ram-row', 'sessions', 'fragments', 'indexed', 'pending', 'toggle', 'quit'):
                widget = app.query_one('#' + name)
                self.assertGreater(widget.region.height, 0, name)
                self.assertLessEqual(widget.region.bottom, app._get_inline_height(), name)
            await pilot.press('down')
            self.assertEqual(self.text(app, 'quit'), '> Quit')
            self.assertEqual(backend.calls, [])
            await pilot.press('up', 'enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop'])
            self.assertEqual(self.text(app, 'toggle'), '> Start indexing')
            await pilot.press('enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop', 'start'])
            await pilot.press('down', 'enter')
        self.assertEqual(backend.calls, ['stop', 'start'])

    async def test_idle_errors_missing_metrics_and_zero(self):
        backend = Backend()
        backend.snapshot['index_pending'] = False
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(70, 11)) as pilot:
            await self.settle(app, pilot)
            self.assertIn('IDLE (awaiting)', self.text(app, 'state'))
            self.assertIn('CPU: 0.0%', self.text(app, 'cpu-value'))
            self.assertIn('RAM: 0.0 GiB', self.text(app, 'ram-value'))
            self.assertIn('Sessions  0', self.text(app, 'sessions'))
            snapshot = dict(app.last_status, cpu_percent=None, memory_bytes=None, sessions=None, index_available=False)
            app.render_status(snapshot)
            self.assertIn('ERROR', self.text(app, 'state'))
            self.assertIn('N/A', self.text(app, 'cpu-value'))
            self.assertIn('N/A', self.text(app, 'sessions'))
            self.assertEqual(app.query_one('#cpu-chart', Sparkline).styles.visibility, 'hidden')
            app.render_status(dict(snapshot, service='active', index_available=True, index_pending=None))
            self.assertIn('ERROR', self.text(app, 'state'))
            app.render_status(dict(snapshot, service='failed'))
            self.assertIn('ERROR service failed', self.text(app, 'state'))
            app.render_status(dict(snapshot, service=None))
            await pilot.press('s')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, [])

    async def test_histories_restart_and_minimal_controls(self):
        app = SessionSearchApp(Backend(), refresh_interval=0)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.render_status(dict(app.last_status, cpu_percent=20, memory_bytes=1024))
            self.assertEqual(list(app.histories['cpu']), [0, 20])
            app.render_status(dict(app.last_status, process_identity='123:2', cpu_percent=None))
            self.assertEqual(list(app.histories['cpu']), [])
            self.assertEqual(list(app.histories['ram']), [1024])
            self.assertEqual(self.text(app, 'toggle'), '> Stop indexing')
            self.assertEqual(self.text(app, 'quit'), '  Quit')
            self.assertEqual(len(app.query(Sparkline)), 2)
            self.assertLessEqual(app._get_inline_height(), 12)

    async def test_poll_failure_recovers_and_index_errors_reported(self):
        backend = Backend()
        app = SessionSearchApp(backend, refresh_interval=0)
        with patch.object(backend, 'status', side_effect=[RuntimeError('socket failed'), dict(backend.snapshot)]) as status:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.settle(app, pilot)
                self.assertIn('ERROR socket failed', self.text(app, 'state'))
                app.refresh_state()
                await self.settle(app, pilot)
                self.assertIn('RUNNING', self.text(app, 'state'))
                app.render_status(dict(app.last_status, errors={'failed': 1}))
                self.assertIn('ERROR index errors', self.text(app, 'state'))

    def test_main_uses_installed_inline_api(self):
        with patch('session_search.tui.SessionSearchApp') as app:
            main()
            app.return_value.run.assert_called_once_with(inline=True, mouse=False)

if __name__ == '__main__':
    unittest.main()
