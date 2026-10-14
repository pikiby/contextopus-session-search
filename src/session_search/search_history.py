"""Search facts in the existing SQLite; no vectors or model calls here."""
import json
import re
import time


def sanitize(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'(?i)secret|password|authorization|api.?key|access.?token', k) else sanitize(v) for k,v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+', '[REDACTED]', value)
        value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', value)
        return re.sub(r'(?i)((?:password|secret|api[_-]?key|token)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', value)
    return value


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS search_requests(
      id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
      duration_seconds REAL, tool TEXT NOT NULL, parameters TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('started','completed','error')),
      result_count INTEGER, error TEXT);
    CREATE INDEX IF NOT EXISTS search_requests_time ON search_requests(started_at);
    CREATE TABLE IF NOT EXISTS search_results(
      request_id TEXT NOT NULL REFERENCES search_requests(id) ON DELETE CASCADE,
      rank INTEGER NOT NULL, session_id TEXT, chunk_id TEXT, distance REAL,
      PRIMARY KEY(request_id,rank));
    ''')


def record(db, event):
    event = sanitize(event)
    with db:
        if event['phase'] == 'start':
            db.execute("INSERT OR IGNORE INTO search_requests(id,started_at,tool,parameters,state) VALUES(?,?,?,?,'started')",
                       (event['id'], event['started_at'], 'session_search', json.dumps(event['parameters'], ensure_ascii=False)))
        elif event['phase'] == 'finish':
            row = db.execute("UPDATE search_requests SET finished_at=?,duration_seconds=?,state=?,result_count=?,error=? WHERE id=? AND state='started'",
                       (event['finished_at'], event['duration_seconds'], 'error' if event.get('error') else 'completed', event.get('result_count'), event.get('error'), event['id']))
            if row.rowcount:
                for rank, result in enumerate(event.get('results', []), 1):
                    db.execute('INSERT INTO search_results VALUES(?,?,?,?,?)', (event['id'], rank, result.get('session_id'), result.get('chunk_id'), result.get('distance')))
        else:
            raise ValueError('Invalid history phase')


def stats(db, now=None):
    now = time.time() if now is None else now
    row = db.execute('''SELECT COUNT(*),
      COALESCE(SUM(state='error'),0),
      COALESCE(SUM(state='completed' AND result_count=0),0),
      AVG(CASE WHEN state='completed' THEN duration_seconds END),
      COALESCE(SUM(state='started'),0)
      FROM search_requests WHERE started_at>=?''', (now-86400,)).fetchone()
    return dict(calls=row[0], errors=row[1], empty=row[2], average_seconds=row[3], unknown=row[4], window_seconds=86400)
