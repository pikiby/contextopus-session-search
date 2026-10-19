"""Deterministic session inventory and explicitly confirmed native Codex operations.

No model loading, semantic classification, direct Codex database writes or backups.
Call apply from the indexer's serialized actor; on_change reconciles its search store.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID


_FILTERS = {'state', 'cwd', 'cwd_prefix', 'path', 'created_after', 'created_before',
            'last_message_after', 'last_message_before', 'min_bytes', 'max_bytes', 'models', 'ids', 'session_types'}


def _date(value):
    if not isinstance(value, str):
        raise ValueError('Dates must be ISO 8601 strings')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError as exc:
        raise ValueError(f'Invalid ISO 8601 date: {value}') from exc


def _fingerprint(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if identity(before) != identity(after):
        raise ValueError(f'Session changed while reading: {path}')
    return dict(size_bytes=after.st_size, mtime_ns=after.st_mtime_ns,
                inode=after.st_ino, device=after.st_dev, sha256=digest.hexdigest())


def _metadata(path, home, state):
    before = path.stat()
    digest = hashlib.sha256()
    session_id, cwd, created, last_message = None, None, None, None
    models = set()
    provenance = {}
    with path.open('rb') as stream:
        for line in stream:
            digest.update(line)
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get('payload')
            if not isinstance(payload, dict):
                continue
            kind = record.get('type')
            if kind == 'session_meta':
                from .session_types import classify
                provenance = dict(classify(payload), source=payload.get('source'), forked_from_id=payload.get('forked_from_id'))
                session_id = payload.get('id')
                cwd, created = payload.get('cwd'), payload.get('timestamp') or record.get('timestamp')
            if kind == 'turn_context' and isinstance(payload.get('model'), str):
                models.add(payload['model'])
            if kind == 'response_item' and payload.get('type') == 'message' and payload.get('role') in ('user', 'assistant'):
                timestamp = record.get('timestamp')
                if timestamp and (last_message is None or _date(timestamp) > _date(last_message)):
                    last_message = timestamp
    try:
        session_id = str(UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        return None
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError(f'Session changed while reading: {path}')
    fingerprint = dict(size_bytes=after.st_size, mtime_ns=after.st_mtime_ns, inode=after.st_ino, device=after.st_dev, sha256=digest.hexdigest())
    return dict(id=session_id, path=str(path), state=state, cwd=cwd, created_at=created,
                last_message_at=last_message, models=sorted(models),
                size_bytes=fingerprint['size_bytes'], fingerprint=fingerprint, **provenance)


def _validate_filters(filters):
    if not isinstance(filters, dict) or set(filters) - _FILTERS:
        raise ValueError('Unknown or invalid metadata filters')
    if filters.get('state', 'all') not in ('active', 'archived', 'all'):
        raise ValueError('state must be active, archived or all')
    if 'session_types' in filters:
        from .session_types import TYPES
        if not isinstance(filters['session_types'], list) or any(t not in TYPES for t in filters['session_types']):
            raise ValueError('Invalid session_types')
    for key in ('cwd', 'cwd_prefix', 'path'):
        if key in filters and not isinstance(filters[key], str):
            raise ValueError(f'{key} must be a string')
    for key in ('min_bytes', 'max_bytes'):
        if key in filters and (type(filters[key]) is not int or filters[key] < 0):
            raise ValueError(f'{key} must be a nonnegative integer')
    if filters.get('min_bytes', 0) > filters.get('max_bytes', float('inf')):
        raise ValueError('min_bytes exceeds max_bytes')
    for key in ('models', 'ids'):
        if key in filters and (not isinstance(filters[key], list) or not all(isinstance(v, str) for v in filters[key])):
            raise ValueError(f'{key} must be an array of strings')
    for sid in filters.get('ids', []):
        try:
            if str(UUID(sid)) != sid:
                raise ValueError('UUID must be lowercase canonical form')
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f'Invalid session UUID: {sid}') from exc
    for key in ('created_after', 'created_before', 'last_message_after', 'last_message_before'):
        if key in filters:
            _date(filters[key])
    for prefix in ('created', 'last_message'):
        if f'{prefix}_after' in filters and f'{prefix}_before' in filters:
            if _date(filters[f'{prefix}_after']) > _date(filters[f'{prefix}_before']):
                raise ValueError(f'{prefix} date interval is inverted')


def _matches(item, filters):
    if 'session_types' in filters and item.get('session_type', 'unknown') not in filters['session_types']:
        return False
    if filters.get('state', 'all') not in ('all', item['state']):
        return False
    for key in ('cwd', 'path'):
        if key in filters and item[key] != filters[key]:
            return False
    if 'cwd_prefix' in filters:
        if not item['cwd'] or not Path(item['cwd']).is_relative_to(Path(filters['cwd_prefix'])):
            return False
    if 'ids' in filters and item['id'] not in filters['ids']:
        return False
    if 'models' in filters and not set(filters['models']).intersection(item['models']):
        return False
    if not filters.get('min_bytes', 0) <= item['size_bytes'] <= filters.get('max_bytes', float('inf')):
        return False
    for field, prefix in (('created_at', 'created'), ('last_message_at', 'last_message')):
        for suffix in ('after', 'before'):
            key = f'{prefix}_{suffix}'
            if key in filters:
                if not item[field]:
                    return False
                value, bound = _date(item[field]), _date(filters[key])
                if (suffix == 'after' and value < bound) or (suffix == 'before' and value > bound):
                    return False
    return True


def inventory(home, filters=None):
    """Scan canonical JSONL metadata. Date bounds inclusive; model names exact."""
    home, filters = Path(home).resolve(), {} if filters is None else filters
    _validate_filters(filters)
    sessions = []
    for state, directory in (('active', 'sessions'), ('archived', 'archived_sessions')):
        root = home / directory
        if root.is_symlink():
            continue
        if filters.get('state', 'all') not in ('all', state):
            continue
        for path in sorted(root.rglob('rollout-*.jsonl')):
            if path.resolve() != path or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                continue
            if 'path' in filters and str(path) != filters['path']:
                continue
            size = path.stat().st_size
            if not filters.get('min_bytes', 0) <= size <= filters.get('max_bytes', float('inf')):
                continue
            if any(key in filters for key in ('ids', 'cwd', 'cwd_prefix')):
                header = None
                with path.open('rb') as stream:
                    for line in stream:
                        try:
                            candidate = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(candidate, dict) and candidate.get('type') == 'session_meta':
                            header = candidate
                            break
                payload = header.get('payload') if isinstance(header, dict) else None
                if not isinstance(payload, dict):
                    continue
                if 'ids' in filters and payload.get('id') not in filters['ids']:
                    continue
                if 'cwd' in filters and payload.get('cwd') != filters['cwd']:
                    continue
                if 'cwd_prefix' in filters and (not isinstance(payload.get('cwd'), str) or not Path(payload['cwd']).is_relative_to(Path(filters['cwd_prefix']))):
                    continue
            item = _metadata(path, home, state)
            if item and _matches(item, filters):
                sessions.append(item)
    return dict(codex_home=str(home), sessions=sessions, count=len(sessions),
                total_bytes=sum(item['size_bytes'] for item in sessions))


@contextmanager
def _readonly_database(db):
    try:
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as connection:
            yield connection
    except sqlite3.Error as exc:
        raise ValueError(f'Cannot inspect native state database: {exc}') from exc


def _delete_order(home, items):
    ids = {item['id'] for item in items}
    edges = {}
    databases = list(home.glob('state_*.sqlite'))
    if len(databases) > 1:
        raise ValueError('Ambiguous native state databases; cannot verify deletion cascade')
    for db in databases:
        with _readonly_database(db) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'thread_spawn_edges' not in tables:
                continue
            columns = {row[1] for row in connection.execute('PRAGMA table_info(thread_spawn_edges)')}
            parent = next((c for c in ('parent_thread_id', 'parent_id') if c in columns), None)
            child = next((c for c in ('child_thread_id', 'child_id') if c in columns), None)
            if not parent or not child:
                raise ValueError('Cannot safely inspect native deletion cascade schema')
            for source, target in connection.execute(f'SELECT "{parent}", "{child}" FROM thread_spawn_edges'):
                edges.setdefault(source, set()).add(target)
    ordered, visited, visiting, blocked = [], set(), set(), set()
    def visit(sid):
        if sid in visiting:
            raise ValueError('Cycle in native session spawn graph')
        if sid in visited:
            return
        visiting.add(sid)
        for child in sorted(edges.get(sid, ())):
            if child not in ids:
                blocked.add(child)
            else:
                visit(child)
        visiting.remove(sid)
        visited.add(sid)
        ordered.append(sid)
    for item in items:
        visit(item['id'])
    mapping = {item['id']: item for item in items}
    return [mapping[sid] for sid in ordered], sorted(blocked)


def _verify_native_state(home, sid, action, new_path, *, preflight=False):
    databases = list(home.glob('state_*.sqlite'))
    if len(databases) > 1:
        raise ValueError('Ambiguous native state databases')
    for db in databases:
        with _readonly_database(db) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'threads' not in tables:
                continue
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            if not {'id', 'archived', 'rollout_path'}.issubset(columns):
                raise ValueError('Cannot verify native thread state schema')
            row = connection.execute('SELECT archived, rollout_path FROM threads WHERE id=?', (sid,)).fetchone()
            if preflight:
                if row and (bool(row[0]) != (action == 'archive') or not isinstance(row[1], str) or str(Path(row[1]).resolve()) != new_path):
                    raise ValueError('Native thread database differs from selected preview')
                continue
            if action == 'delete':
                if row:
                    raise RuntimeError('Native delete retained thread database row')
            elif not row or bool(row[0]) != (action == 'archive') or str(Path(row[1]).resolve()) != new_path:
                raise RuntimeError('Native thread database state does not match session location')
    if action == 'delete' and not preflight:
        index = home / 'session_index.jsonl'
        if index.exists():
            with index.open('rb') as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(record, dict) and record.get('id') == sid:
                        raise RuntimeError('Native delete retained session_index entry')


def _plan_id(plan):
    body = {k: v for k, v in plan.items() if k != 'plan_id'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def preview(home, action, filters=None):
    """Return exact selection; never execute an operation."""
    if action not in ('archive', 'unarchive', 'delete'):
        raise ValueError('action must be archive, unarchive or delete')
    data = inventory(home, filters)
    items = data.pop('sessions')
    required = {'archive': 'active', 'unarchive': 'archived'}.get(action)
    if required:
        items = [item for item in items if item['state'] == required]
    ids = [item['id'] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate session IDs; repair ambiguous storage before cleanup')
    blocked = []
    if action == 'delete':
        items, blocked = _delete_order(Path(data['codex_home']), items)
    plan = dict(version=1, action=action, codex_home=data['codex_home'], items=items,
                count=len(items), total_bytes=sum(item['size_bytes'] for item in items),
                blocked_ids=blocked, blocked_count=len(blocked))
    plan['plan_id'] = _plan_id(plan)
    return plan


def apply(home, plan, confirmation, on_change=None, *, codex_bin='codex', runner=None, protected_ids=()):
    """Execute only an unchanged preview, via native Codex, in the indexer actor.

    Stops on first failure and reports partial completion. Confirmation is the
    exact plan_id; the caller must obtain explicit user authorization for it.
    """
    home = Path(home).resolve()
    if not isinstance(plan, dict) or plan.get('version') != 1 or plan.get('plan_id') != _plan_id(plan):
        raise ValueError('Invalid cleanup plan')
    if confirmation != plan['plan_id'] or str(home) != plan.get('codex_home'):
        raise ValueError('Confirmation or Codex home does not match preview')
    action = plan.get('action')
    if action not in ('archive', 'unarchive', 'delete'):
        raise ValueError('Invalid cleanup action')
    items = plan.get('items', [])
    if plan.get('blocked_count'):
        raise ValueError('Native delete cascades to unselected children; preview blocked')
    snapshot = inventory(home, {'ids': [item['id'] for item in items]})
    current = {item['id']: item for item in snapshot['sessions']}
    if len(current) != snapshot['count']:
        raise ValueError('Duplicate session IDs')
    if len({item['id'] for item in items}) != len(items):
        raise ValueError('Duplicate items in cleanup plan')
    if action == 'delete':
        ordered, blocked = _delete_order(home, items)
        if blocked or ordered != items:
            raise ValueError('Native deletion cascade changed; preview again')
    protected = set(protected_ids) | {os.environ.get('CODEX_THREAD_ID')}
    for item in items:
        if item['id'] in protected:
            raise ValueError('Cannot mutate the current Codex session')
        if action == 'archive' and item['state'] != 'active' or action == 'unarchive' and item['state'] != 'archived':
            raise ValueError('Invalid source state for action')
        if current.get(item['id']) != item:
            raise ValueError(f"Preview is stale for {item['id']}; preview again")
        _verify_native_state(home, item['id'], 'archive' if item['state'] == 'archived' else 'unarchive', item['path'], preflight=True)
    completed, error = [], None
    runner = runner or subprocess.run
    for item in items:
        native_completed = False
        try:
            if action == 'delete':
                _, blocked = _delete_order(home, [item])
                if blocked:
                    raise ValueError('Native delete cascades to children; preview again')
            path = Path(item['path'])
            root = home / ('sessions' if item['state'] == 'active' else 'archived_sessions')
            if path.resolve() != path or root.resolve() != root or not path.is_relative_to(root):
                raise ValueError('Session path left canonical storage or became a symlink')
            _verify_native_state(home, item['id'], 'archive' if item['state'] == 'archived' else 'unarchive', item['path'], preflight=True)
            if _fingerprint(Path(item['path'])) != item['fingerprint']:
                raise ValueError('Session changed after preflight')
            command = [str(codex_bin), action]
            if action == 'delete':
                command.append('--force')
            command.append(str(UUID(item['id'])))
            result = runner(command, env={**os.environ, 'CODEX_HOME': str(home)},
                            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or 'Native Codex command failed')[-2000:])
            native_completed = True
            new_items = [entry for entry in inventory(home, {'ids': [item['id']]})['sessions'] if entry['id'] == item['id']]
            if action == 'delete':
                if new_items or Path(item['path']).exists():
                    raise RuntimeError('Native delete did not remove canonical session')
                new_path = None
            else:
                expected = 'archived' if action == 'archive' else 'active'
                if len(new_items) != 1 or new_items[0]['state'] != expected:
                    raise RuntimeError('Native command did not produce expected session state')
                new_path = new_items[0]['path']
            _verify_native_state(home, item['id'], action, new_path)
            if on_change:
                on_change(item, new_path)
            completed.append(dict(id=item['id'], previous_path=item['path'], path=new_path))
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
            error = dict(id=item['id'], message=str(exc), native_completed=native_completed)
            break
    return dict(ok=error is None, action=action, plan_id=plan['plan_id'],
                completed=completed, error=error, count=len(completed))
