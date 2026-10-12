"""Single-writer SQLite session index and durable embedding queue."""
import hashlib
import json
import sqlite3
import struct
import time


class Store:
    def __init__(self, path, model, dimensions, max_attempts=5):
        if not isinstance(dimensions, int) or dimensions <= 0 or max_attempts <= 0:
            raise ValueError('Dimensions and max_attempts must be positive')
        import sqlite_vec
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.dimensions = dimensions
        self.max_attempts = max_attempts
        self.db.enable_load_extension(True)
        try:
            sqlite_vec.load(self.db)
        finally:
            self.db.enable_load_extension(False)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, state TEXT NOT NULL, checkpoint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks(pk INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, text TEXT NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, content_hash TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, chunk_pk INTEGER UNIQUE NOT NULL REFERENCES chunks(pk) ON DELETE CASCADE, content_hash TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, error TEXT);
        ''')
        identity = json.dumps({'model': model, 'dimensions': dimensions}, sort_keys=True)
        existing = self.db.execute("SELECT value FROM metadata WHERE key='embedding_identity'").fetchone()
        if existing and existing[0] != identity:
            self.db.close()
            raise ValueError('Embedding model or dimensions mismatch; rebuild index explicitly')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('embedding_identity',?)", (identity,))
            self.db.execute(f'CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(embedding float[{int(dimensions)}])')
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='running' AND attempts<?", (self.max_attempts,))
            self.db.execute("UPDATE jobs SET state='failed',error='Interrupted final attempt' WHERE state='running'", ())

    def close(self):
        self.db.close()

    def checkpoint(self, session_id):
        row = self.db.execute('SELECT checkpoint FROM sessions WHERE id=?', (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def sessions(self):
        return [dict(json.loads(r["metadata"]), checkpoint=json.loads(r["checkpoint"]))
                for r in self.db.execute("SELECT metadata,checkpoint FROM sessions")]

    def stats(self):
        result = {name: self.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("sessions", "chunks", "vectors")}
        result["jobs"] = {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state")}
        return result

    def reset_session(self, session_id):
        with self.db:
            self._delete_session(session_id)

    def reconcile_sessions(self, session_ids):
        keep = set(session_ids)
        with self.db:
            for row in self.db.execute('SELECT id FROM sessions').fetchall():
                if row[0] not in keep:
                    self._delete_session(row[0])

    def _delete_session(self, session_id):
        self.db.execute('DELETE FROM vectors WHERE rowid IN (SELECT pk FROM chunks WHERE session_id=?)', (session_id,))
        self.db.execute('DELETE FROM sessions WHERE id=?', (session_id,))

    def ingest(self, session, checkpoint, chunks):
        """Upsert appended chunks and commit the file checkpoint atomically."""
        session_id = session['id']
        state = session.get('state', 'active')
        if state not in ('active', 'archived'):
            raise ValueError('Invalid session state')
        if len({c['id'] for c in chunks}) != len(chunks):
            raise ValueError('Duplicate chunk IDs')
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,state=excluded.state,checkpoint=excluded.checkpoint',
                            (session_id, json.dumps(session), state, json.dumps(checkpoint)))
            for chunk in chunks:
                digest = hashlib.sha256(chunk['text'].encode('utf-8')).hexdigest()
                old = self.db.execute('SELECT pk,content_hash,session_id FROM chunks WHERE id=?', (chunk['id'],)).fetchone()
                if old and old['session_id'] != session_id:
                    raise ValueError('Chunk ID belongs to another session')
                self.db.execute('INSERT INTO chunks(id,session_id,text,start_offset,end_offset,content_hash,metadata) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,start_offset=excluded.start_offset,end_offset=excluded.end_offset,content_hash=excluded.content_hash,metadata=excluded.metadata',
                                (chunk['id'], session_id, chunk['text'], chunk['start_offset'], chunk['end_offset'], digest, json.dumps({k: v for k, v in chunk.items() if k not in ('id', 'session_id', 'text', 'start_offset', 'end_offset', 'content_hash', 'text_hash')})))
                pk = self.db.execute('SELECT pk FROM chunks WHERE id=?', (chunk['id'],)).fetchone()[0]
                if not old or old['content_hash'] != digest:
                    self.db.execute('DELETE FROM vectors WHERE rowid=?', (pk,))
                    self.db.execute('DELETE FROM jobs WHERE chunk_pk=?', (pk,))
                    self.db.execute("INSERT INTO jobs(chunk_pk,content_hash,state) VALUES(?,?,'pending')", (pk, digest))

    def claim_job(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT j.*,c.text,c.id AS chunk_id FROM jobs j JOIN chunks c ON c.pk=j.chunk_pk WHERE j.state IN ('pending','failed') AND j.attempts<? AND j.next_attempt<=? ORDER BY j.id LIMIT 1", (self.max_attempts, now)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (row['id'],))
            result = dict(row)
            result['attempts'] += 1
            result['state'] = 'running'
            return result

    def complete_job(self, job_id, content_hash, vector):
        blob = self._vector(vector)
        with self.db:
            row = self.db.execute("SELECT j.chunk_pk FROM jobs j JOIN chunks c ON c.pk=j.chunk_pk WHERE j.id=? AND j.state='running' AND j.content_hash=? AND c.content_hash=j.content_hash", (job_id, content_hash)).fetchone()
            if row is None:
                return False
            self.db.execute('DELETE FROM vectors WHERE rowid=?', (row[0],))
            self.db.execute('INSERT INTO vectors(rowid,embedding) VALUES(?,?)', (row[0], blob))
            self.db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            return True

    def fail_job(self, job_id, error, now=None):
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT attempts FROM jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='failed',error=?,next_attempt=? WHERE id=?", (str(error)[:2000], now + min(3600, 2 ** row[0]), job_id))

    def _vector(self, vector):
        if len(vector) != self.dimensions:
            raise ValueError('Wrong vector dimensions')
        import math
        if not all(math.isfinite(v) for v in vector):
            raise ValueError('Non-finite vector')
        return struct.pack(f'{self.dimensions}f', *vector)

    def search(self, vector, limit=10, state='all'):
        if state not in ('active', 'archived', 'all'):
            raise ValueError('Invalid state filter')
        limit = max(0, int(limit))
        if not limit:
            return []
        rows = self.db.execute("""WITH nearest AS (
            SELECT rowid,distance FROM vectors
            WHERE embedding MATCH ? AND k=? AND rowid IN (
                SELECT c.pk FROM chunks c JOIN sessions s ON s.id=c.session_id
                WHERE (?='all' OR s.state=?)
            )
        ) SELECT c.*,s.metadata AS session_metadata,s.state,n.distance
        FROM nearest n JOIN chunks c ON c.pk=n.rowid JOIN sessions s ON s.id=c.session_id
        ORDER BY n.distance,c.id""", (self._vector(vector), limit, state, state)).fetchall()
        return [dict(json.loads(r['metadata']), id=r['id'], text=r['text'], session=json.loads(r['session_metadata']), state=r['state'], start_offset=r['start_offset'], end_offset=r['end_offset'], distance=r['distance'], score=r['distance']) for r in rows]
