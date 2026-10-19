"""Single actor owns model/SQLite; RPC threads enqueue search or read cached status."""
import argparse
import collections
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from .cpu import core_groups, read_cores
import queue
import signal
import socket
import socketserver
import threading
import time


def default_socket():
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime) / 'codex-session-index.sock' if runtime else Path.home() / '.cache/codex-session-index/index.sock'


MAX_RPC_BYTES = 16 * 1024 * 1024


def cleanup_timeout(plan):
    items = plan.get("items", []) if isinstance(plan, dict) else []
    return max(180, 120 * len(items) + 120)


def request_index(payload, socket_path=None, timeout=60):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path or os.environ.get('CODEX_SESSION_INDEX_SOCKET') or default_socket()))
        encoded = json.dumps(payload).encode() + b'\n'
        if len(encoded) > MAX_RPC_BYTES:
            raise ValueError('Request too large; split the selection into smaller previews')
        client.sendall(encoded)
        with client.makefile('rb') as stream:
            raw = stream.readline(MAX_RPC_BYTES + 1)
        if not raw.endswith(b'\n'):
            raise RuntimeError('Incomplete indexer response')
        response = json.loads(raw)
        if not response.get('ok'):
            raise RuntimeError(response.get('error', 'Indexer request failed'))
        return response


def _digest(path, start, length):
    with path.open('rb') as stream:
        stream.seek(start)
        return hashlib.sha256(stream.read(length)).hexdigest()


class DeferredHeader(ValueError):
    """The writer has not finished its first JSONL record yet."""


def _session_id(path):
    # Filename is a locator only: canonical identity always comes from metadata.
    with path.open('rb') as stream:
        for number in range(20):
            raw = stream.readline()
            if not raw.endswith(b'\n'):
                if number == 0:
                    raise DeferredHeader('Session header is not written yet')
                break
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if item.get('type') == 'session_meta':
                value = item.get('payload', {}).get('id')
                if isinstance(value, str) and value:
                    return value
    raise ValueError('session_meta.id missing')


def cpu_topology():
    """Observe physical cores when supported, otherwise label logical/unknown counts.

    This is status metadata only. The module never changes process affinity.
    """
    affinity = getattr(os, 'sched_getaffinity', None)
    if affinity is not None:
        try:
            return len(core_groups(allowed=affinity(0))), 'physical'
        except (OSError, ValueError):
            pass
    count = os.cpu_count()
    return (count, 'logical') if count is not None else (None, 'unknown')


class Indexer:
    def __init__(self, codex_home, store, embedder, *, selection_path=None, block_tokens=512, selection_reader=None, read_bytes=1024 * 1024):
        self.home = Path(codex_home).resolve()
        self.store, self.embedder = store, embedder
        from .settings import read_selection
        self.selection_path = selection_path
        self.selection_reader = selection_reader or (lambda: read_selection(self.selection_path))
        self.selection = self.selection_reader()
        self.read_bytes = read_bytes
        self.block_tokens = block_tokens
        self.events, self.requests = queue.Queue(), queue.Queue()
        self.dirty = collections.OrderedDict()
        self.paths, self.titles, self.errors = {}, {}, {}
        self.stopping = threading.Event()
        self.next_sweep = 0
        self.pid = os.getpid()
        self.cpu_cores, self.cpu_unit = cpu_topology()
        self.started_at = self._timestamp()
        self.embedding_samples = collections.deque()
        self.metrics_started = time.monotonic()
        self.activity = None
        self.last_completed_at = None
        self._status_lock = threading.Lock()
        self._status_snapshot = {}
        self._stats_cache = None
        self._stats_refreshed_at = 0
        self._last_step_worked = False
        self.status(force=True)

    @staticmethod
    def _timestamp():
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _working(self, operation, session_id=None, path=None):
        self.activity = dict(operation=operation, session_id=session_id,
                             path=str(path) if path is not None else None,
                             started_at=self._timestamp())
        self.status(refresh=False)
        try:
            yield
        finally:
            self.activity = None
            self.last_completed_at = self._timestamp()
            self.status(refresh=False)

    def cached_status(self, request=None):
        # Handler threads must never touch SQLite or the model.
        if request and request.get('codex_home') and Path(request['codex_home']).resolve() != self.home:
            raise ValueError('codex_home differs from running indexer')
        with self._status_lock:
            result = copy.deepcopy(self._status_snapshot)
        if not self.events.empty():
            result['index_pending'] = True
        return result

    def mark(self, path):
        self.events.put(str(path))

    def _error(self, path, exc):
        name = type(exc).__name__
        self.errors[str(path)] = name
        logging.error('Index failure path=%s type=%s', path, name)

    def reconcile(self):
        found, successful = {}, True
        known = {item['path']: item['id'] for item in self.store.sessions()
                 if item.get('path') and item.get('id')}
        for name, state in (('sessions', 'active'), ('archived_sessions', 'archived')):
            root = self.home / name
            try:
                self.errors.pop(str(root), None)
                if not root.exists():
                    continue
                def fail(error):
                    raise error
                for directory, _, files in os.walk(root, onerror=fail):
                    for filename in files:
                        if filename.startswith('rollout-') and filename.endswith('.jsonl'):
                            path = Path(directory) / filename
                            try:
                                sid = _session_id(path)
                                found[str(path)] = (sid, state)
                            except DeferredHeader:
                                if str(path) in known:
                                    found[str(path)] = (known[str(path)], state)
                                self.errors.pop(str(path), None)
                            except Exception as exc:
                                successful = False
                                self._error(path, exc)
            except Exception as exc:
                successful = False
                self._error(root, exc)
        try:
            titles = {}
            with (self.home / 'session_index.jsonl').open() as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                        if item.get('id'):
                            titles[item['id']] = item.get('thread_name', item.get('title', ''))
                    except ValueError:
                        continue
            self.titles = titles
        except FileNotFoundError:
            self.titles = {}
        except OSError as exc:
            self._error(self.home / 'session_index.jsonl', exc)
        if successful:
            self.paths = found
            self.store.reconcile_sessions(sid for sid, _ in found.values())
        else:
            self.paths.update(found)
        def modified(path):
            try:
                return Path(path).stat().st_mtime_ns
            except OSError:
                return 0
        for path in sorted(found, key=modified, reverse=True):
            sid, state = found[path]
            self.store.relocate_session(sid, path, state)
            if self.selection.get(state):
                self.dirty[path] = None
        self.next_sweep = time.monotonic() + 60

    def process_file(self, filename):
        from .reader import read_increment
        path = Path(filename)
        path = path.resolve()
        if not any(root == path or root in path.parents for root in
                   (self.home / 'sessions', self.home / 'archived_sessions')):
            return
        if not path.is_file():
            return
        try:
            if path.stat().st_size == 0:
                self.errors.pop(filename, None)
                return
            sid, state = self.paths.get(filename, (None, None))
            if not sid:
                sid = _session_id(path)
                state = 'archived' if 'archived_sessions' in path.relative_to(self.home).parts else 'active'
                self.paths[filename] = (sid, state)
            self.store.relocate_session(sid, filename, state)
            if not self.selection.get(state):
                return
            if self.activity and self.activity['operation'] == 'read':
                self.activity['session_id'] = sid
                self.status(refresh=False)
            stat = path.stat()
            checkpoint = self.store.checkpoint(sid)
            offset, reader_state = 0, None
            if checkpoint:
                offset = checkpoint['offset']
                if (stat.st_ino == checkpoint['inode'] and stat.st_size == checkpoint['size'] and
                        stat.st_mtime_ns == checkpoint['mtime_ns'] and
                        checkpoint.get('path') == filename and checkpoint.get('title') == self.titles.get(sid, '') and
                        checkpoint.get('state') == state and checkpoint.get('provenance_version') == 1 and checkpoint.get('exhausted')):
                    return
                actual_sid = _session_id(path)
                if actual_sid != sid:
                    self.paths[filename] = (actual_sid, state)
                    if not any(existing_sid == sid for existing_sid, _ in self.paths.values()):
                        self.store.reset_session(sid)
                    self.process_file(filename)
                    return
                changed = (stat.st_ino != checkpoint['inode'] or stat.st_size < offset or
                           (stat.st_size == checkpoint['size'] and stat.st_mtime_ns != checkpoint['mtime_ns']))
                if not changed:
                    changed = (_digest(path, 0, checkpoint['prefix_size']) != checkpoint['prefix_hash'] or
                               _digest(path, max(0, offset - 4096), min(offset, 4096)) != checkpoint['tail_hash'])
                if changed:
                    self.store.reset_session(sid)
                    offset = 0
                else:
                    reader_state = checkpoint['reader_state']
            result = read_increment(path, offset, reader_state, max_tokens=self.block_tokens, token_counter=self.embedder.count_tokens, max_bytes=self.read_bytes)
            if not result['session'].get('id'):
                return
            if result['session']['id'] != sid:
                raise ValueError('Session identity changed')
            end = result['next_offset']
            prefix_size = min(end, 4096)
            cp = dict(offset=end, reader_state=result['state'], inode=stat.st_ino,
                      size=stat.st_size, mtime_ns=stat.st_mtime_ns, prefix_size=prefix_size,
                      prefix_hash=_digest(path, 0, prefix_size),
                      tail_hash=_digest(path, max(0, end - 4096), min(end, 4096)),
                      path=filename, title=self.titles.get(sid, ''), state=state,
                      exhausted=end == offset or end >= stat.st_size, provenance_version=1)
            from .session_types import read_provenance
            result['session'].update(read_provenance(path))
            cp['reader_state']['session'].update(result['session'])
            session = dict(result['session'], path=filename, state=state, title=self.titles.get(sid, ''))
            self.store.ingest(session, cp, result['chunks'])
            if result['errors']:
                self.errors[filename] = 'MalformedJSONL'
            else:
                self.errors.pop(filename, None)
            if end > offset and end < stat.st_size:
                self.dirty[filename] = None
        except DeferredHeader:
            self.errors.pop(filename, None)
        except Exception as exc:
            self._error(filename, exc)

    def status(self, *, refresh=True, force=False):
        # Large COUNT/GROUP BY queries run at most once every two seconds;
        # activity publications reuse cached counts and remain immediate.
        now = time.monotonic()
        if self._stats_cache is None or force or (refresh and now - self._stats_refreshed_at >= 2):
            self._stats_cache = self.store.stats(self.selection)
            from .search_history import stats as search_stats
            self._search_stats = search_stats(self.store.db)
            self._stats_refreshed_at = now
        stats = copy.deepcopy(self._stats_cache)
        while self.embedding_samples and self.embedding_samples[0][0] < now-60:
            self.embedding_samples.popleft()
        durations = [duration for _, duration in self.embedding_samples]
        embedding_metrics = dict(window_seconds=60, count=len(durations),
                                 average_seconds=sum(durations)/len(durations) if durations else None,
                                 per_minute=len(durations), observed_seconds=min(60, now-self.metrics_started))
        result = dict(stats=stats, codex_home=str(self.home), errors=dict(self.errors),
                      index_pending=bool(self.next_sweep == 0 or self.dirty or not self.events.empty() or
                                         any(stats['jobs'].values()) or
                                         (self.activity and self.activity['operation'] in ('reconcile', 'read', 'embedding'))),
                      pid=self.pid, started_at=self.started_at, cpu_cores=self.cpu_cores, cpu_unit=self.cpu_unit,
                      activity=copy.deepcopy(self.activity), last_completed_at=self.last_completed_at)
        result['session_selection'] = dict(self.selection)
        result['search_metrics'] = copy.deepcopy(self._search_stats)
        result['embedding_metrics'] = embedding_metrics
        result['database_bytes'] = self.store.database_bytes()
        with self._status_lock:
            self._status_snapshot = copy.deepcopy(result)
        return result

    def handle_request(self, request):
        if request.get('track_history') and request.get('op') in ('search', 'literal'):
            from .search_history import record
            import uuid
            ident, started = uuid.uuid4().hex, time.time()
            record(self.store.db, dict(phase='start', id=ident, started_at=started, parameters=request))
            try:
                result = self._handle_request(request)
            except Exception as exc:
                record(self.store.db, dict(phase='finish', id=ident, finished_at=time.time(),
                    duration_seconds=time.time()-started, error=type(exc).__name__ + ': ' + str(exc)))
                raise
            matches = result.get('matches', [])
            record(self.store.db, dict(phase='finish', id=ident, finished_at=time.time(),
                duration_seconds=time.time()-started, result_count=len(matches) if 'matches' in result else result.get('total', 0),
                results=[dict(session_id=m['session']['id'],chunk_id=m['id'],distance=m['distance']) for m in matches]))
            return dict(result, search_request_id=ident)
        return self._handle_request(request)

    def _handle_request(self, request):
        if request.get('codex_home') and Path(request['codex_home']).resolve() != self.home:
            raise ValueError('codex_home differs from running indexer')
        op = request.get('op')
        if op == 'status':
            return self.status()
        if op == 'search_history':
            from .search_history import record
            record(self.store.db, request['event'])
            return {}
        if op == 'session_cleanup_apply':
            from .session_management import apply
            def changed(item, new_path):
                sid, previous = item['id'], item['path']
                for path, (known_sid, _) in list(self.paths.items()):
                    if known_sid == sid:
                        self.paths.pop(path, None)
                        self.dirty.pop(path, None)
                        self.errors.pop(path, None)
                self.dirty.pop(previous, None)
                self.errors.pop(previous, None)
                if new_path is None:
                    self.store.reset_session(sid)
                else:
                    state = 'archived' if Path(new_path).is_relative_to(self.home / 'archived_sessions') else 'active'
                    self.store.relocate_session(sid, new_path, state)
                    self.paths[new_path] = (sid, state)
                    if self.selection.get(state):
                        self.dirty[new_path] = None
                self.status(force=True)
            with self._working('session_cleanup'):
                result = apply(self.home, request.get('plan'), request.get('confirmation'),
                               on_change=changed, protected_ids=request.get('protected_ids', ()))
            # Rescan after any partial operation too, including native command failure.
            self.next_sweep = 0
            self.status(force=True)
            return dict(cleanup=result)
        if op == 'literal':
            from .server import _tool_session_search
            content = _tool_session_search(dict(query=request['query'],mode='literal',codex_home=str(self.home),state=request.get('state','all'),limit=request.get('limit',10),include_snippets=True), selection=self.selection)
            return json.loads(content[0]['text'])
        if op != 'search':
            raise ValueError('Unknown operation')
        limit = request.get('limit', 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError('limit must be between 1 and 200')
        state = request.get('state', 'all')
        if state not in ('all', 'active', 'archived'):
            raise ValueError('Invalid state filter')
        if not any(enabled and state in ('all', name) for name, enabled in self.selection.items()):
            return dict(matches=[], **self.status())
        with self._working('search'):
            vector = self.embedder.encode_query(request.get('query'))
            matches = self.store.search(vector, limit=limit, state=request.get('state', 'all'), selection=self.selection)
        return dict(matches=matches, **self.status())

    def step(self, *, indexing_enabled=True):
        from .settings import read_selection
        selection = self.selection_reader()
        if selection != self.selection:
            self.selection = selection
            self.dirty.clear()
            self.next_sweep = 0
            self.status(force=True)
        # Interactive requests go before background file/model work.
        try:
            request, response = self.requests.get_nowait()
        except queue.Empty:
            pass
        else:
            try:
                response.put(dict(ok=True, **self.handle_request(request)))
            except Exception as exc:
                response.put(dict(ok=False, error=type(exc).__name__ + ': ' + str(exc)))
            return True
        if not indexing_enabled:
            return False
        if time.monotonic() >= self.next_sweep:
            with self._working('reconcile'):
                self.reconcile()
        for _ in range(1000):
            try:
                path = self.events.get_nowait()
            except queue.Empty:
                break
            if path.endswith('.jsonl') and Path(path).name.startswith('rollout-'):
                self.dirty[path] = None
                self.dirty.move_to_end(path, last=False)
        worked = False
        if self.dirty:
            path, _ = self.dirty.popitem(last=False)
            with self._working('read', self.paths.get(path, (None, None))[0], path):
                self.process_file(path)
            worked = True
        # One job per step bounds request delay to one inference call.
        if not self.requests.empty():
            # Publish newly ingested jobs before serving an intervening search.
            self.status(force=worked)
            self._last_step_worked = worked or self._last_step_worked
            return True
        job = self.store.claim_job(selection=self.selection)
        if job:
            try:
                row = self.store.db.execute('SELECT session_id FROM chunks WHERE id=?', (job['chunk_id'],)).fetchone()
                sid = row['session_id'] if row else None
                path = next((path for path, (session_id, _) in self.paths.items() if session_id == sid), None)
                with self._working('embedding', sid, path):
                    started = time.monotonic()
                    vector = self.embedder.encode([job['text']])[0]
                    if self.store.complete_job(job['id'], job['content_hash'], vector):
                        self.embedding_samples.append((time.monotonic(), time.monotonic()-started))
            except Exception as exc:
                self.store.fail_job(job['id'], type(exc).__name__)
                self._error('embedding', exc)
            else:
                self.errors.pop('embedding', None)
            worked = True
        self.status(force=self._last_step_worked and not worked)
        self._last_step_worked = worked
        return worked


def serve(actor, path):
    import fcntl
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(60)
            try:
                raw = self.rfile.readline(MAX_RPC_BYTES + 1)
                if not raw.endswith(b'\n') or len(raw) > MAX_RPC_BYTES:
                    raise ValueError('Invalid request size')
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('Request must be an object')
                if payload.get('op') == 'status':
                    result = dict(ok=True, **actor.cached_status(payload))
                else:
                    response = queue.Queue(maxsize=1)
                    actor.requests.put((payload, response))
                    wait = cleanup_timeout(payload.get('plan')) if payload.get('op') == 'session_cleanup_apply' else 60
                    result = response.get(timeout=wait)
            except Exception as exc:
                result = dict(ok=False, error=type(exc).__name__)
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode() + b'\n')

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    socket_lock = open(str(path) + '.lock', 'a')
    try:
        fcntl.flock(socket_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    pass
                else:
                    raise RuntimeError('Indexer socket is already owned by a live service')
            path.unlink()
        server = Server(str(path), Handler)
    except Exception:
        socket_lock.close()
        raise
    server.socket_lock = socket_lock
    os.chmod(path, 0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def configure_cores(cores):
    groups = core_groups(allowed=os.sched_getaffinity(0))
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= len(groups):
        raise ValueError(f'cores must be between 1 and {len(groups)} physical cores')
    # One logical CPU per physical core: SMT siblings are not separate cores.
    os.sched_setaffinity(0, [group[0] for group in groups[:cores]])
    return cores


def run(args):
    import fcntl
    from .embedding import Embedder
    from .store import Store
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    database = Path(args.database).expanduser()
    database.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with open(str(database) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        requested = args.cores if args.cores is not None else read_cores()
        if requested is None:
            requested = min(args.threads, len(core_groups(allowed=os.sched_getaffinity(0))))
        cores = configure_cores(requested)
        embedder = Embedder(args.model_path, threads=cores, dimensions=args.dimensions, max_length=args.block_tokens)
        store = Store(database, embedder.identity, embedder.dimensions,
                      chunking=f'conversation-blocks-v1:{args.block_tokens}', rebuild=args.rebuild_index)
        actor = Indexer(args.codex_home, store, embedder, block_tokens=args.block_tokens)
        class Events(FileSystemEventHandler):
            def on_any_event(self, event):
                if not event.is_directory and event.event_type in ('created', 'modified', 'moved', 'deleted'):
                    actor.mark(event.src_path)
                    if getattr(event, 'dest_path', None):
                        actor.mark(event.dest_path)
        observer = Observer()
        observed = set()
        observer.schedule(Events(), str(actor.home), recursive=False)
        def watch_roots():
            for name in ('sessions', 'archived_sessions'):
                root = actor.home / name
                if root.is_dir() and root not in observed:
                    observer.schedule(Events(), str(root), recursive=True)
                    observed.add(root)
        watch_roots()
        observer.start()
        server = serve(actor, args.socket)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: actor.stopping.set())
        try:
            while not actor.stopping.is_set():
                watch_roots()
                if not actor.step():
                    actor.stopping.wait(0.05)
        finally:
            observer.stop()
            observer.join()
            server.shutdown()
            server.server_close()
            Path(args.socket).unlink(missing_ok=True)
            server.socket_lock.close()
            store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex-home', default=str(Path.home() / '.codex'))
    parser.add_argument('--database', default=str(Path.home() / '.local/share/codex-session-index/index.sqlite3'))
    parser.add_argument('--socket', default=str(default_socket()))
    parser.add_argument('--model-path')
    parser.add_argument('--block-tokens', type=int, choices=(512,1024,2048), default=512)
    parser.add_argument('--dimensions', type=int, choices=(256,512,1024), default=1024)
    parser.add_argument('--rebuild-index', action='store_true', help='Rebuild incompatible derived index; preserve JSONL and search history')
    parser.add_argument('--cores', type=int, help='Physical cores; overrides saved TUI setting')
    parser.add_argument('--threads', type=int, default=2, help='Legacy fallback core count when no setting exists')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
