"""Minimal inline status for the independent indexer."""
import asyncio
from collections import deque
try:
    from textual import work
except ModuleNotFoundError as exc:
    if exc.name != 'textual':
        raise
    raise SystemExit('TUI requires Textual 8.2.8; dependency installation requires separate approval.') from exc
from textual.app import App
from textual.containers import Horizontal
from textual.widgets import Sparkline, Static


class SessionSearchApp(App):
    BINDINGS = [('up', 'select_previous', ''), ('down', 'select_next', ''),
                ('enter', 'activate', ''), ('s', 'toggle_service', ''), ('q', 'quit', '')]
    ENABLE_COMMAND_PALETTE = False
    CSS = '''
    Screen { height: auto; max-width: 70; padding: 0; background: transparent; }
    * { border: none; background: transparent; }
    Static { height: 1; }
    .resource { height: 1; }
    .resource Static { width: 22; }
    Sparkline { width: 20; height: 1; }
    #sessions { margin-top: 1; }
    #toggle { margin-top: 1; }
    '''

    def __init__(self, backend=None, *, refresh_interval=3):
        super().__init__()
        if backend is None:
            from .tui_backend import Backend
            backend = Backend()
        self.backend = backend
        self.refresh_interval = refresh_interval
        self.last_status = {}
        self.histories = {key: deque(maxlen=40) for key in ('cpu', 'ram')}
        self._identity = None
        self._busy = False
        self._requested = None
        self._action_error = None
        self._selected = 0

    def compose(self):
        yield Static('Indexing: ERROR status unavailable', id='state', markup=False)
        for key in ('cpu', 'ram'):
            with Horizontal(classes='resource', id=key + '-row'):
                yield Static('', id=key + '-value', markup=False)
                chart = Sparkline([], id=key + '-chart')
                chart.styles.visibility = 'hidden'
                yield chart
        for name in ('Sessions', 'Fragments', 'Indexed', 'Pending'):
            yield Static(name + ': N/A', id=name.lower(), markup=False)
        yield Static('> Start/stop indexing', id='toggle', markup=False)
        yield Static('  Quit', id='quit', markup=False)

    def on_mount(self):
        self.query_one('#cpu-row').display = False
        self.query_one('#ram-row').display = False
        self.refresh_state()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self.refresh_state)

    def render_status(self, status):
        self.last_status = status
        self.render_commands()
        service = status.get('service')
        reason = self._action_error or status.get('service_error')
        if not reason and status.get('errors'):
            reason = 'index errors: ' + str(status['errors'])
        if reason:
            state = 'ERROR ' + str(reason).splitlines()[0][:45]
        elif service == 'active':
            if status.get('index_available') is not True or not isinstance(status.get('index_pending'), bool):
                state = 'ERROR ' + str(status.get('index_error') or 'index status unavailable').splitlines()[0][:45]
            else:
                state = 'RUNNING (processing)' if status['index_pending'] else 'IDLE (awaiting)'
        elif service == 'inactive':
            state = 'STOPPED'
        elif service == 'failed':
            state = 'ERROR service failed'
        else:
            state = 'ERROR service state unavailable'
        self.query_one('#state', Static).update('Indexing: ' + state)
        identity = status.get('process_identity')
        if identity != self._identity:
            for history in self.histories.values():
                history.clear()
            self._identity = identity
        for key, source in [('cpu', 'cpu_percent'), ('ram', 'memory_bytes')]:
            self.query_one('#' + key + '-row').display = service == 'active'
            value = status.get(source) if service == 'active' else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.histories[key].append(value)
                text = f'{value:.1f}%' if key == 'cpu' else f'{value / 1024**3:.1f} GiB'
            else:
                self.histories[key].clear()
                text = 'N/A'
            self.query_one('#' + key + '-value', Static).update(key.upper() + ': ' + text)
            chart = self.query_one('#' + key + '-chart', Sparkline)
            chart.data = list(self.histories[key])
            chart.styles.visibility = 'visible' if len(chart.data) >= 2 else 'hidden'
        for name, source in [('sessions', 'sessions'), ('fragments', 'chunks'), ('indexed', 'vectors'), ('pending', 'pending')]:
            value = status.get(source)
            self.query_one('#' + name, Static).update(f'{name.capitalize():9} ' + ('N/A' if value is None else f'{value:,}' if isinstance(value, int) else str(value)))

    def render_commands(self):
        service = self.last_status.get('service')
        label = 'Stop indexing' if service == 'active' else 'Start indexing'
        if service not in {'active', 'inactive', 'failed'}:
            label = 'Start/stop indexing (unavailable)'
        if self._requested is not None:
            label = 'Please wait…'
        for index, (name, text) in enumerate((('toggle', label), ('quit', 'Quit'))):
            self.query_one('#' + name, Static).update(('> ' if index == self._selected else '  ') + text)

    def action_select_previous(self):
        self._selected = (self._selected - 1) % 2
        self.render_commands()

    def action_select_next(self):
        self._selected = (self._selected + 1) % 2
        self.render_commands()

    def action_activate(self):
        if self._selected == 0:
            self.action_toggle_service()
        else:
            self.exit()

    def action_toggle_service(self):
        if self._requested is not None:
            return
        service = self.last_status.get('service')
        if service not in {'active', 'inactive', 'failed'}:
            self._action_error = 'service state unavailable'
            self.render_status(self.last_status)
            return
        self._requested = 'stop' if service == 'active' else 'start'
        self.render_commands()
        self.refresh_state()

    @work(group='status', exit_on_error=False)
    async def refresh_state(self):
        if self._busy:
            return
        self._busy = True
        try:
            while True:
                if self._requested is not None:
                    action = self._requested
                    result = await asyncio.to_thread(self.backend.action, action)
                    self._requested = None
                    self._action_error = None if result.get('ok') else result.get('output') or 'service action failed'
                self.render_status(await asyncio.to_thread(self.backend.status))
                if self._requested is None:
                    break
        except Exception as exc:
            self._requested = None
            self.render_status({'service_error': str(exc)})
        finally:
            self._busy = False


def main():
    SessionSearchApp().run(inline=True, mouse=False)


if __name__ == '__main__':
    main()
