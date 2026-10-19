"""Core SQL facade for the existing block/occurrence Store, with no local DB."""
import hashlib
import json
import re
import threading
from pathlib import Path
from .store import Store, _states, _type_predicate

TABLES = frozenset(('metadata', 'sessions', 'blocks', 'chunks', 'jobs', 'vectors',
                    'vector_deletions', 'search_requests', 'search_results',
                    'chunks_block', 'chunks_session', 'jobs_newest', 'search_requests_time'))
TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*")


def namespace(sql):
    # Rewrite only table positions and table-qualified references, never a
    # column called metadata or strings containing SQL/JSON names.
    matches = list(TOKEN.finditer(sql))
    edits = []
    for index, match in enumerate(matches):
        word = match.group()
        previous = matches[index-1].group().upper() if index else ''
        before = matches[index-2].group().upper() if index > 1 else ''
        table_position = previous in {'FROM','JOIN','INTO','UPDATE','REFERENCES'} or (previous == 'BY' and before == 'INDEXED')
        qualifier = sql[match.end():].lstrip().startswith('.')
        if word in TABLES and (table_position or qualifier):
            edits.append((match.start(),match.end(),'"{{module}}::'+word+'"'))
    for start,end,value in reversed(edits): sql = sql[:start]+value+sql[end:]
    return sql


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)
    def __iter__(self):
        return iter(self.values())


class Cursor:
    def __init__(self, rows=(), rowcount=-1, lastrowid=None):
        self.rows = iter(Row(row) for row in rows)
        self.rowcount, self.lastrowid = rowcount, lastrowid
    def fetchone(self): return next(self.rows, None)
    def fetchall(self): return list(self.rows)
    def __iter__(self): return self.rows


class SQLConnection:
    """Small DB-API subset; paged replies fit the child protocol frame limit."""
    def __init__(self, storage):
        self.storage, self.local = storage, threading.local()
    @property
    def client(self): return getattr(self.local, 'transaction', self.storage)
    def execute(self, sql, parameters=()):
        if sql == 'PRAGMA table_info(chunks)':
            return Cursor([{'cid': 0, 'name': 'block_pk'}])
        sql = namespace(sql)
        if sql.lstrip().upper().startswith(('SELECT', 'WITH')):
            return Cursor(self.query_rows(sql, parameters))
        result = self.client.execute(sql, parameters)
        return Cursor(rowcount=result['rowcount'], lastrowid=result.get('lastrowid'))
    def query_rows(self, sql, parameters):
        offset, size = 0, 64
        while True:
            try:
                result = self.client.query('SELECT * FROM (' + sql + ') LIMIT ? OFFSET ?',
                                           [*parameters, size, offset])
            except RuntimeError as exc:
                if size > 1 and ('1 MiB' in str(exc) or 'exceeds' in str(exc)):
                    size = max(1, size // 2)
                    continue
                raise
            rows = result['rows']
            yield from rows
            offset += len(rows)
            if len(rows) < size: break
    def executemany(self, sql, rows):
        for row in rows: self.execute(sql, row)
    def __enter__(self):
        if getattr(self.local, 'transaction', None) is not None:
            raise RuntimeError('Nested module SQL transaction')
        manager = self.storage.transaction(timeout_seconds=5)
        self.local.manager = manager
        self.local.transaction = manager.__enter__()
        return self
    def __exit__(self, *args):
        manager = self.local.manager
        try: return manager.__exit__(*args)
        finally:
            del self.local.transaction
            del self.local.manager
    def close(self): pass


class ModuleStore(Store):
    def __init__(self, core, model, dimensions, vector_backend, *, chunking, max_attempts=5):
        self.db = SQLConnection(core.storage)
        self.dimensions, self.max_attempts = dimensions, max_attempts
        self.vector_backend = vector_backend
        identity = json.dumps({'model': model, 'dimensions': dimensions}, sort_keys=True)
        existing = dict(self.db.execute('SELECT key,value FROM metadata'))
        expected = dict(embedding_identity=identity, chunking_identity=chunking, vector_backend='core-lancedb')
        for key, value in expected.items():
            if key in existing and existing[key] != value:
                raise ValueError('Embedding profile or chunking mismatch; explicit migration/reindex required')
        with self.db:
            for key, value in expected.items():
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, value))
            # A running reservation has no confirmed outcome, even on the last
            # attempt. Reclaim its slot; confirmed failed jobs/backoff stay intact.
            self.db.execute("UPDATE jobs SET state='pending',attempts=MAX(0,attempts-1) WHERE state='running'")
    def ingest(self, session, checkpoint, chunks):
        sid,state = session['id'],session.get('state','active')
        if state not in ('active','archived'): raise ValueError('Invalid session state')
        if len({chunk['id'] for chunk in chunks}) != len(chunks): raise ValueError('Duplicate chunk IDs')
        # File reading, hashing and JSON preparation precede the SQL transaction.
        prepared = []
        for chunk in chunks:
            meta = {key:value for key,value in chunk.items() if key not in ('id','session_id','text','start_offset','end_offset','content_hash','text_hash')}
            prepared.append((chunk,hashlib.sha256(chunk['text'].encode()).hexdigest(),json.dumps(meta)))
        session_json,checkpoint_json = json.dumps(session),json.dumps(checkpoint)
        priority = int(checkpoint.get('mtime_ns',0))
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,state=excluded.state,checkpoint=excluded.checkpoint',(sid,session_json,state,checkpoint_json))
            self.db.execute('UPDATE jobs SET priority_ns=MAX(priority_ns,?) WHERE block_pk IN (SELECT block_pk FROM chunks WHERE session_id=?)',(priority,sid))
            candidates = []
            for offset in range(0,len(prepared),32):
                batch = prepared[offset:offset+32]
                if not batch: continue
                ids = [item[0]['id'] for item in batch]
                old = {row['id']:row for row in self.db.execute('SELECT id,session_id,block_pk FROM chunks WHERE id IN ('+','.join('?' for _ in ids)+')',ids)}
                if any(row['session_id'] != sid for row in old.values()): raise ValueError('Chunk ID belongs to another session')
                self.db.execute('INSERT OR IGNORE INTO blocks(content_hash,text) VALUES '+','.join('(?,?)' for _ in batch),[value for chunk,digest,meta in batch for value in (digest,chunk['text'])])
                hashes = [item[1] for item in batch]
                blocks = {row['content_hash']:row for row in self.db.execute('SELECT pk,content_hash,text FROM blocks WHERE content_hash IN ('+','.join('?' for _ in hashes)+')',hashes)}
                pks = [blocks[digest]['pk'] for chunk,digest,meta in batch]
                indexed = {row[0] for row in self.db.execute('SELECT rowid FROM vectors WHERE rowid IN ('+','.join('?' for _ in pks)+')',pks)}
                occurrence_values,job_values = [],[]
                for chunk,digest,meta in batch:
                    block = blocks[digest];pk=block['pk']
                    if block['text'] != chunk['text']: raise ValueError('Text hash collision')
                    if chunk['id'] in old and old[chunk['id']]['block_pk'] != pk: candidates.append(old[chunk['id']]['block_pk'])
                    occurrence_values.extend((chunk['id'],sid,pk,chunk['start_offset'],chunk['end_offset'],meta))
                    if pk not in indexed: job_values.append((pk,digest,priority,chunk['start_offset']))
                self.db.execute('INSERT INTO chunks(id,session_id,block_pk,start_offset,end_offset,metadata) VALUES '+','.join('(?,?,?,?,?,?)' for _ in batch)+' ON CONFLICT(id) DO UPDATE SET block_pk=excluded.block_pk,start_offset=excluded.start_offset,end_offset=excluded.end_offset,metadata=excluded.metadata',occurrence_values)
                if job_values:
                    self.db.execute("INSERT INTO jobs(block_pk,content_hash,state,priority_ns,source_offset) VALUES "+','.join("(?,?,'pending',?,?)" for _ in job_values)+' ON CONFLICT(block_pk) DO UPDATE SET priority_ns=MAX(jobs.priority_ns,excluded.priority_ns),source_offset=MAX(jobs.source_offset,excluded.source_offset)',[value for row in job_values for value in row])
            self._prune(candidates)
    def _prune(self, candidates):
        ids = sorted(set(candidates))
        for offset in range(0,len(ids),256):
            batch = ids[offset:offset+256]
            parameters = ','.join('?' for _ in batch)
            orphan = 'SELECT b.pk FROM blocks b WHERE b.pk IN ('+parameters+') AND NOT EXISTS(SELECT 1 FROM chunks c WHERE c.block_pk=b.pk)'
            self.db.execute('INSERT OR IGNORE INTO vector_deletions SELECT pk FROM ('+orphan+')',batch)
            self.db.execute('DELETE FROM vectors WHERE rowid IN ('+orphan+')',batch)
            self.db.execute('DELETE FROM blocks WHERE pk IN ('+orphan+')',batch)
    def reconcile_sessions(self, session_ids):
        keep = set(session_ids)
        for row in self.db.execute('SELECT id FROM sessions').fetchall():
            if row[0] not in keep: self.reset_session(row[0])
    def database_bytes(self):
        # File ownership/path accounting belongs to Core, never open its DB.
        return None
    def complete_job(self, job_id, content_hash, vector):
        self._vector(vector)
        with self.db:
            row = self.db.execute("SELECT j.block_pk,b.text FROM jobs j JOIN blocks b ON b.pk=j.block_pk WHERE j.id=? AND j.state='running' AND j.content_hash=? AND b.content_hash=j.content_hash", (job_id,content_hash)).fetchone()
            if row is None: return False
            pk = row['block_pk']
            # Persist cleanup intent before any RAG write. The final SQL ack is
            # the only step that removes it, atomically with the vector marker.
            self.db.execute('INSERT OR IGNORE INTO vector_deletions VALUES(?)', (pk,))
        # A crash or failed acknowledgement leaves durable cleanup, then replay.
        # Neither provider/RAG work nor waiting occurs inside the transaction.
        self.vector_backend.put(pk, row['text'], content_hash, vector)
        with self.db:
            current = self.db.execute("SELECT 1 FROM jobs j JOIN blocks b ON b.pk=j.block_pk WHERE j.id=? AND j.state='running' AND j.content_hash=? AND b.content_hash=j.content_hash AND b.pk=?", (job_id,content_hash,pk)).fetchone()
            if current is None: return False
            self.db.execute('INSERT OR REPLACE INTO vectors(rowid) VALUES(?)', (pk,))
            self.db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            self.db.execute('DELETE FROM vector_deletions WHERE block_pk=?', (pk,))
        return True
    def recover_vectors(self, revisions):
        """Manual integrity repair from an explicitly supplied inventory only.

        The service never calls this helper during startup, search or restart.
        """
        # Reconcile bounded groups; a large catalog must not put thousands of
        # round-trips into one five-second writer transaction.
        rows = self.db.execute('SELECT v.rowid,b.content_hash FROM vectors v JOIN blocks b ON b.pk=v.rowid').fetchall()
        missing = [(row[0],row[1]) for row in rows if revisions.get(str(row[0])) != row[1]]
        for offset in range(0,len(missing),16):
            with self.db:
                for pk,revision in missing[offset:offset+16]:
                    self.db.execute('DELETE FROM vectors WHERE rowid=?',(pk,))
                    self.db.execute("INSERT OR IGNORE INTO jobs(block_pk,content_hash,state,priority_ns,source_offset) SELECT ?,?,'pending',COALESCE(MAX(json_extract(s.checkpoint,'$.mtime_ns')),0),COALESCE(MAX(c.start_offset),0) FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.block_pk=?",(pk,revision,pk))
        current = {str(row[0]) for row in self.db.execute('SELECT pk FROM blocks')}
        orphaned = [pk for pk in revisions if pk not in current]
        for offset in range(0,len(orphaned),32):
            with self.db:
                for pk in orphaned[offset:offset+32]:
                    self.db.execute('INSERT OR IGNORE INTO vector_deletions VALUES(?)',(pk,))
        self.flush_vector_deletions()
    def search(self, vector, limit=10, state='all', selection=None):
        if state not in ('active','archived','all'): raise ValueError('Invalid state filter')
        states = tuple(s for s in _states(selection) if state == 'all' or s == state)
        if not states or limit <= 0: return []
        placeholders = ','.join('?' for _ in states)
        valid = set()
        for row in self.db.execute(f'SELECT s.id,s.metadata,s.checkpoint FROM sessions s WHERE s.state IN ({placeholders}) AND {_type_predicate(selection)}', states):
            meta, cp = json.loads(row['metadata']), json.loads(row['checkpoint'])
            try:
                stat = Path(meta['path']).stat()
                if any(stat_value != cp.get(key) for key, stat_value in
                       (('inode',stat.st_ino),('size',stat.st_size),('mtime_ns',stat.st_mtime_ns))): continue
            except (OSError,KeyError): continue
            valid.add(row['id'])
        eligible = sorted({r['block_pk'] for r in self.db.execute('SELECT c.session_id,c.block_pk FROM chunks c JOIN vectors v ON v.rowid=c.block_pk') if r['session_id'] in valid})
        nearest = self.vector_backend.search(vector, eligible, int(limit))
        matches = []
        for pk, distance in nearest:
            for row in self.db.execute('SELECT c.*,b.text,s.metadata AS session_metadata,s.state FROM chunks c JOIN blocks b ON b.pk=c.block_pk JOIN sessions s ON s.id=c.session_id WHERE c.block_pk=? ORDER BY c.id', (pk,)):
                if row['session_id'] in valid:
                    matches.append(dict(json.loads(row['metadata']),id=row['id'],text=row['text'],session=json.loads(row['session_metadata']),state=row['state'],start_offset=row['start_offset'],end_offset=row['end_offset'],distance=distance,score=distance))
        return matches
