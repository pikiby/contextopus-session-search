"""Persistent folder-module service. Core alone owns SQL, model and vectors."""
import hashlib
import json
import queue
import threading
from pathlib import Path
from .indexer import Indexer, cleanup_timeout
from .module_store import ModuleStore, SQLConnection

MODULE, COLLECTION = 'session_search', 'blocks'
DEFAULT_SELECTION = dict(active=True, archived=False, types=['main'])


def profile_identity(config):
    provider = {key:value for key,value in config['provider'].items()
                if key not in {'api_key_env','timeout_seconds','cache_folder','local_files_only'}}
    serialized = json.dumps(dict(adapter_version=1,provider=provider,profile=config['profile']),
                            sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    return 'sha256:' + hashlib.sha256(serialized.encode()).hexdigest()


class SharedEmbedder:
    def __init__(self, core):
        self.core, self.config = core, core.embeddings.profile()
        self.dimensions = self.config['profile']['dimensions']
        self.identity = json.dumps(profile_identity(self.config), sort_keys=True)
    def count_tokens(self, text):
        return self.core.embeddings.count_tokens(text=text, profile=self.config)
    def encode(self, texts):
        # A single vector response is bounded even at high dimensions; no batch
        # silently clips text or overruns the SDK's 1 MiB response frame.
        return [self.core.embeddings.embed_documents(texts=[text],profile=self.config)[0] for text in texts]
    def encode_query(self, text):
        if not isinstance(text,str) or not text.strip(): raise ValueError('Query must be nonempty text')
        return self.core.embeddings.embed_query(text=text,profile=self.config)


class SharedVectors:
    def __init__(self, core, profile): self.core, self.profile = core, profile
    @staticmethod
    def key(pk): return dict(collection=COLLECTION,id=str(pk))
    def put(self, pk, text, revision, vector):
        return self.core.rag.put_vector(document=dict(key=self.key(pk),revision=revision,text=text,
            location='session-search://blocks/'+str(pk),metadata=dict(type='session_block')),
            vector=list(vector),profile=self.profile)
    def delete(self, pk): return self.core.rag.delete_document(key=self.key(pk))
    def search(self, vector, eligible, limit):
        # Each disjoint partition contributes its top-k; global merge is exact
        # for the Core distance order and never truncates eligibility at 10k.
        nearest = []
        def fetch(ids):
            try:
                return self.core.rag.search_vector(collection=COLLECTION,
                    object_ids=[str(pk) for pk in ids],vector=list(vector),
                    profile=self.profile,limit=min(limit,len(ids)))
            except RuntimeError as exc:
                # Core includes vector/text fields in candidate responses. Split
                # the ID domain after an explicit overflow; reducing top-k alone
                # would silently omit globally relevant blocks.
                if len(ids)>1 and ('1 MiB' in str(exc) or 'exceeds' in str(exc)):
                    middle=len(ids)//2
                    return fetch(ids[:middle])+fetch(ids[middle:])
                raise
        for offset in range(0,len(eligible),2000):
            rows=fetch(eligible[offset:offset+2000])
            nearest.extend((int(row['object_id']),row['_distance']) for row in rows)
        return sorted(nearest,key=lambda row:(row[1],row[0]))[:limit]


class Service:
    def __init__(self, core):
        self.core, self.db = core, SQLConnection(core.storage)
        self.closed, self.enabled, self.wake = threading.Event(), threading.Event(), threading.Event()
        self.commands = queue.Queue()
        self.thread = None
        self.actor = self.store = None
        self.error = None
        self.model_name = None
        self._config = self._read_json('runtime_config', None)
        self._selection = self._read_json('session_selection', DEFAULT_SELECTION)
        from .store import _counts
        from .search_history import stats
        self._snapshot = dict(stats=_counts(self.db,self._selection),search_metrics=stats(self.db),database_bytes=None)
    def _read_json(self, key, default):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default.copy() if isinstance(default,dict) else default
    def _save_json(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,json.dumps(value)))
    def start(self):
        # Factory lifecycle start is distinct from enabling indexing.
        if self.thread and self.thread.is_alive(): return
        if self._config and self._config.get('autostart'): self.enabled.set()
        self.thread = threading.Thread(target=self._run,name='session-search',daemon=True)
        self.thread.start()
    def config(self): return dict(self._config) if self._config else None
    def settings(self): return dict(config=self.config(),selection=dict(self._selection))
    def status(self):
        result = self.actor.cached_status() if self.actor else dict(self._snapshot)
        result.update(module=MODULE,integration='core',indexing=self.enabled.is_set(),
            state='error' if self.error else 'running' if self.enabled.is_set() else 'stopped',
            error=self.error,worker_alive=bool(self.thread and self.thread.is_alive()),
            config=self.config(),session_selection=dict(self._selection),model_name=self.model_name)
        return result
    def _initialize(self):
        if self.actor is not None: return
        if not self._config: raise ValueError('Configure session_search first')
        migration = self._read_json('migration_state',None)
        if migration and migration.get('state') != 'completed':
            raise ValueError('An explicit migration is incomplete; resume migration_apply')
        embedder = SharedEmbedder(self.core)
        self.model_name = embedder.config['profile']['model']
        vectors = SharedVectors(self.core, embedder.config)
        self.store = ModuleStore(self.core, embedder.identity, embedder.dimensions, vectors,
            chunking=f"conversation-blocks-v1:{self._config['block_tokens']}")
        # Durable SQL jobs and deletion intents are sufficient for crash
        # recovery. Never request a full Core RAG inventory on connection.
        self.store.flush_vector_deletions()
        self.actor = Indexer(self._config['codex_home'],self.store,embedder,
            block_tokens=self._config['block_tokens'],selection_reader=lambda:dict(self._selection),
            read_bytes=16*1024)
    def _command(self, operation, arguments):
        if self.closed.is_set(): raise RuntimeError('Module service is closed')
        if not self.thread or not self.thread.is_alive(): raise RuntimeError('Module worker is not running')
        response = queue.Queue(maxsize=1)
        self.commands.put((operation,arguments,response))
        self.wake.set()
        try: ok, result = response.get(timeout=cleanup_timeout(arguments.get('plan')) if operation=='cleanup_apply' else 180)
        except queue.Empty: raise TimeoutError('Module worker has not completed the request') from None
        if not ok: raise RuntimeError(result)
        return result
    def _execute(self, operation, arguments):
        if operation == 'configure':
            home = Path(arguments['codex_home']).expanduser().resolve()
            block_tokens = arguments.get('block_tokens',512)
            if not home.is_dir() or block_tokens not in (512,1024,2048):
                raise ValueError('Existing Codex home and supported block size required')
            value = dict(codex_home=str(home),block_tokens=block_tokens,autostart=arguments.get('autostart',False))
            if self._config and any(self._config[key]!=value[key] for key in ('codex_home','block_tokens')):
                if self.db.execute('SELECT 1 FROM sessions LIMIT 1').fetchone() or self.actor:
                    raise ValueError('Changing indexed source/chunking requires explicit migration')
            self._save_json('runtime_config',value)
            self._config = value
            return value
        if operation == 'selection':
            from .session_types import TYPES
            if type(arguments['active']) is not bool or type(arguments['archived']) is not bool or not isinstance(arguments['types'],list) or any(t not in TYPES for t in arguments['types']):
                raise ValueError('Invalid session selection')
            value = dict(active=arguments['active'],archived=arguments['archived'],types=list(dict.fromkeys(arguments['types'])))
            self._save_json('session_selection',value)
            self._selection = value
            if self.actor:
                self.actor.selection = dict(value)
                self.actor.next_sweep = 0
                self.actor.dirty.clear()
                self.actor.status(force=True)
            else:
                from .store import _counts
                from .search_history import stats
                self._snapshot = dict(stats=_counts(self.db,self._selection),
                    search_metrics=stats(self.db),database_bytes=None)
            return value
        if operation == 'stop':
            self.enabled.clear()
            return self.status()
        if operation == 'migration_preview':
            from .module_migration import preview
            return preview(self,arguments['source'])
        if operation == 'migration_apply':
            from .module_migration import apply
            result = apply(self,arguments['plan'],arguments['confirmation'])
            from .store import _counts
            from .search_history import stats
            self._snapshot = dict(stats=_counts(self.db,self._selection),search_metrics=stats(self.db),database_bytes=None)
            return result
        self._initialize()
        if operation == 'start':
            self.error = None
            self.enabled.set()
            return self.status()
        if operation == 'search':
            # Construct the actor payload here: query arguments cannot inject op
            # or turn a query-bound dispatch into an arbitrary execute request.
            query = arguments['query']
            if not isinstance(query,str) or not query.strip(): raise ValueError('Query must be nonempty text')
            mode = arguments.get('mode','semantic')
            if mode not in ('semantic','literal'): raise ValueError('Invalid search mode')
            result = self.actor.handle_request(dict(op='literal' if mode=='literal' else 'search',
                query=query,state=arguments.get('state','all'),limit=arguments.get('limit',20),track_history=True))
            return dict(ok=True,**result)
        if operation == 'cleanup_apply':
            return dict(ok=True,**self.actor.handle_request(dict(op='session_cleanup_apply',
                plan=arguments['plan'],confirmation=arguments['confirmation'])))
        if operation == 'history':
            limit = arguments.get('limit',100)
            if type(limit) is not int or not 1 <= limit <= 200: raise ValueError('Invalid history limit')
            return dict(requests=[dict(row) for row in self.db.execute('SELECT * FROM search_requests ORDER BY started_at DESC,id DESC LIMIT ?', (limit,))])
        raise ValueError('Unsupported worker operation')
    def _run(self):
        try:
            if self._config and self.enabled.is_set():
                try: self._initialize()
                except Exception as exc: self.error = type(exc).__name__+': '+str(exc); self.enabled.clear()
            while not self.closed.is_set():
                try: operation,arguments,response = self.commands.get_nowait()
                except queue.Empty: pass
                else:
                    try: result = self._execute(operation,arguments)
                    except Exception as exc: response.put((False,type(exc).__name__+': '+str(exc)))
                    else: response.put((True,result))
                    continue
                if self.actor:
                    try:
                        self.store.flush_vector_deletions()
                        worked = self.actor.step(indexing_enabled=self.enabled.is_set())
                    except Exception as exc:
                        self.error = type(exc).__name__+': '+str(exc)
                        self.enabled.clear()
                        worked = False
                    if worked: continue
                self.wake.wait(.05)
                self.wake.clear()
        except Exception as exc:
            self.error = type(exc).__name__+': '+str(exc)
            self.enabled.clear()
        finally:
            if self.actor: self._snapshot = self.actor.cached_status()
            if self.store: self.store.close()
            self.actor = self.store = None
            while True:
                try: _,_,response = self.commands.get_nowait()
                except queue.Empty: break
                response.put((False,'Module worker stopped'))
    def dispatch(self, operation, arguments, context=None):
        name = operation.removeprefix(MODULE+'.')
        if name == 'status': return self.status()
        if name == 'settings': return self.settings()
        if name == 'open_tui':
            from .tui_core_backend import launch_descriptor
            return launch_descriptor(__file__,context)
        if name in ('info','resume_command','list','cleanup_preview'):
            from . import server
            if not self._config: raise ValueError('Configure session_search first')
            arguments = dict(arguments,codex_home=self._config['codex_home'])
            if name in ('list','cleanup_preview'):
                filters = dict(arguments.get('filters') or {})
                filters.setdefault('session_types',['main'])
                arguments['filters'] = filters
            return json.loads(server.TOOLS['session_'+name]['handler'](arguments)[0]['text'])
        if name not in {'configure','selection','start','stop','search','history','cleanup_apply','migration_preview','migration_apply'}:
            raise ValueError('Unsupported operation: '+operation)
        return self._command(name,arguments)
    def close(self):
        self.enabled.clear()
        self.closed.set()
        self.wake.set()
        if self.thread: self.thread.join(timeout=3)


def create_service(core): return Service(core)
