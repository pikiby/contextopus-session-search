"""Explicit resumable import from a read-only legacy SQLite catalog.

Partial target rows remain unavailable to the module while migration_state is
importing. Each 16-row target batch and its cursor share one <=5s transaction.
No source file, source vector, JSONL or legacy checkpoint is rewritten.
"""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path

TABLES = ('sessions','blocks','chunks','jobs','search_requests','search_results')


def fingerprint(source):
    result = {}
    for path in (source,Path(str(source)+'-wal')):
        if path.exists():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
            result[str(path)] = dict(size=path.stat().st_size,sha256=digest.hexdigest())
    return result


@contextmanager
def source_db(source):
    db = sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    try: yield db
    finally: db.close()


def preview(service, source):
    source = Path(source).expanduser().resolve()
    if not source.is_file(): raise ValueError('Legacy source must be an existing file')
    config = service.config()
    if not config: raise ValueError('Configure the destination first')
    marker = service._read_json('migration_state',None)
    if marker and marker.get('state') == 'completed': raise ValueError('A migration was already completed')
    if service.actor or service.enabled.is_set(): raise ValueError('Stop indexing and restart the module before importing')
    if not marker and service.db.execute('SELECT 1 FROM sessions LIMIT 1').fetchone():
        raise ValueError('Migration requires an empty destination')
    before = fingerprint(source)
    with source_db(source) as db:
        metadata = dict(db.execute('SELECT key,value FROM metadata'))
        if metadata.get('vector_backend','sqlite-vec') != 'sqlite-vec':
            raise ValueError('Expected a legacy sqlite-vec catalog')
        if metadata.get('chunking_identity') != f"conversation-blocks-v1:{config['block_tokens']}":
            raise ValueError('Legacy chunking differs; explicit source reindex is required')
        counts = {table:db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES}
        if 'block_pk' not in [row[1] for row in db.execute('PRAGMA table_info(chunks)')]:
            raise ValueError('Legacy catalog must already use deduplicated blocks')
    if before != fingerprint(source): raise ValueError('Legacy source changed during preview')
    plan = dict(source=str(source),destination='core.storage/session_search',files=before,
        counts=counts,config=config,profile=service.core.embeddings.profile(),
        source_embedding_identity=metadata.get('embedding_identity'),
        vectors='Recreate vectors with the exact Core profile; legacy vectors are never reused; terminal failed jobs retain retry limits until explicit recovery')
    plan['plan_id'] = hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    if marker and marker['plan_id'] != plan['plan_id']:
        raise ValueError('An incomplete migration belongs to another plan')
    return plan


def apply(service, plan, confirmation):
    if not isinstance(plan,dict) or not isinstance(plan.get('plan_id'),str) or confirmation != plan['plan_id']:
        raise ValueError('Exact approved migration plan_id required')
    if preview(service,plan['source']) != plan: raise ValueError('Migration inputs changed; preview again')
    source = Path(plan['source'])
    marker = service._read_json('migration_state',None)
    if marker is None:
        marker = dict(state='importing',plan_id=plan['plan_id'],table=0,offset=0)
        service._save_json('migration_state',marker)
    with source_db(source) as old:
        for table_index in range(marker['table'],len(TABLES)):
            table = TABLES[table_index]
            columns = [row[1] for row in old.execute(f'PRAGMA table_info({table})')]
            # Explicit closed table list and validated SQLite identifiers.
            if any(not column.replace('_','').isalnum() for column in columns):
                raise ValueError('Unsupported legacy column name')
            names = ','.join('"'+column+'"' for column in columns)
            offset = marker['offset'] if table_index == marker['table'] else 0
            while True:
                rows = old.execute(f'SELECT {names} FROM {table} ORDER BY rowid LIMIT 16 OFFSET ?', (offset,)).fetchall()
                if not rows: break
                with service.db:
                    for row in rows:
                        service.db.execute(f'INSERT INTO {table}({names}) VALUES({",".join("?" for column in columns)})',tuple(row))
                    offset += len(rows)
                    marker = dict(state='importing',plan_id=plan['plan_id'],table=table_index,offset=offset)
                    service._save_json('migration_state',marker)
            marker = dict(state='importing',plan_id=plan['plan_id'],table=table_index+1,offset=0)
            service._save_json('migration_state',marker)
    if fingerprint(source) != plan['files']:
        raise ValueError('Source changed during import; incomplete destination remains unavailable')
    # Queue every imported block; preserve existing retry/ordering information.
    last = int(marker.get('queued_through',0))
    while True:
        rows = service.db.execute('SELECT pk,content_hash FROM blocks WHERE pk>? ORDER BY pk LIMIT 16',(last,)).fetchall()
        if not rows: break
        with service.db:
            for row in rows:
                service.db.execute("INSERT OR IGNORE INTO jobs(block_pk,content_hash,state,priority_ns,source_offset) SELECT ?,?,'pending',COALESCE(MAX(json_extract(s.checkpoint,'$.mtime_ns')),0),COALESCE(MAX(c.start_offset),0) FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.block_pk=?", (row['pk'],row['content_hash'],row['pk']))
                last = row['pk']
            marker.update(queued_through=last)
            service._save_json('migration_state',marker)
    counts = {table:service.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES}
    if any(counts[table] != plan['counts'][table] for table in TABLES if table != 'jobs'):
        raise ValueError('Migration counts differ; incomplete destination remains unavailable')
    # Foreign-key enforcement runs on each inserted row. No SQL/RAG work shares
    # this transaction; only the durable visibility marker changes here.
    marker.update(state='completed',counts=counts)
    service._save_json('migration_state',marker)
    return dict(status='imported',counts=counts,queued=counts['jobs'],indexing=False,
                vectors_reused=False,source_preserved=True)
