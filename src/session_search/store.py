"""Single-writer SQLite: unique text blocks, occurrences and durable jobs."""
import hashlib
import json
import sqlite3
import struct
import time
from pathlib import Path


def _connect(path, readonly=False, *, vectors=True):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) if readonly else sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    if vectors:
        import sqlite_vec
        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)
    db.execute('PRAGMA foreign_keys=ON')
    return db


def _states(selection=None):
    return tuple(s for s in ('active', 'archived') if selection is None or selection.get(s, False))


def _type_predicate(selection, alias="s"):
    if selection is None or "types" not in selection:
        return "1"
    from .session_types import TYPES
    types = selection["types"]
    if not isinstance(types, list) or any(t not in TYPES for t in types):
        raise ValueError("Invalid session types")
    values = ",".join("'" + t + "'" for t in types) or "NULL"
    return f"COALESCE(json_extract({alias}.metadata,'$.session_type'),'unknown') IN ({values})"


def _counts(db, selection=None):
    states = _states(selection)
    placeholders = ','.join('?' for _ in states) or 'NULL'
    modern = any(r[1] == 'block_pk' for r in db.execute('PRAGMA table_info(chunks)'))
    key = 'block_pk' if modern else 'pk'
    eligible = f'SELECT DISTINCT c.{key} FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE s.state IN ({placeholders}) AND {_type_predicate(selection)}'
    jobs_key = 'block_pk' if modern else 'chunk_pk'
    return dict(sessions=db.execute(f'SELECT COUNT(*) FROM sessions s WHERE state IN ({placeholders}) AND {_type_predicate(selection)}', states).fetchone()[0],
                chunks=db.execute(f'SELECT COUNT(*) FROM ({eligible})', states).fetchone()[0],
                vectors=db.execute(f'SELECT COUNT(*) FROM vectors WHERE rowid IN ({eligible})', states).fetchone()[0],
                jobs={r[0]: r[1] for r in db.execute(f'SELECT state,COUNT(*) FROM jobs WHERE {jobs_key} IN ({eligible}) GROUP BY state', states)})


def database_bytes(path):
    return sum(p.stat().st_size for p in (Path(path), Path(str(path)+'-wal'), Path(str(path)+'-shm')) if p.exists())


def read_stats(path, selection=None):
    """Read old or new on-disk counters without initialization or job recovery."""
    from .search_history import stats
    db = _connect(path, readonly=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        result = dict(stats=_counts(db, selection), database_bytes=database_bytes(path))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='search_requests'").fetchone():
            result['search_metrics'] = stats(db)
        return result
    finally:
        db.close()


class Store:
    def __init__(self, path, model, dimensions, max_attempts=5, *, chunking='blocks-v1', rebuild=False, vector_backend=None):
        if not isinstance(dimensions, int) or dimensions <= 0 or max_attempts <= 0:
            raise ValueError('Dimensions and max_attempts must be positive')
        self.path, self.dimensions, self.max_attempts = Path(path), dimensions, max_attempts
        self.vector_backend = vector_backend
        self.db = _connect(path, vectors=vector_backend is None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        identity = json.dumps({'model': model, 'dimensions': dimensions}, sort_keys=True)
        existing = dict(self.db.execute('SELECT key,value FROM metadata'))
        backend = 'core-lancedb' if vector_backend else 'sqlite-vec'
        if existing.get('vector_backend', 'sqlite-vec') != backend and existing:
            self.db.close()
            raise ValueError('Storage backend differs; use explicit migration into a separate destination')
        legacy = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='chunks'").fetchone() and not any(r[1] == 'block_pk' for r in self.db.execute('PRAGMA table_info(chunks)'))
        mismatch = existing.get('embedding_identity', identity) != identity or existing.get('chunking_identity', chunking) != chunking or legacy
        if mismatch and not rebuild:
            self.db.close()
            raise ValueError('Embedding model, dimensions or chunking mismatch; rebuild index explicitly')
        with self.db:
            if mismatch:
                # Derived data only. JSONL and search history are not touched.
                for table in ('vectors', 'jobs', 'chunks', 'blocks'):
                    self.db.execute(f'DROP TABLE IF EXISTS {table}')
                self.db.execute("UPDATE sessions SET checkpoint='{}'")
            statements = (
                'CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, state TEXT NOT NULL, checkpoint TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS blocks(pk INTEGER PRIMARY KEY AUTOINCREMENT, content_hash TEXT UNIQUE NOT NULL, text TEXT NOT NULL)',
                "CREATE TABLE IF NOT EXISTS chunks(pk INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, block_pk INTEGER NOT NULL REFERENCES blocks(pk), start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,metadata TEXT NOT NULL DEFAULT '{}')",
                'CREATE INDEX IF NOT EXISTS chunks_block ON chunks(block_pk)',
                'CREATE INDEX IF NOT EXISTS chunks_session ON chunks(session_id)',
                'CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, block_pk INTEGER UNIQUE NOT NULL REFERENCES blocks(pk) ON DELETE CASCADE,content_hash TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,error TEXT,priority_ns INTEGER NOT NULL DEFAULT 0,source_offset INTEGER NOT NULL DEFAULT 0)',
                'CREATE INDEX IF NOT EXISTS jobs_newest ON jobs(priority_ns DESC,source_offset DESC,id DESC)',
                ('CREATE TABLE IF NOT EXISTS vectors(rowid INTEGER PRIMARY KEY REFERENCES blocks(pk) ON DELETE CASCADE)' if vector_backend else f'CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(embedding float[{dimensions}])'),
            )
            for statement in statements:
                self.db.execute(statement)
            for key, value in (('embedding_identity', identity), ('chunking_identity', chunking), ('vector_backend', backend)):
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, value))
            if vector_backend:
                self.db.execute('CREATE TABLE IF NOT EXISTS vector_deletions(block_pk INTEGER PRIMARY KEY)')
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='running' AND attempts<?", (max_attempts,))
            self.db.execute("UPDATE jobs SET state='failed',error='Interrupted final attempt' WHERE state='running'")
        from .search_history import initialize
        initialize(self.db)

    def database_bytes(self):
        return database_bytes(self.path)

    def close(self):
        self.db.close()

    def checkpoint(self, session_id):
        row = self.db.execute('SELECT checkpoint FROM sessions WHERE id=?', (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def sessions(self):
        return [dict(json.loads(r['metadata']), checkpoint=json.loads(r['checkpoint'])) for r in self.db.execute('SELECT metadata,checkpoint FROM sessions')]

    def stats(self, selection=None):
        return _counts(self.db, selection)

    def _prune(self, candidates):
        for pk in set(candidates):
            if not self.db.execute('SELECT 1 FROM chunks WHERE block_pk=? LIMIT 1', (pk,)).fetchone():
                if self.vector_backend:
                    self.db.execute('INSERT OR IGNORE INTO vector_deletions VALUES(?)', (pk,))
                self.db.execute('DELETE FROM vectors WHERE rowid=?', (pk,))
                self.db.execute('DELETE FROM blocks WHERE pk=?', (pk,))

    def _delete_session(self, session_id):
        candidates = [r[0] for r in self.db.execute('SELECT DISTINCT block_pk FROM chunks WHERE session_id=?', (session_id,))]
        self.db.execute('DELETE FROM sessions WHERE id=?', (session_id,))
        self._prune(candidates)

    def reset_session(self, session_id):
        with self.db:
            self._delete_session(session_id)

    def reconcile_sessions(self, session_ids):
        keep = set(session_ids)
        with self.db:
            for row in self.db.execute('SELECT id FROM sessions').fetchall():
                if row[0] not in keep:
                    self._delete_session(row[0])

    def relocate_session(self, session_id, path, state):
        """Track moves even for disabled groups, without advancing the checkpoint."""
        row = self.db.execute('SELECT metadata FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row:
            old = json.loads(row[0])
            if old.get('path') == path and old.get('state') == state:
                return
            meta = dict(old, path=path, state=state)
            with self.db:
                self.db.execute('UPDATE sessions SET metadata=?,state=? WHERE id=?', (json.dumps(meta), state, session_id))

    def ingest(self, session, checkpoint, chunks):
        sid, state = session['id'], session.get('state', 'active')
        if state not in ('active', 'archived'):
            raise ValueError('Invalid session state')
        if len({c['id'] for c in chunks}) != len(chunks):
            raise ValueError('Duplicate chunk IDs')
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,state=excluded.state,checkpoint=excluded.checkpoint', (sid,json.dumps(session),state,json.dumps(checkpoint)))
            priority = int(checkpoint.get('mtime_ns', 0))
            self.db.execute('UPDATE jobs SET priority_ns=MAX(priority_ns,?) WHERE block_pk IN (SELECT block_pk FROM chunks WHERE session_id=?)', (priority,sid))
            candidates = []
            for chunk in chunks:
                text = chunk['text']
                digest = hashlib.sha256(text.encode()).hexdigest()
                old = self.db.execute('SELECT session_id,block_pk FROM chunks WHERE id=?', (chunk['id'],)).fetchone()
                if old and old[0] != sid:
                    raise ValueError('Chunk ID belongs to another session')
                self.db.execute('INSERT OR IGNORE INTO blocks(content_hash,text) VALUES(?,?)', (digest,text))
                block = self.db.execute('SELECT pk,text FROM blocks WHERE content_hash=?', (digest,)).fetchone()
                if block['text'] != text:
                    raise ValueError('Text hash collision')
                pk = block['pk']
                if old and old['block_pk'] != pk:
                    candidates.append(old['block_pk'])
                meta = {k:v for k,v in chunk.items() if k not in ('id','session_id','text','start_offset','end_offset','content_hash','text_hash')}
                self.db.execute('INSERT INTO chunks(id,session_id,block_pk,start_offset,end_offset,metadata) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET block_pk=excluded.block_pk,start_offset=excluded.start_offset,end_offset=excluded.end_offset,metadata=excluded.metadata', (chunk['id'],sid,pk,chunk['start_offset'],chunk['end_offset'],json.dumps(meta)))
                if not self.db.execute('SELECT 1 FROM vectors WHERE rowid=?', (pk,)).fetchone():
                    self.db.execute("INSERT INTO jobs(block_pk,content_hash,state,priority_ns,source_offset) VALUES(?,?,'pending',?,?) ON CONFLICT(block_pk) DO UPDATE SET priority_ns=MAX(jobs.priority_ns,excluded.priority_ns),source_offset=MAX(jobs.source_offset,excluded.source_offset)", (pk,digest,priority,chunk['start_offset']))
            self._prune(candidates)

    def claim_job(self, now=None, selection=None):
        now = time.time() if now is None else now
        states = _states(selection)
        if not states:
            return None
        placeholders = ','.join('?' for _ in states)
        with self.db:
            row = self.db.execute(f'''SELECT j.*,b.text FROM jobs j INDEXED BY jobs_newest
                JOIN blocks b ON b.pk=j.block_pk
                WHERE j.state IN ('pending','failed') AND j.attempts<? AND j.next_attempt<=?
                AND EXISTS(SELECT 1 FROM chunks c JOIN sessions s ON s.id=c.session_id
                    WHERE c.block_pk=j.block_pk AND s.state IN ({placeholders}) AND {_type_predicate(selection)})
                ORDER BY j.priority_ns DESC,j.source_offset DESC,j.id DESC LIMIT 1''', (self.max_attempts,now,*states)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (row['id'],))
            source = self.db.execute(f'SELECT c.id,c.session_id FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.block_pk=? AND s.state IN ({placeholders}) AND {_type_predicate(selection)} ORDER BY c.start_offset DESC LIMIT 1', (row['block_pk'],*states)).fetchone()
            return dict(row, attempts=row['attempts']+1,state='running',chunk_id=source['id'],session_id=source['session_id'])

    def complete_job(self, job_id, content_hash, vector):
        blob = self._vector(vector)
        with self.db:
            row = self.db.execute("SELECT j.block_pk FROM jobs j JOIN blocks b ON b.pk=j.block_pk WHERE j.id=? AND j.state='running' AND j.content_hash=? AND b.content_hash=j.content_hash", (job_id,content_hash)).fetchone()
            if row is None:
                return False
            self.db.execute('DELETE FROM vectors WHERE rowid=?', (row[0],))
            if self.vector_backend:
                block = self.db.execute('SELECT text,content_hash FROM blocks WHERE pk=?', (row[0],)).fetchone()
                self.vector_backend.put(row[0], block['text'], block['content_hash'], vector)
                self.db.execute('INSERT INTO vectors(rowid) VALUES(?)', (row[0],))
            else:
                self.db.execute('INSERT INTO vectors(rowid,embedding) VALUES(?,?)', (row[0],blob))
            self.db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            return True

    def flush_vector_deletions(self):
        if self.vector_backend:
            for row in self.db.execute('SELECT block_pk FROM vector_deletions').fetchall():
                self.vector_backend.delete(row[0])
                with self.db:
                    self.db.execute('DELETE FROM vector_deletions WHERE block_pk=?', (row[0],))

    def recover_vectors(self, revisions):
        """Repair split-store interruptions using durable jobs and revision checks."""
        with self.db:
            for pk, revision in self.db.execute('SELECT v.rowid,b.content_hash FROM vectors v JOIN blocks b ON b.pk=v.rowid').fetchall():
                if revisions.get(str(pk)) != revision:
                    self.db.execute('DELETE FROM vectors WHERE rowid=?', (pk,))
                    self.db.execute("INSERT OR IGNORE INTO jobs(block_pk,content_hash,state,priority_ns,source_offset) SELECT ?,?,'pending',COALESCE(MAX(json_extract(s.checkpoint,'$.mtime_ns')),0),COALESCE(MAX(c.start_offset),0) FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.block_pk=?", (pk,revision,pk))
            for pk in revisions:
                if not self.db.execute('SELECT 1 FROM blocks WHERE pk=?', (pk,)).fetchone():
                    self.db.execute('INSERT OR IGNORE INTO vector_deletions VALUES(?)', (pk,))
        self.flush_vector_deletions()

    def fail_job(self, job_id, error, now=None):
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT attempts FROM jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='failed',error=?,next_attempt=? WHERE id=?", (str(error)[:2000],now+min(3600,2**row[0]),job_id))

    def _vector(self, vector):
        import math
        if len(vector) != self.dimensions or not all(math.isfinite(v) for v in vector):
            raise ValueError('Invalid vector dimensions or values')
        return struct.pack(f'{self.dimensions}f', *vector)

    def search(self, vector, limit=10, state='all', selection=None):
        if state not in ('active','archived','all'):
            raise ValueError('Invalid state filter')
        states = tuple(s for s in _states(selection) if state=='all' or s==state)
        if not states or int(limit)<=0:
            return []
        placeholders = ','.join('?' for _ in states)
        if self.vector_backend:
            eligible = [r[0] for r in self.db.execute(f'SELECT DISTINCT c.block_pk FROM chunks c JOIN sessions s ON s.id=c.session_id JOIN vectors v ON v.rowid=c.block_pk WHERE s.state IN ({placeholders}) AND {_type_predicate(selection)}', states)]
            nearest = self.vector_backend.search(vector, eligible, int(limit))
            matches = []
            for pk, distance in nearest:
                rows = self.db.execute(f'''SELECT c.*,b.text,s.metadata AS session_metadata,s.state
                    FROM chunks c JOIN blocks b ON b.pk=c.block_pk JOIN sessions s ON s.id=c.session_id
                    WHERE c.block_pk=? AND s.state IN ({placeholders}) AND {_type_predicate(selection)} ORDER BY c.id''', (pk,*states))
                matches.extend(dict(json.loads(r['metadata']),id=r['id'],text=r['text'],session=json.loads(r['session_metadata']),state=r['state'],start_offset=r['start_offset'],end_offset=r['end_offset'],distance=distance,score=distance) for r in rows)
            return matches
        rows = self.db.execute(f'''WITH nearest AS (
            SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=? AND rowid IN (
                SELECT c.block_pk FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE s.state IN ({placeholders}) AND {_type_predicate(selection)}))
            SELECT c.*,b.text,s.metadata AS session_metadata,s.state,n.distance
            FROM nearest n JOIN blocks b ON b.pk=n.rowid JOIN chunks c ON c.block_pk=b.pk JOIN sessions s ON s.id=c.session_id
            WHERE s.state IN ({placeholders}) AND {_type_predicate(selection)} ORDER BY n.distance,b.pk,c.id''', (self._vector(vector),int(limit),*states,*states)).fetchall()
        # All source locations survive deduplication; k bounds unique blocks.
        return [dict(json.loads(r['metadata']),id=r['id'],text=r['text'],session=json.loads(r['session_metadata']),state=r['state'],start_offset=r['start_offset'],end_offset=r['end_offset'],distance=r['distance'],score=r['distance']) for r in rows]
