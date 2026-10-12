"""Single actor owns the model and SQLite; watcher/RPC threads only enqueue."""
import argparse
import collections
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import signal
import socket
import socketserver
import threading
import time


def default_socket():
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime) / 'codex-session-index.sock' if runtime else Path.home() / '.cache/codex-session-index/index.sock'


def request_index(payload, socket_path=None, timeout=60):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path or os.environ.get('CODEX_SESSION_INDEX_SOCKET') or default_socket()))
        client.sendall(json.dumps(payload).encode() + b'\n')
        with client.makefile('rb') as stream:
            raw = stream.readline(4 * 1024 * 1024)
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


class Indexer:
    def __init__(self, codex_home, store, embedder):
        self.home = Path(codex_home).resolve()
        self.store, self.embedder = store, embedder
        self.events, self.requests = queue.Queue(), queue.Queue()
        self.dirty = collections.OrderedDict()
        self.paths, self.titles, self.errors = {}, {}, {}
        self.stopping = threading.Event()
        self.next_sweep = 0

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
            stat = path.stat()
            checkpoint = self.store.checkpoint(sid)
            offset, reader_state = 0, None
            if checkpoint:
                offset = checkpoint['offset']
                if (stat.st_ino == checkpoint['inode'] and stat.st_size == checkpoint['size'] and
                        stat.st_mtime_ns == checkpoint['mtime_ns'] and
                        checkpoint.get('path') == filename and checkpoint.get('title') == self.titles.get(sid, '') and
                        checkpoint.get('state') == state and checkpoint.get('exhausted')):
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
            result = read_increment(path, offset, reader_state, token_counter=self.embedder.count_tokens)
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
                      exhausted=end == offset or end >= stat.st_size)
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

    def status(self):
        stats = self.store.stats()
        return dict(stats=stats, codex_home=str(self.home), errors=dict(self.errors),
                    index_pending=bool(self.next_sweep == 0 or self.dirty or not self.events.empty() or
                                       any(stats['jobs'].values())))

    def handle_request(self, request):
        if request.get('codex_home') and Path(request['codex_home']).resolve() != self.home:
            raise ValueError('codex_home differs from running indexer')
        op = request.get('op')
        if op == 'status':
            return self.status()
        if op != 'search':
            raise ValueError('Unknown operation')
        limit = request.get('limit', 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError('limit must be between 1 and 200')
        vector = self.embedder.encode_query(request.get('query'))
        matches = self.store.search(vector, limit=limit, state=request.get('state', 'all'))
        return dict(matches=matches, **self.status())

    def step(self):
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
        if time.monotonic() >= self.next_sweep:
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
            self.process_file(path)
            worked = True
        # One job per step bounds request delay to one inference call.
        if not self.requests.empty():
            return True
        job = self.store.claim_job()
        if job:
            try:
                self.store.complete_job(job['id'], job['content_hash'], self.embedder.encode([job['text']])[0])
            except Exception as exc:
                self.store.fail_job(job['id'], type(exc).__name__)
                self._error('embedding', exc)
            else:
                self.errors.pop('embedding', None)
            worked = True
        return worked


def serve(actor, path):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(60)
            try:
                raw = self.rfile.readline(65537)
                if not raw.endswith(b'\n') or len(raw) > 65536:
                    raise ValueError('Invalid request size')
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('Request must be an object')
                response = queue.Queue(maxsize=1)
                actor.requests.put((payload, response))
                result = response.get(timeout=60)
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


def run(args):
    from .embedding import Embedder
    from .store import Store
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    database = Path(args.database).expanduser()
    database.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with open(str(database) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        embedder = Embedder(args.model_path, threads=args.threads)
        store = Store(database, embedder.identity, embedder.dimensions)
        actor = Indexer(args.codex_home, store, embedder)
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
    parser.add_argument('--threads', type=int, default=2)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
