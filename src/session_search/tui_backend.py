"""Bounded local diagnostics and explicit user service actions for the TUI."""
import json
import os
from pathlib import Path
import subprocess
import tomllib
import time
from . import indexer, server
from .embedding import DEFAULT_MODEL_PATH

UNIT = 'codex-session-index.service'

class Backend:
    def __init__(self, codex_home=None, model_path=None, *, proc_root="/proc", clock=None):
        self.home = Path(codex_home or os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
        self.model_path = Path(model_path or DEFAULT_MODEL_PATH)
        self.proc_root = Path(proc_root)
        self.clock = clock or time.monotonic
        self._cpu_sample = None
        self._ticks_per_second = os.sysconf('SC_CLK_TCK')
        self._page_size = os.sysconf('SC_PAGE_SIZE')

    def _resources(self, pid):
        result = dict(cpu_percent=None, memory_bytes=None, process_identity=None)
        try:
            pid = int(pid)
            if pid <= 0:
                raise ValueError('No running process')
            directory = self.proc_root / str(pid)
            # comm may contain spaces and parentheses; fields begin after its final ')'.
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            identity = f'{pid}:{int(fields[19])}'
            now = self.clock()
        except (OSError, ValueError, TypeError, IndexError):
            self._cpu_sample = None
            return result
        result['process_identity'] = identity
        previous = self._cpu_sample
        if previous and previous[0] == identity:
            elapsed = now - previous[2]
            delta = ticks - previous[1]
            if elapsed > 0 and delta >= 0:
                result['cpu_percent'] = 100.0 * delta / self._ticks_per_second / elapsed
        self._cpu_sample = (identity, ticks, now)
        try:
            resident_pages = int((directory / 'statm').read_text().split()[1])
            if resident_pages >= 0:
                result['memory_bytes'] = resident_pages * self._page_size
        except (OSError, ValueError, IndexError):
            pass
        return result

    def _run(self, args, **kwargs):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=10, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(args, 1, '', str(exc))

    def _configured(self):
        try:
            config = tomllib.loads((self.home / 'config.toml').read_text())
            for entry in config.get('mcp_servers', {}).values():
                if not isinstance(entry, dict):
                    continue
                command = entry.get('command', '')
                args = entry.get('args', [])
                if (Path(str(command)).name == 'session-search' and 'mcp' in args) or ('session_search.server' in args):
                    return True
            return False
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError):
            return None

    def status(self):
        unit = self._run(['systemctl', '--user', 'show', UNIT, '--property=ActiveState,SubState,MainPID,UnitFileState'])
        props = dict(line.split('=', 1) for line in unit.stdout.splitlines() if '=' in line)
        package = self._run(['dpkg-query', '-W', '-f=${Status}\n${Version}', 'session-search'])
        files = ('config.json', 'model.safetensors', 'tokenizer.json', 'tokenizer_config.json', 'modules.json')
        missing = [name for name in files if not (self.model_path / name).is_file()]
        result = dict(version=package.stdout.splitlines()[-1] if package.returncode == 0 and package.stdout.startswith('install ok installed\n') else None,
                      model='available' if not missing else 'missing: '+', '.join(missing),
                      service=props.get('ActiveState'), pid=props.get('MainPID'),
                      autostart=props.get('UnitFileState'), mcp_configured=self._configured(),
                      sessions=None, chunks=None, vectors=None, pending=None, errors=None,
                      index_pending=None, operation='unknown', session_id=None, path=None, operation_started_at=None, index_available=False, last_progress=None, index_error=None)
        result.update(self._resources(result['pid']))
        if unit.returncode:
            result['service_error'] = unit.stderr.strip()
        try:
            snapshot = indexer.request_index({'op': 'status', 'codex_home': str(self.home.resolve())}, timeout=2)
            if not snapshot.get('ok'):
                raise ValueError(snapshot.get('error', 'Indexer status failed'))
            result['index_available'] = True
            result['index_pending'] = snapshot.get('index_pending')
            stats = snapshot.get('stats', {})
            result.update({key: stats.get(key) for key in ('sessions', 'chunks', 'vectors')})
            jobs = stats.get('jobs')
            result['pending'] = sum(jobs.get(key, 0) for key in ('pending', 'queued', 'retry', 'running')) if isinstance(jobs, dict) else None
            result['errors'] = snapshot.get('errors')
            result['jobs'] = jobs
            if 'activity' in snapshot:
                activity = snapshot['activity']
                result['operation'] = activity.get('operation', 'unknown') if activity else 'idle'
                if activity:
                    result['session_id'] = activity.get('session_id')
                    result['path'] = activity.get('path')
                    result['operation_started_at'] = activity.get('started_at')
            result['last_progress'] = snapshot.get('last_completed_at')
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            result['index_error'] = str(exc)
        return result

    def search(self, query, state='all'):
        content = server.tool_session_search({'query': query, 'state': state, 'codex_home': str(self.home), 'include_snippets': True})
        return json.loads(content[0]['text'])

    def journal(self):
        result = self._run(['journalctl', '--user', '-u', UNIT, '-n', '100', '--no-pager', '-o', 'short-iso'])
        return (result.stdout if result.returncode == 0 else result.stderr)[-30000:]

    def action(self, name):
        if name not in {'start', 'stop', 'restart', 'enable', 'disable'}:
            raise ValueError('Unknown service action')
        result = self._run(['systemctl', '--user', name, UNIT])
        return {'ok': result.returncode == 0, 'output': (result.stdout + result.stderr).strip()}

    def check_mcp(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'session-search-tui', 'version': '1'}}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        result = self._run(['/usr/bin/session-search', 'mcp'], input=''.join(json.dumps(request)+'\n' for request in requests))
        try:
            responses = {item.get('id'): item for item in map(json.loads, result.stdout.splitlines())}
            initialized = responses[1]['result']['serverInfo']['name']
            names = {tool['name'] for tool in responses[2]['result']['tools']}
            ok = result.returncode == 0 and {'session_search', 'session_info', 'session_resume_command', 'index_status'} <= names
            return {'ok': ok, 'output': f'{initialized}: '+', '.join(sorted(names))}
        except (ValueError, KeyError, TypeError, AttributeError):
            return {'ok': False, 'output': 'Installed MCP did not complete initialize/tools/list. '+result.stderr[-2000:]}

    def connection_config(self):
        return '[mcp_servers.session_search]\ncommand = "/usr/bin/session-search"\nargs = ["mcp"]\n'
