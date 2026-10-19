"""Explicit legacy catalog import. Source is read-only; vectors are requeued."""
import hashlib
import json
import sqlite3
from pathlib import Path


def preview(runtime, source):
    source = Path(source).expanduser().resolve()
    if not source.is_file() or runtime.path.exists():
        raise ValueError("Source must exist and destination must not exist")
    if not runtime.config():
        raise ValueError("Configure the destination source first")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("vector_backend", "sqlite-vec") != "sqlite-vec":
            raise ValueError("Expected a legacy sqlite-vec catalog")
        if metadata.get("chunking_identity") != f"conversation-blocks-v1:{runtime.config()['block_tokens']}":
            raise ValueError("Legacy chunking differs; explicit source reindex is required")
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sessions", "blocks", "chunks", "jobs", "search_requests", "search_results")}
    files = {str(p): [p.stat().st_size, p.stat().st_mtime_ns] for p in
             (source, Path(str(source)+"-wal")) if p.exists()}
    plan = dict(source=str(source), destination=str(runtime.path), files=files,
                counts=counts, config=runtime.config(), profile=runtime.index.profile(),
                vectors="recreate with the Core profile; do not reuse legacy vectors")
    plan["plan_id"] = hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
    return plan


def apply(runtime, plan, confirmation):
    if not isinstance(plan, dict) or confirmation != plan.get("plan_id"):
        raise ValueError("Exact approved migration plan_id required")
    if runtime.thread and runtime.thread.is_alive():
        raise ValueError("Close the module runtime before importing")
    from contextopus.core import locking
    from contextopus.core.embeddings import embedding_identity
    from .store import Store
    from .core_module import CoreVectors
    source = Path(plan["source"])
    # Cooperate with the old indexer's exclusive ownership lock.
    with Path(str(source)+".lock").open("a") as lock:
        locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
        if preview(runtime, source) != plan:
            raise ValueError("Migration inputs changed; preview again")
        temporary = runtime.path.with_name("import.sqlite3")
        if temporary.exists():
            raise ValueError("An incomplete import already exists; inspect it first")
        profile = runtime.index.profile()
        store = Store(temporary, json.dumps(embedding_identity(profile),sort_keys=True),
                      profile["profile"]["dimensions"],
                      chunking=f"conversation-blocks-v1:{runtime.config()['block_tokens']}",
                      vector_backend=CoreVectors(runtime.index, profile))
        try:
            with sqlite3.connect(source.as_uri()+"?mode=ro",uri=True) as old, store.db:
                old.execute("BEGIN")
                for table in ("sessions", "blocks", "chunks", "search_requests", "search_results"):
                    columns = [r[1] for r in store.db.execute(f"PRAGMA table_info({table})")]
                    names = ",".join(columns)
                    store.db.executemany(f"INSERT INTO {table}({names}) VALUES({','.join('?' for c in columns)})",
                                         old.execute(f"SELECT {names} FROM {table}"))
                # Refresh provenance on the next file read, including old checkpoint tails.
                for sid, meta, cp in store.db.execute('SELECT id,metadata,checkpoint FROM sessions').fetchall():
                    value = json.loads(cp)
                    value["exhausted"] = False
                    store.db.execute('UPDATE sessions SET checkpoint=? WHERE id=?', (json.dumps(value), sid))
                store.db.execute("""INSERT INTO jobs(block_pk,content_hash,state,priority_ns,source_offset)
                    SELECT b.pk,b.content_hash,'pending',COALESCE(MAX(json_extract(s.checkpoint,'$.mtime_ns')),0),COALESCE(MAX(c.start_offset),0)
                    FROM blocks b JOIN chunks c ON c.block_pk=b.pk JOIN sessions s ON s.id=c.session_id GROUP BY b.pk""")
                if store.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Imported catalog has broken references")
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            store.close()
        temporary.replace(runtime.path)
    return dict(status="imported", counts=plan["counts"], queued=plan["counts"]["blocks"], indexing=False)
