"""Session index actor hosted by Core; no separate model or vector database."""
import json
import queue
import sqlite3
import threading
from pathlib import Path

from contextopus.core.rag import DocumentKey, IndexDocument, RagIndex
from contextopus.core.embeddings import embedding_identity
from contextopus.core.operations import OperationSpec
from .indexer import Indexer
from .store import Store, _counts, database_bytes

MODULE = "session_search"
COLLECTION = "blocks"


def vector_revisions(index):
    """Compatibility inventory via bounded snapshot pages, never an unbounded API.

    The earlier in-process adapter still reconciles its complete catalog on
    startup. The primary folder service uses durable SQL jobs/outbox instead.
    """
    revisions, cursor = {}, None
    while True:
        page = index.vector_revisions_page(MODULE, COLLECTION, cursor=cursor)
        if page["conflicts"]:
            raise ValueError("Conflicting RAG revisions require repair before recovery")
        revisions.update(page["revisions"])
        cursor = page["next_cursor"]
        if cursor is None:
            return revisions


class CoreEmbedder:
    def __init__(self, index):
        self.config = index.profile()
        self.dimensions = self.config["profile"]["dimensions"]
        self.identity = json.dumps(embedding_identity(self.config), sort_keys=True)
        self.provider = index.resources.acquire(self.config)

    def count_tokens(self, text):
        return len(self.provider.tokenizer()(text, add_special_tokens=True, truncation=False)["input_ids"])

    def encode(self, texts):
        return RagIndex.vectors(self.provider, texts, self.dimensions, "document")

    def encode_query(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Query must be nonempty text")
        return RagIndex.vectors(self.provider, [text], self.dimensions, "query")[0]


class CoreVectors:
    def __init__(self, index, profile):
        self.index, self.profile = index, profile

    @staticmethod
    def key(pk):
        return DocumentKey(MODULE, COLLECTION, str(pk))

    def put(self, pk, text, revision, vector):
        self.index.put_vector(IndexDocument(self.key(pk), revision, text,
                              "session-search://blocks/" + str(pk), {"type": "session_block"}),
                              vector, profile=self.profile)

    def delete(self, pk):
        self.index.delete_document(self.key(pk))

    def search(self, vector, eligible, limit):
        rows = self.index.search_vector(vector, module=MODULE, collection=COLLECTION,
                                       object_ids=map(str, eligible), profile=self.profile, limit=limit)
        return [(int(row["object_id"]), row["_distance"]) for row in rows]


class SessionSource:
    """Read-only source adapter; shared blocks retain every source occurrence."""
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "catalog.sqlite3"

    def documents(self):
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT b.pk,b.text,b.content_hash,s.metadata,c.id,c.start_offset,c.end_offset FROM blocks b JOIN vectors v ON v.rowid=b.pk JOIN chunks c ON c.block_pk=b.pk JOIN sessions s ON s.id=c.session_id ORDER BY b.pk,c.id")
            groups = {}
            for pk, text, revision, session, chunk, start, end in rows:
                meta = json.loads(session)
                group = groups.setdefault(pk, dict(text=text, revision=revision, occurrences=[]))
                group["occurrences"].append(dict(session=meta, chunk_id=chunk, start_offset=start, end_offset=end))
        return [IndexDocument(CoreVectors.key(pk), g["revision"], g["text"],
                              g["occurrences"][0]["session"]["path"],
                              dict(type="session_block", occurrences=g["occurrences"],
                                   archived=all(x["session"]["state"] == "archived" for x in g["occurrences"])))
                for pk, g in groups.items()]

    def eligible_documents(self, rows):
        from .settings import read_selection
        selected = read_selection(self.directory / "sessions.json")
        requested = {str(r["object_id"]) for r in rows}
        result = {}
        for doc in self.documents():
            occurrences = [x for x in doc.metadata["occurrences"] if selected.get(x["session"]["state"], False) and x["session"].get("session_type", "unknown") in selected.get("types", ["main"]) and Path(x["session"]["path"]).is_file()]
            if doc.key.id in requested and occurrences:
                result[doc.key] = IndexDocument(doc.key, doc.revision, doc.text,
                    occurrences[0]["session"]["path"], dict(doc.metadata, occurrences=occurrences,
                    archived=all(x["session"]["state"] == "archived" for x in occurrences)))
        return result


class Runtime:
    def __init__(self, layout, resources):
        self.layout, self.resources = layout, resources
        self.directory = layout.root / "modules/session_search"
        self.path = self.directory / "catalog.sqlite3"
        self.config_path = self.directory / "runtime.json"
        self.index = RagIndex(layout, resources)
        self.source = SessionSource(self.directory)
        self.actor = None
        self.thread = None
        self.enabled = threading.Event()
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.RLock()
        self.error = None

    def config(self):
        if not self.config_path.exists():
            return None
        return json.loads(self.config_path.read_text())

    def configure(self, codex_home, block_tokens=512, autostart=False):
        if self.thread and self.thread.is_alive():
            raise ValueError("Close the runtime before changing its source")
        home = Path(codex_home).expanduser().resolve()
        if not home.is_dir() or block_tokens not in (512, 1024, 2048):
            raise ValueError("Existing Codex home and supported block size required")
        proposed = dict(codex_home=str(home), block_tokens=block_tokens, autostart=autostart)
        old = self.config()
        if self.path.exists() and old and any(old[k] != proposed[k] for k in ("codex_home", "block_tokens")):
            raise ValueError("Changing an indexed source or chunk size requires explicit migration")
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.config_path.with_suffix(".tmp")
        temp.write_text(json.dumps(proposed))
        temp.replace(self.config_path)
        selection = self.directory / "sessions.json"
        if not selection.exists():
            selection.write_text(json.dumps(dict(active=True, archived=False, types=["main"])))
        return proposed

    def status(self):
        result = dict(module=MODULE, integration="core", indexing=self.enabled.is_set(),
                      state="running" if self.enabled.is_set() else "stopped", error=self.error)
        if self.actor is not None:
            result.update(self.actor.cached_status())
        elif self.path.exists():
            from .settings import read_selection
            from .search_history import stats
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                result.update(stats=_counts(db, read_selection(self.directory / "sessions.json")),
                              search_metrics=stats(db), database_bytes=database_bytes(self.path))
        return result

    def _ensure_thread(self):
        with self.lock:
            if not self.config():
                raise ValueError("Configure session_search before starting it")
            if self.closed.is_set():
                raise RuntimeError("Runtime is closed")
            if not self.thread or not self.thread.is_alive():
                self.ready.clear()
                self.error = None
                self.thread = threading.Thread(target=self._run, name="session-search", daemon=True)
                self.thread.start()
        if not self.ready.wait(60):
            raise TimeoutError("Session index initialization is still running")
        if self.error:
            raise RuntimeError(self.error)

    def start_if_enabled(self):
        config = self.config()
        if config and config.get("autostart"):
            self.start()

    def selection(self, active, archived, types):
        from .session_types import TYPES
        if any(t not in TYPES for t in types):
            raise ValueError("Invalid session types")
        if not self.config():
            raise ValueError("Configure session_search first")
        value = dict(active=active, archived=archived, types=list(dict.fromkeys(types)))
        path = self.directory / "sessions.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)
        return value

    def metadata(self, tool, arguments):
        from . import server
        config = self.config()
        if not config:
            raise ValueError("Configure session_search first")
        arguments = dict(arguments, codex_home=config["codex_home"])
        if tool in ("session_list", "session_cleanup_preview"):
            filters = dict(arguments.get("filters") or {})
            filters.setdefault("session_types", ["main"])
            arguments["filters"] = filters
        return json.loads(server.TOOLS[tool]["handler"](arguments)[0]["text"])

    def start(self):
        self._ensure_thread()
        self.enabled.set()
        return self.status()

    def stop(self):
        self.enabled.clear()
        return self.status()

    def request(self, payload, timeout=60):
        # Searching opens existing data; it never enables background indexing.
        if not self.path.exists() and not self.enabled.is_set():
            raise ValueError("Session index has not been initialized")
        self._ensure_thread()
        response = queue.Queue(maxsize=1)
        self.actor.requests.put((payload, response))
        result = response.get(timeout=timeout)
        if not result.get("ok"):
            raise RuntimeError(result.get("error"))
        return result

    def _run(self):
        store = embedder = None
        try:
            config = self.config()
            # A file lock protects against a second Core owner using this catalog.
            from contextopus.core import locking
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "owner.lock").open("a") as lock:
                locking.flock(lock, locking.LOCK_EX | locking.LOCK_NB)
                embedder = CoreEmbedder(self.index)
                backend = CoreVectors(self.index, embedder.config)
                store = Store(self.path, embedder.identity, embedder.dimensions,
                              chunking=f"conversation-blocks-v1:{config['block_tokens']}", vector_backend=backend)
                store.recover_vectors(vector_revisions(self.index))
                self.actor = Indexer(config["codex_home"], store, embedder,
                    selection_path=self.directory / "sessions.json", block_tokens=config["block_tokens"])
                self.ready.set()
                while not self.closed.is_set():
                    store.flush_vector_deletions()
                    if not self.actor.step(indexing_enabled=self.enabled.is_set()):
                        self.closed.wait(0.05)
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
            self.enabled.clear()
        finally:
            self.ready.set()
            if store is not None:
                store.close()
            if embedder is not None:
                embedder.provider.close()
            self.actor = None

    def close(self):
        self.enabled.clear()
        self.closed.set()
        if self.thread:
            self.thread.join()


def register(app):
    runtime = app.resources.service((str(app.layout.root), MODULE), lambda: Runtime(app.layout, app.resources))
    app.index.register_source(MODULE, runtime.source)
    app.registry.register_module(MODULE, available=True, state="available", open_tui=None)

    def operation(name, kind, handler, properties=None, required=()):
        schema = dict(type="object", properties=properties or {}, additionalProperties=False, required=list(required))
        # Waiting on the actor must not hold the root lock it needs to write RAG.
        app.registry.register(OperationSpec("session_search." + name, kind, schema,
            lambda ctx, **kw: handler(**kw), guard_root=False))

    operation("status", "query", runtime.status)
    operation("configure", "execute", runtime.configure, dict(codex_home=dict(type="string"),
        block_tokens=dict(enum=[512, 1024, 2048]), autostart=dict(type="boolean")), ["codex_home"])
    operation("start", "execute", runtime.start)
    operation("stop", "execute", runtime.stop)
    operation("search", "query", lambda mode="semantic", **kw: runtime.request(dict(op="literal" if mode == "literal" else "search", track_history=True, **kw)),
        dict(query=dict(type="string", minLength=1), mode=dict(enum=["semantic", "literal"]), state=dict(enum=["all", "active", "archived"]),
             limit=dict(type="integer", minimum=1, maximum=200)), ["query"])
    operation("selection", "execute", runtime.selection, dict(active=dict(type="boolean"),
        archived=dict(type="boolean"), types=dict(type="array", items=dict(enum=["main", "subagent", "guardian", "exec", "unknown"]))), ["active", "archived", "types"])
    from .server import TOOLS
    import copy
    for tool in ("session_info", "session_resume_command", "session_list", "session_cleanup_preview"):
        schema = copy.deepcopy(TOOLS[tool]["inputSchema"])
        schema["properties"].pop("codex_home", None)
        app.registry.register(OperationSpec("session_search." + tool.removeprefix("session_"), "query", schema,
            lambda ctx, _tool=tool, **kw: runtime.metadata(_tool, kw), description=TOOLS[tool]["description"], guard_root=False))
    from .indexer import cleanup_timeout
    operation("cleanup_apply", "execute", lambda plan, confirmation: runtime.request(
        dict(op="session_cleanup_apply", plan=plan, confirmation=confirmation), timeout=cleanup_timeout(plan)),
        dict(plan=dict(type="object"), confirmation=dict(type="string")), ["plan", "confirmation"])
    from .migration import preview, apply
    operation("migration_preview", "query", lambda source: preview(runtime, source), dict(source=dict(type="string")), ["source"])
    operation("migration_apply", "execute", lambda plan, confirmation: apply(runtime, plan, confirmation),
              dict(plan=dict(type="object"), confirmation=dict(type="string")), ["plan", "confirmation"])
    return runtime
