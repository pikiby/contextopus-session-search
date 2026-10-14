"""Inline keyboard dashboard matching the user-approved text mockup."""
import asyncio
import time
from collections import deque
from .charts import bar, history, estimated_time
try:
    from textual import work
except ModuleNotFoundError as exc:
    if exc.name != 'textual':
        raise
    raise SystemExit('TUI requires Textual 8.2.8; dependency installation requires separate approval.') from exc
from rich.text import Text
from rich.style import Style
from textual.app import App
from textual.containers import Grid, Vertical
from textual.widgets import Static

COLORS = {'cpu': 'bright_cyan', 'ram': 'bright_green', 'speed': 'bright_yellow'}


def number(value):
    return '—' if value is None else f'{value:,}'


def indicator(value, color):
    text = Text(bar(value) if value is not None else '—', style=Style.parse(color))
    if value is not None:
        text.append(f' {value:.1f}%', style=Style.parse('default'))
    return text


class SessionSearchApp(App):
    BINDINGS = [('up', 'select_previous', ''), ('down', 'select_next', ''),
                ('left', 'fewer_cores', ''), ('right', 'more_cores', ''), ('enter', 'activate', ''), ('s', 'toggle_service', ''), ('q', 'quit', '')]
    ENABLE_COMMAND_PALETTE = False
    CSS = '''
    Screen { height: auto; padding: 0; background: ansi_default; color: ansi_default; }
    * { border: none; background: transparent; }
    #dashboard { width: 100%; max-width: 79; height: auto; }
    Static { height: 1; }
    .header { height: auto; padding: 0 1; grid-size: 2; grid-columns: 25 1fr; grid-rows: auto; }
    #title { text-style: bold; }
    #state { height: auto; text-align: right; }
    .header.narrow { grid-size: 1; grid-columns: 1fr; }
    .header.narrow #state { text-align: left; }
    #resources { grid-size: 3; grid-columns: 25; grid-rows: auto; grid-gutter: 1 1; }
    .column { height: auto; width: 25; }
    .history { height: 5; margin-top: 1; }
    .group { margin-top: 1; padding: 0 1; height: auto; }
    .heading { text-style: bold; }
    .stats { height: auto; grid-size: 2; grid-columns: 36; grid-rows: auto; grid-gutter: 1 2; }
    .stats-column { height: auto; width: 36; }
    #counts { margin-top: 1; }
    #actions { padding: 0; }
    #hints { height: auto; margin-top: 1; padding: 0 1; color: ansi_default; text-style: dim; }
    '''

    def __init__(self, backend=None, *, refresh_interval=3):
        super().__init__(ansi_color=True)
        self.theme = "ansi-dark"
        if backend is None:
            from .tui_backend import Backend
            backend = Backend()
        self.backend = backend
        self.refresh_interval = refresh_interval
        self.last_status = {}
        self.histories = {key: deque(maxlen=240) for key in ('cpu', 'ram', 'speed')}
        self._busy = False
        self._requested = None
        self._action_error = None
        self._selected = 0
        self._core_choice = None

    def compose(self):
        with Vertical(id="dashboard"):
            yield from self._compose_dashboard()

    def _compose_dashboard(self):
        with Grid(classes='header', id='header'):
            yield Static('SESSION SEARCH', id='title', markup=False)
            yield Static('Indexing unavailable', id='state', markup=False)
        with Grid(classes='group', id='resources'):
            for key, label, detail in [('cpu', 'CPU', 'cores'), ('ram', 'RAM', 'memory'), ('speed', 'Embeddings', 'embedding-average')]:
                with Vertical(classes='column', id=key+'-row'):
                    yield Static(label, classes='heading', markup=False)
                    yield Static('—', id=key+'-value', markup=False)
                    yield Static('—', id=detail, markup=False)
                    yield Static('', id=key+'-chart', classes='history', markup=False)
        with Vertical(classes='group', id='indexing'):
            yield Static('INDEXING', classes='heading', markup=False)
            yield Static('—', id='progress', markup=False)
            with Grid(id='counts', classes='stats'):
                for names in [('indexed', 'remaining', 'eta'), ('sessions', 'database', 'model')]:
                    with Vertical(classes='stats-column'):
                        for name in names:
                            yield Static('—', id=name, markup=False)
        with Vertical(classes='group', id='search'):
            yield Static('SEARCH · LAST 24 HOURS', classes='heading', markup=False)
            with Grid(id='search-counts', classes='stats'):
                for names in [('requests', 'empty'), ('errors', 'search-average')]:
                    with Vertical(classes='stats-column'):
                        for name in names:
                            yield Static('—', id=name, markup=False)
            yield Static('MCP             — · stdio', id='mcp', markup=False)
        with Vertical(classes='group', id='actions'):
            yield Static('ACTIONS', classes='heading', markup=False)
            for name in ('toggle', 'core-choice', 'index-autostart', 'quit'):
                yield Static('', id=name, markup=False)
        yield Static('↑↓ Select   ←→ Change   Enter Apply   Q Quit', id='hints', markup=False)

    def on_resize(self, event):
        # Move complete metric/statistic groups, preserving their internal columns.
        width = max(1, min(79, event.size.width - self.screen.scrollbar_size_vertical) - 2)
        self.query_one('#resources').styles.grid_size_columns = max(1, min(3, (width + 1) // 26))
        for name in ('counts', 'search-counts'):
            self.query_one('#' + name).styles.grid_size_columns = 2 if width >= 74 else 1
        self.query_one('#header').set_class(width < 50, 'narrow')

    def on_mount(self):
        self.refresh_state()
        if self.refresh_interval > 0:
            self.set_interval(self.refresh_interval, self.refresh_state)

    def render_status(self, status):
        self.last_status = status
        self.render_commands()
        service = status.get('service')
        active = service == 'active'
        reason = self._action_error or status.get('service_error')
        if not reason and status.get('errors'):
            reason = 'index errors: ' + str(status['errors'])
        if reason:
            state = 'error: ' + str(reason).splitlines()[0][:36]
        elif active:
            if status.get('index_available') is not True or not isinstance(status.get('index_pending'), bool):
                state = 'error: ' + str(status.get('index_error') or 'status unavailable').splitlines()[0][:36]
            else:
                state = 'running' if status['index_pending'] else 'idle'
        elif service == 'inactive':
            state = 'stopped'
        elif service == 'failed':
            state = 'error: service failed'
        else:
            state = 'unavailable'
        self.query_one('#state', Static).update(Text('Indexing '+state, style=Style.parse('bright_red' if state.startswith('error') else 'bright_green' if active else 'bright_yellow')))
        allocated = status.get('cpu_cores') if active else 0 if service == 'inactive' else None
        self.query_one('#cores', Static).update(f'{number(allocated)} of {number(status.get("cpu_total"))} cores')
        now = time.monotonic()
        memory, memory_total = status.get('memory_bytes'), status.get('memory_total')
        ram_percent = 100*memory/memory_total if memory is not None and memory_total else None
        metrics = status.get('embedding_metrics') or {}
        rate = metrics.get('per_minute') if active and status.get('index_available') else None
        for key, value in [('cpu', status.get('cpu_percent')), ('ram', ram_percent), ('speed', rate)]:
            value = value if active else None
            self.histories[key].append((now, value))
            while self.histories[key] and self.histories[key][0][0] < now-120:
                self.histories[key].popleft()
            content = indicator(value, COLORS[key]) if key != 'speed' else Text(number(value)+'/min' if value is not None else '—', style=Style.parse(COLORS[key]))
            self.query_one('#'+key+'-value', Static).update(content)
            chart = self.query_one('#'+key+'-chart', Static)
            text = Text(history(self.histories[key], now, width=16, unit='' if key == 'speed' else '%'), style=Style.parse(COLORS[key]))
            offset = 0
            for line in text.plain.splitlines(keepends=True):
                text.stylize(Style.parse('dim'), offset, offset+min(5, len(line)))
                offset += len(line)
            chart.update(text)
        avg = metrics.get('average_seconds') if active else None
        self.query_one('#embedding-average', Static).update(f'{avg:.2f} s/item' if avg is not None else '— s/item')
        self.query_one('#memory', Static).update(f'{memory/1024**3:.2f} GiB' if active and memory is not None else '— GiB')
        total, indexed = status.get('chunks'), status.get('vectors')
        remaining = max(0, total-indexed) if total is not None and indexed is not None else None
        progress = min(100, 100*indexed/total) if total and indexed is not None else 0 if total == 0 and indexed == 0 else None
        self.query_one('#progress', Static).update(indicator(progress, 'bright_green'))
        values = {'indexed': ('Indexed', f'{number(indexed)} / {number(total)}'),
                  'remaining': ('Remaining', number(remaining)),
                  'sessions': ('Sessions', number(status.get('sessions'))),
                  'eta': ('Time remaining', estimated_time(remaining, rate, metrics.get('observed_seconds'), active=active)),
                  'database': ('Database size', f'{status["database_bytes"]/1024**3:.2f} GiB' if status.get('database_bytes') is not None else '—'),
                  'model': ('Model', str(status.get('model_name') or '—').removeprefix('Qwen/'))}
        for key,(label,value) in values.items():
            self.query_one('#'+key, Static).update(f'{label:16}{value}')
        processes = status.get('mcp_processes')
        mcp = '—' if processes is None else f'Running ({processes})' if processes else 'Not running'
        self.query_one('#mcp', Static).update(f'{"MCP":16}'+mcp+' · stdio')
        metrics = status.get('search_metrics') or {}
        for widget,label,key in [('requests','Requests','calls'),('errors','Errors','errors'),('empty','Empty results','empty')]:
            self.query_one('#'+widget, Static).update(f'{label:16}'+number(metrics.get(key)))
        avg = metrics.get('average_seconds')
        self.query_one('#search-average', Static).update(f'{"Average time":16}'+(f'{avg:.2f} s' if avg is not None else '—'))

    def render_commands(self):
        service = self.last_status.get('service')
        label = 'Stop indexing' if service == 'active' else 'Start indexing'
        if service not in {'active', 'inactive', 'failed'}:
            label = 'Start/stop unavailable'
        if self._requested is not None:
            label = 'Please wait…'
        if self._core_choice is None:
            self._core_choice = self.last_status.get('cpu_configured')
        choice = number(self._core_choice)
        core_label = f'Allocated cores ◀ {choice} ▶'
        if self._requested is not None:
            core_label = 'Allocated cores Applying…'
        autostart = self.last_status.get('autostart')
        autostart = {'enabled':'Enabled','enabled-runtime':'Enabled (runtime)','disabled':'Disabled'}.get(autostart,'—')
        commands = [('toggle',label),('core-choice',core_label),('index-autostart',f'Indexer autostart {autostart}'),('quit','Quit')]
        for index,(name,text) in enumerate(commands):
            selected = index == self._selected
            text = ('▶ ' if selected else '  ') + text
            self.query_one('#'+name, Static).update(Text(text, style=Style.parse('bold bright_cyan' if selected else 'default')))

    def action_select_previous(self):
        self._selected = (self._selected - 1) % 4
        self.render_commands()
        self._scroll_selection()

    def action_select_next(self):
        self._selected = (self._selected + 1) % 4
        self.render_commands()
        self._scroll_selection()

    def _scroll_selection(self):
        names = ('toggle', 'core-choice', 'index-autostart', 'quit')
        self.query_one('#' + names[self._selected]).scroll_visible(animate=False)

    def action_activate(self):
        if self._selected == 0:
            self.action_toggle_service()
        elif self._selected == 1:
            if self._requested is None and self._core_choice is not None:
                self._requested = ('cores', self._core_choice)
                self.render_commands()
                self.refresh_state()
        elif self._selected == 2:
            autostart = self.last_status.get('autostart')
            if self._requested is None and autostart in {'enabled', 'disabled', 'enabled-runtime'}:
                self._requested = 'disable' if autostart.startswith('enabled') else 'enable'
                self.refresh_state()
        else:
            self.exit()

    def change_cores(self, delta):
        available = self.last_status.get('cpu_available')
        if self._selected == 1 and available and self._requested is None:
            self._core_choice = max(1, min(available, (self._core_choice or 1) + delta))
            self.render_commands()

    def action_fewer_cores(self):
        self.change_cores(-1)

    def action_more_cores(self):
        self.change_cores(1)

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
                    if isinstance(action, tuple):
                        result = await asyncio.to_thread(self.backend.set_cores, action[1])
                    else:
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
