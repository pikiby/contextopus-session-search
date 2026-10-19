import os
import unittest
from unittest.mock import patch
from textual.widgets import Sparkline, Static
from session_search.tui import SessionSearchApp, main

class Backend:
    def __init__(self):
        self.snapshot = dict(service='active', index_available=True, index_pending=True, sessions=0,
                             chunks=3, vectors=2, pending=1, cpu_percent=0.0, memory_total=1024**3,
                             memory_bytes=0, autostart='enabled', mcp_processes=0, process_identity='123:1', cpu_configured=2, cpu_available=8)
        self.calls = []
    def set_cores(self, count):
        self.calls.append(('cores', count))
        self.snapshot['cpu_configured'] = count
        return {'ok': True}
    def set_selection(self, state, enabled):
        self.calls.append(('selection', state, enabled))
        selection = dict(self.snapshot.get('session_selection') or {'active': True, 'archived': False})
        selection[state] = enabled
        self.snapshot['session_selection'] = selection
        return {'ok': True}
    def status(self):
        return dict(self.snapshot)
    def action(self, name):
        self.calls.append(name)
        if name in {'enable', 'disable'}:
            self.snapshot['autostart'] = 'enabled' if name == 'enable' else 'disabled'
        else:
            self.snapshot['service'] = 'inactive' if name == 'stop' else 'active'
        return {'ok': True}

class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("NO_COLOR", None)

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
            self.assertIn('Indexing running', self.text(app, 'state'))
            await pilot.press('s')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop'])
            self.assertIn('Indexing stopped', self.text(app, 'state'))
            self.assertIn('—', self.text(app, 'speed-value'))
            await pilot.press('s')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop', 'start'])
            await pilot.press('q')
        self.assertEqual(backend.calls, ['stop', 'start'])

    async def test_manual_indexer_renders_resources_and_toggle_does_not_control_service(self):
        backend = Backend()
        backend.snapshot.update(service='inactive', pid='123', service_pid='0',
                                indexer_running=True, indexer_managed=False,
                                cpu_percent=25.0, memory_bytes=1024**3, cpu_cores=2)
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(80, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertIn('Indexing running', self.text(app, 'state'))
            self.assertEqual(set(app.histories), {'speed'})
            self.assertNotIn('Start indexing', self.text(app, 'toggle'))
            await pilot.press('s')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, [])
            self.assertIn('Ctrl+C', self.text(app, 'state'))
            app._action_error = None
            app.render_status(dict(backend.snapshot, index_pending=False))
            self.assertIn('Indexing idle', self.text(app, 'state'))
            await pilot.press('enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, [])

    async def test_arrow_selection_enter_and_visible_commands(self):
        backend = Backend()
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            for name in ('state', 'speed-value', 'sessions', 'remaining', 'indexed', 'progress', 'toggle', 'quit'):
                widget = app.query_one('#' + name)
                self.assertGreater(widget.region.height, 0, name)
                self.assertTrue(widget.display, name)
            await pilot.press('down', 'down', 'down', 'down')
            self.assertEqual(self.text(app, 'quit'), '▶ Quit')
            self.assertEqual(backend.calls, [])
            await pilot.press('up', 'up', 'up', 'up', 'enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop'])
            self.assertEqual(self.text(app, 'toggle'), '▶ Start indexing')
            await pilot.press('enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['stop', 'start'])
            await pilot.press('down', 'down', 'down', 'down', 'enter')
        self.assertEqual(backend.calls, ['stop', 'start'])

    async def test_autostart_independent_from_running_and_mcp_display(self):
        backend = Backend()
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            self.assertIn('Not running', self.text(app, 'mcp'))
            await pilot.press('down', 'enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['disable'])
            self.assertEqual(backend.snapshot['service'], 'active')
            self.assertIn('Disabled', self.text(app, 'index-autostart'))
            await pilot.press('enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, ['disable', 'enable'])
            app.render_status(dict(app.last_status, mcp_processes=2))
            self.assertIn('Running (2)', self.text(app, 'mcp'))
            await pilot.press('down', 'down', 'down')
            await pilot.pause()
            self.assertLessEqual(app.query_one('#quit').region.bottom, 24)
            self.assertEqual(len(app.query('#speed-chart')), 1)

    async def test_session_selection_works_while_stopped(self):
        backend = Backend()
        backend.snapshot.update(service='inactive', chunks=5, vectors=3, sessions=2, search_metrics={'calls': 7})
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(80, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertIn('3 / 5', self.text(app, 'indexed'))
            self.assertIn('Sessions        2', self.text(app, 'sessions'))
            self.assertIn('Requests        7', self.text(app, 'requests'))
            self.assertIn('N/A', self.text(app, 'eta'))
            self.assertIn('Active sessions Enabled', self.text(app, 'active-sessions'))
            self.assertIn('Archived sessions Disabled', self.text(app, 'archived-sessions'))
            await pilot.press('down', 'down', 'enter')
            await self.settle(app, pilot)
            await pilot.press('down', 'enter')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, [('selection', 'active', False), ('selection', 'archived', True)])
            self.assertEqual(backend.snapshot['service'], 'inactive')
            self.assertIn('Active sessions Disabled', self.text(app, 'active-sessions'))
            self.assertIn('Archived sessions Enabled', self.text(app, 'archived-sessions'))

    async def test_resource_controls_are_absent(self):
        backend = Backend()
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(80, 40)) as pilot:
            await self.settle(app, pilot)
            for name in ('cpu-row', 'ram-row', 'cpu-chart', 'ram-chart', 'cores', 'memory', 'core-choice'):
                self.assertEqual(len(app.query('#' + name)), 0, name)
            await pilot.press('down', 'right', 'left')
            self.assertEqual(backend.calls, [])
            self.assertEqual(self.text(app, 'index-autostart'), '▶ Indexer autostart Enabled')

    async def test_idle_errors_missing_metrics_and_zero(self):
        backend = Backend()
        backend.snapshot['index_pending'] = False
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(70, 11)) as pilot:
            await self.settle(app, pilot)
            self.assertIn('Indexing idle', self.text(app, 'state'))
            self.assertIn('Sessions        0', self.text(app, 'sessions'))
            app.render_status(dict(app.last_status, cpu_total=16, cpu_cores=4, cpu_percent=25.0))
            snapshot = dict(app.last_status, cpu_percent=None, memory_bytes=None, sessions=None, index_available=False)
            app.render_status(snapshot)
            self.assertIn('error', self.text(app, 'state'))
            self.assertIn('—', self.text(app, 'speed-value'))
            self.assertIn('—', self.text(app, 'sessions'))
            self.assertNotIn('█', self.text(app, 'speed-chart').splitlines()[0])
            app.render_status(dict(snapshot, service='active', index_available=True, index_pending=None))
            self.assertIn('error', self.text(app, 'state'))
            app.render_status(dict(snapshot, service='failed'))
            self.assertIn('error: service failed', self.text(app, 'state'))
            app.render_status(dict(snapshot, service=None))
            await pilot.press('s')
            await self.settle(app, pilot)
            self.assertEqual(backend.calls, [])

    async def test_histories_restart_and_minimal_controls(self):
        app = SessionSearchApp(Backend(), refresh_interval=0)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.settle(app, pilot)
            app.render_status(dict(app.last_status, cpu_percent=20, memory_bytes=1024))
            self.assertEqual(set(app.histories), {'speed'})
            app.render_status(dict(app.last_status, process_identity='123:2', cpu_percent=None))
            self.assertIsNone(app.histories['speed'][-1][1])
            self.assertEqual(self.text(app, 'toggle'), '▶ Stop indexing')
            self.assertEqual(self.text(app, 'quit'), '  Quit')
            self.assertEqual(len(app.query(Sparkline)), 0)
            self.assertLessEqual(app._get_inline_height(), 35)

    async def test_poll_failure_recovers_and_index_errors_reported(self):
        backend = Backend()
        app = SessionSearchApp(backend, refresh_interval=0)
        with patch.object(backend, 'status', side_effect=[RuntimeError('socket failed'), dict(backend.snapshot)]) as status:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.settle(app, pilot)
                self.assertIn('error: socket failed', self.text(app, 'state'))
                app.refresh_state()
                await self.settle(app, pilot)
                self.assertIn('Indexing running', self.text(app, 'state'))
                app.render_status(dict(app.last_status, errors={'failed': 1}))
                self.assertIn('error: index errors', self.text(app, 'state'))

    async def test_progress_eta_color_and_speed_history(self):
        backend = Backend()
        backend.snapshot.update(chunks=585763, vectors=32568, pending=553195,
                                embedding_metrics=dict(per_minute=150, average_seconds=.4, observed_seconds=60))
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(80, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertIn('5.6%', self.text(app, 'progress'))
            self.assertIn('553,195', self.text(app, 'remaining'))
            self.assertIn('32,568 / 585,763', self.text(app, 'indexed'))
            self.assertNotIn('94.4%', self.text(app, 'remaining'))
            self.assertIn('~', self.text(app, 'eta'))
            self.assertEqual(app.histories['speed'][-1][1], 150)
            self.assertIn('200', self.text(app, 'speed-chart'))
            self.assertTrue(app.query_one('#speed-value').render().spans)
            self.assertLess(app.query_one('#speed-chart').region.bottom, app.query_one('#mcp').region.y)
            app.render_status(dict(app.last_status, chunks=0, vectors=0))
            self.assertIn('0.0%', self.text(app, 'progress'))

    async def test_user_mock_columns_sections_and_units(self):
        backend = Backend()
        backend.snapshot.update(cpu_cores=8, cpu_total=16, cpu_percent=23, memory_bytes=int(2.81*1024**3),
            memory_total=32*1024**3, database_bytes=int(1.13*1024**3), model_name='Qwen/Qwen3-Embedding-0.6B',
            search_metrics=dict(calls=0,errors=0,empty=0,average_seconds=None),
            embedding_metrics=dict(per_minute=117,average_seconds=.25,observed_seconds=30))
        app = SessionSearchApp(backend, refresh_interval=0)
        async with app.run_test(size=(84,40)) as pilot:
            await self.settle(app,pilot)
            self.assertEqual(self.text(app,'title'),'SESSION SEARCH')
            self.assertEqual(self.text(app,'speed-value'),'117/min')
            self.assertEqual(self.text(app,'embedding-average'),'0.25 s/item')
            self.assertIn('1.13 GiB',self.text(app,'database'))
            self.assertIn('Qwen3-Embedding-0.6B',self.text(app,'model'))
            self.assertIn('Calculating…',self.text(app,'eta'))
            self.assertEqual(len(app.query('#speed-chart')), 1)
            groups=[app.query_one('#'+k).region for k in ('indexing','search','actions')]
            self.assertTrue(all(a.bottom < b.y for a,b in zip(groups,groups[1:])))
            self.assertEqual(self.text(app,'hints'),'↑↓ Select   Enter Apply   Q Quit')
            self.assertEqual(app.query_one('#indexed').region.x, app.query_one('#remaining').region.x)
            self.assertEqual(app.query_one('#sessions').region.x, app.query_one('#database').region.x)

    async def test_resize_wraps_whole_blocks_without_stretch_or_new_samples(self):
        app = SessionSearchApp(Backend(), refresh_interval=0)
        async with app.run_test(size=(120, 80)) as pilot:
            await self.settle(app, pilot)
            histories = {key: list(values) for key, values in app.histories.items()}
            for width, columns, stats_columns in [(120,3,2),(79,3,2),(78,2,2),(76,2,2),
                                                   (75,2,1),(54,2,1),(52,1,1),(40,1,1),(120,3,2)]:
                await pilot.resize_terminal(width,80)
                await pilot.pause()
                blocks = [app.query_one('#'+key+'-row').region for key in ('speed',)]
                self.assertEqual(len(blocks), 1, width)
                self.assertTrue(all(block.width == 25 for block in blocks), width)
                self.assertEqual(blocks[0].x,1)
                self.assertTrue(all(block.right <= min(width,79)-1 for block in blocks),width)
                for index, block in enumerate(blocks):
                    if index >= columns:
                        self.assertGreater(block.y,blocks[index-columns].bottom)
                left, right = app.query_one('#indexed').region, app.query_one('#sessions').region
                if stats_columns == 2:
                    self.assertEqual(left.y,right.y,width)
                    self.assertEqual(right.x,39,width)
                else:
                    self.assertEqual(left.x,right.x,width)
                    self.assertGreater(right.y,app.query_one('#eta').region.bottom,width)
                self.assertEqual(app.query_one('#actions').region.x,0)
                self.assertEqual(histories,{key:list(values) for key,values in app.histories.items()})
            self.assertEqual(self.text(app,'toggle'),'▶ Stop indexing')
            self.assertEqual(self.text(app,'index-autostart'),'  Indexer autostart Enabled')

    async def test_render_keeps_terminal_background_and_metric_colors(self):
        app = SessionSearchApp(Backend(), refresh_interval=0)
        async with app.run_test(size=(79,80)) as pilot:
            await self.settle(app,pilot)
            segments=[segment for strip in app.screen._compositor.render_strips() for segment in strip]
            backgrounds={str(segment.style.bgcolor) for segment in segments if segment.style and segment.style.bgcolor}
            self.assertEqual(backgrounds,{"Color('default', ColorType.DEFAULT)"})
            colors={segment.style.color.number for segment in segments if segment.style and segment.style.color}
            self.assertTrue({14,10,11} <= colors, colors)

    def test_main_uses_installed_inline_api(self):
        with patch('session_search.tui.SessionSearchApp') as app:
            main()
            app.return_value.run.assert_called_once_with(inline=True, mouse=False)

if __name__ == '__main__':
    unittest.main()
