# Non-English strings are intentional Unicode/UTF-8 search and JSONL fixtures.
"""Folder module acceptance with real Core SQL/RAG/child and fake embeddings."""
import json
import os
import socket
import threading
from pathlib import Path
import runpy
import time
import pytest
from contextopus.startup import Application
from contextopus.core.resources import ProcessResources
from contextopus.core.embeddings import default_config
from contextopus.core.chunking import default_chunking
from session_search.module_store import namespace
from session_search.module_service import SharedVectors

ROOT = Path(__file__).resolve().parents[1]

class Provider:
    def __init__(self,config): self.calls=[]
    def embed(self,texts,purpose='document'):
        self.calls.append((list(texts),purpose))
        return [[1.,0.] if 'привет' in text or 'hello' in text else [0.,1.] for text in texts]
    def tokenizer(self): return lambda text,**kw:dict(input_ids=list(range(len(text.split())+1)))
    def close(self): pass

def write(home,sid='one',text='hello',source='cli',state='sessions'):
    directory=home/state; directory.mkdir(parents=True,exist_ok=True)
    path=directory/f'rollout-{sid}.jsonl'
    rows=[dict(type='session_meta',payload=dict(id=sid,cwd='/example',source=source)),
          dict(type='response_item',payload=dict(type='message',role='user',content=[dict(type='input_text',text=text)]))]
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return path

def wait(app,predicate,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        status=app.query('session_search.status')
        assert status['error'] is None,status
        if predicate(status): return status
        time.sleep(.05)
    raise AssertionError(status)

@pytest.fixture
def legacy_adapter(tmp_path,monkeypatch):
    # The old adapter is genuinely available and responds over an isolated Unix
    # socket. No production executable, daemon or user index is accessed.
    if os.name == 'nt': pytest.skip('Legacy adapter fixture requires Unix sockets')
    binary=tmp_path/'bin/session-search'
    binary.parent.mkdir();binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(binary.parent)+os.pathsep+os.environ.get('PATH',''))
    endpoint=tmp_path/'legacy.sock'
    monkeypatch.setenv('CODEX_SESSION_INDEX_SOCKET',str(endpoint))
    server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    server.bind(str(endpoint));server.listen();server.settimeout(.1)
    stopped=threading.Event()
    requests=[]
    def serve():
        while not stopped.is_set():
            try: connection,_=server.accept()
            except socket.timeout: continue
            with connection,connection.makefile('rb') as stream:
                request=json.loads(stream.readline());requests.append(request)
                connection.sendall(json.dumps(dict(ok=True,implementation='legacy',query=request.get('query'))).encode()+b'\n')
    thread=threading.Thread(target=serve);thread.start()
    # Pure Core has no built-in compatibility adapter. Keep the old daemon
    # fixture available to prove installation/restart never accidentally use it.
    try:
        from contextopus.modules.session_search import SessionSearch
    except ModuleNotFoundError:
        SessionSearch = None
    if SessionSearch is not None:
        assert SessionSearch().available()
    try: yield dict(binary=binary,requests=requests)
    finally:
        stopped.set();thread.join(timeout=2);server.close()
        assert not thread.is_alive()


@pytest.fixture
def setup(tmp_path,legacy_adapter):
    module=tmp_path/'root/modules/session_search'
    runpy.run_path(str(ROOT/'scripts/build-module.py'))['build'](module)
    created=[]
    def factory(config):
        provider=Provider(config);created.append(provider);return provider
    resources=ProcessResources(factory)
    app=Application(tmp_path/'root',resources=resources,discover=False)
    cfg=default_config();cfg['profile']['dimensions']=2
    app.index.embeddings=cfg;app.index.chunking=default_chunking()
    app.index._generic_table(create=True)
    if 'session_search.search' in app.registry.describe()['operations']['query']:
        assert app.query('session_search.search',dict(query='before replacement'))==dict(ok=True,implementation='legacy',query='before replacement')
    else:
        with pytest.raises(ValueError,match='Unknown operation'):
            app.query('session_search.search',dict(query='before installation'))
    resources.start_services()
    assert app.execute('core.module_install',dict(module='session_search'))['state']=='enabled'
    status=app.query('session_search.status')
    assert status['integration']=='core' and status['worker_alive']
    assert legacy_adapter['binary'].is_file()
    home=tmp_path/'codex';home.mkdir()
    try: yield app,home,created
    finally: app.close();resources.close()

def test_namespace_preserves_columns_and_literals():
    sql="SELECT s.metadata,metadata,json_extract(s.metadata,'$.sessions') FROM sessions s JOIN chunks c ON c.session_id=s.id"
    value=namespace(sql)
    assert 's.metadata,metadata' in value and "'$.sessions'" in value
    assert 'FROM "{{module}}::sessions"' in value
    assert 'JOIN "{{module}}::chunks"' in value
    assert namespace('INSERT INTO sessions(id,metadata,state,checkpoint) VALUES(?,?,?,?)').startswith('INSERT INTO "{{module}}::sessions"(id,metadata,')
    assert '"{{module}}::jobs".priority_ns' in namespace('UPDATE jobs SET priority_ns=MAX(jobs.priority_ns,?)')

def test_real_child_dedup_search_history_and_restart(setup):
    app,home,created=setup
    first=write(home);write(home,'two')
    app.execute('session_search.configure',dict(codex_home=str(home)))
    app.execute('session_search.start')
    status=wait(app,lambda s:s['stats']['vectors']==1 and not s['index_pending'])
    assert status['stats']['sessions']==2 and len(created)==1
    app.execute('session_search.stop')
    matches=app.query('session_search.search',dict(query='hello',limit=1))['matches']
    assert {m['session']['id'] for m in matches}=={'one','two'}
    history=app.query('session_search.history')['requests']
    assert len(history)==1 and history[0]['result_count']==2 and history[0]['state']=='completed'
    assert not app.query('session_search.status')['indexing']
    app.execute('core.module_disable',dict(module='session_search'))
    app.execute('core.module_enable',dict(module='session_search'))
    app.module_manager.start_services()
    status=app.query('session_search.status')
    assert status['stats']['vectors']==1 and status['stats']['sessions']==2
    assert app.query('session_search.settings')['config']['codex_home']==str(home)
    app.execute('session_search.start')
    first.unlink()
    # Restart scan checks source deletion and drops only its occurrence.
    app.execute('core.module_disable',dict(module='session_search'))
    app.execute('core.module_enable',dict(module='session_search'))
    app.module_manager.start_services();app.execute('session_search.start')
    wait(app,lambda s:s['stats']['sessions']==1 and not s['index_pending'])
    assert len(app.query('session_search.search',dict(query='hello'))['matches'])==1
    with pytest.raises(Exception):
        app.query('session_search.search',dict(query='hello',op='session_cleanup_apply'))


def test_real_child_source_rewrite_reindexes_rag_and_search(setup):
    app,home,created=setup
    write(home,'rewrite','hello original')
    app.execute('session_search.configure',dict(codex_home=str(home)))
    app.execute('session_search.start')
    wait(app,lambda s:s['stats']['vectors']==1 and not s['index_pending'])
    assert 'hello original' in app.query('session_search.search',dict(query='original'))['matches'][0]['text']
    core,access=sdk(app)
    old=core.rag.vector_revisions_page(collection='blocks',limit=10)
    assert len(old['revisions'])==1 and old['next_cursor'] is None
    app.execute('session_search.stop')
    write(home,'rewrite','replacement changed conversation')
    # Source identity changes immediately invalidate stale search results.
    assert not app.query('session_search.search',dict(query='original'))['matches']
    app.execute('core.module_disable',dict(module='session_search'))
    app.execute('core.module_enable',dict(module='session_search'))
    app.module_manager.start_services();app.execute('session_search.start')
    wait(app,lambda s:s['stats']['vectors']==1 and not s['index_pending'])
    app.execute('session_search.stop')
    matches=app.query('session_search.search',dict(query='replacement'))['matches']
    assert [row['session']['id'] for row in matches]==['rewrite']
    assert 'changed conversation' in matches[0]['text']
    assert all('original' not in row['text'] for row in matches)
    current=core.rag.vector_revisions_page(collection='blocks',limit=10)
    assert len(current['revisions'])==1 and current['next_cursor'] is None
    assert current['revisions']!=old['revisions']
    assert sum(len(texts) for provider in created for texts,purpose in provider.calls if purpose=='document')==2
    access.close()

def test_main_and_archived_type_filters_before_top_k(setup):
    app,home,_=setup
    write(home,'main','hello');write(home,'child','hello',{'subagent':{'thread_spawn':{'parent_thread_id':'main'}}})
    write(home,'archived','hello',state='archived_sessions')
    app.execute('session_search.configure',dict(codex_home=str(home)))
    app.execute('session_search.start')
    wait(app,lambda s:s['stats']['vectors']==1 and not s['index_pending'])
    assert [m['session']['id'] for m in app.query('session_search.search',dict(query='hello',limit=1))['matches']]==['main']
    app.execute('session_search.selection',dict(active=True,archived=False,types=['subagent']))
    wait(app,lambda s:s['stats']['sessions']==1 and not s['index_pending'])
    matches=app.query('session_search.search',dict(query='hello',limit=1))['matches']
    assert [m['session']['id'] for m in matches]==['child']
    assert matches[0]['session']['parent_thread_id']=='main'
    app.execute('session_search.selection',dict(active=False,archived=True,types=['main']))
    wait(app,lambda s:s['stats']['sessions']==1 and not s['index_pending'])
    assert [m['session']['id'] for m in app.query('session_search.search',dict(query='hello',limit=1))['matches']]==['archived']

def test_large_eligible_partition_exact_global_merge():
    from types import SimpleNamespace
    calls=[]
    def search_vector(**arguments):
        ids=arguments['object_ids'];calls.extend(ids)
        return [dict(object_id=ident,_distance=-int(ident)) for ident in sorted(ids,key=int,reverse=True)[:arguments['limit']]]
    vectors=SharedVectors(SimpleNamespace(rag=SimpleNamespace(search_vector=search_vector)),{})
    result=vectors.search([1.,0.],list(range(12001)),3)
    assert result==[(12000,-12000),(11999,-11999),(11998,-11998)]
    assert len(calls)==12001 and len(set(calls))==12001


def sdk(app):
    from contextopus.core.module_management.core_access import ModuleCoreAccess
    from contextopus.core.module_management.child import StorageClient,NamespaceClient
    access=ModuleCoreAccess(app,'session_search',storage_declared=True,rag_declared=True)
    class SDK:
        def call(self,method,arguments): return access.call(method,arguments)
    value=SDK();value.storage=StorageClient(value,'storage')
    value.embeddings=NamespaceClient(value,'embeddings');value.rag=NamespaceClient(value,'rag')
    return value,access

def store_for(app):
    from session_search.module_service import SharedEmbedder
    from session_search.module_store import ModuleStore
    core,access=sdk(app);embedder=SharedEmbedder(core)
    store=ModuleStore(core,embedder.identity,embedder.dimensions,SharedVectors(core,embedder.config),chunking='conversation-blocks-v1:512')
    return store,core,access

def ingest_one(store,home,sid='one',text='hello'):
    path=write(home,sid,text);stat=path.stat()
    session=dict(id=sid,path=str(path),state='active',session_type='main')
    cp=dict(mtime_ns=stat.st_mtime_ns,size=stat.st_size,inode=stat.st_ino,offset=stat.st_size)
    store.ingest(session,cp,[dict(id=sid+'-chunk',text=text,start_offset=0,end_offset=stat.st_size)])
    return session,cp

def test_sdk_store_stale_completion_recovery_and_deletion_outbox(setup):
    app,home,_=setup
    store,core,access=store_for(app)
    try:
        ingest_one(store,home)
        job=store.claim_job()
        original=store.vector_backend.put
        def race(*args):
            original(*args)
            store.reset_session('one')
        store.vector_backend.put=race
        assert store.complete_job(job['id'],job['content_hash'],[1.,0.]) is False
        assert store.db.execute('SELECT COUNT(*) FROM vector_deletions').fetchone()[0]==1
        delete=store.vector_backend.delete
        store.vector_backend.delete=lambda pk:(_ for _ in ()).throw(RuntimeError('simulated RAG failure'))
        with pytest.raises(RuntimeError): store.flush_vector_deletions()
        assert store.db.execute('SELECT COUNT(*) FROM vector_deletions').fetchone()[0]==1
        store.vector_backend.delete=delete;store.flush_vector_deletions()
        lookup=core.rag.vector_revisions(collection='blocks',object_ids=[str(job['block_pk'])])
        assert lookup['revisions']=={} and lookup['missing']==[str(job['block_pk'])] and not lookup['conflicts']
        store.vector_backend.put=original
        ingest_one(store,home)
        job=store.claim_job()
        # Simulate the crash after RAG commit before SQL acknowledgement.
        original(job['block_pk'],job['text'],job['content_hash'],[1.,0.])
        assert store.stats()['vectors']==0
        store.close();access.close()
        store,core,access=store_for(app)
        lookup=core.rag.vector_revisions(collection='blocks',object_ids=[str(job['block_pk'])])
        assert not lookup['conflicts']
        store.recover_vectors(lookup['revisions'])
        job=store.claim_job();assert job and job['attempts']==1
        assert store.complete_job(job['id'],job['content_hash'],[1.,0.])
        assert store.stats()['vectors']==1
        core.rag.delete_document(key=dict(collection='blocks',id=str(job['block_pk'])))
        lookup=core.rag.vector_revisions(collection='blocks',object_ids=[str(job['block_pk'])])
        assert not lookup['conflicts']
        store.recover_vectors(lookup['revisions'])
        assert store.stats()['vectors']==0 and store.stats()['jobs']['pending']==1
    finally:store.close();access.close()

def test_sdk_store_changed_and_missing_sources_are_ineligible(setup):
    app,home,_=setup
    store,core,access=store_for(app)
    try:
        session,cp=ingest_one(store,home)
        job=store.claim_job();store.complete_job(job['id'],job['content_hash'],[1.,0.])
        assert store.search([1.,0.])
        path=Path(session['path']);path.write_text(path.read_text()+'{}\n')
        assert not store.search([1.,0.])
        path.unlink()
        assert not store.search([1.,0.])
        with pytest.raises(ValueError,match='profile or chunking mismatch'):
            from session_search.module_store import ModuleStore
            ModuleStore(core,'other-profile',2,store.vector_backend,chunking='conversation-blocks-v1:512')
    finally:store.close();access.close()

def test_sql_reply_paging_reduces_explicit_frame_errors_without_truncation():
    from session_search.module_store import SQLConnection
    class Storage:
        calls=[]
        def query(self,sql,parameters):
            size,offset=parameters[-2:];self.calls.append((size,offset))
            if size>8: raise RuntimeError('Core result exceeds 1 MiB')
            return dict(rows=[dict(id=n) for n in range(offset,min(offset+size,257))])
    storage=Storage();db=SQLConnection(storage)
    assert [row['id'] for row in db.execute('SELECT id FROM sessions')]==list(range(257))
    assert storage.calls[:4]==[(64,0),(32,0),(16,0),(8,0)]

def legacy(path,count=22,home=None):
    import hashlib,sqlite3
    schema=(ROOT/'module/migrations/001-schema.sql').read_text().replace('{{module}}::','')
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute('INSERT INTO metadata VALUES(?,?)',('vector_backend','sqlite-vec'))
        db.execute('INSERT INTO metadata VALUES(?,?)',('chunking_identity','conversation-blocks-v1:512'))
        for n in range(1,count+1):
            sid='s'+str(n);text='hello '+sid;revision=hashlib.sha256(text.encode()).hexdigest()
            source_path='/fixture/'+sid
            checkpoint=dict(offset=n,mtime_ns=n,reader_state=dict(tail='retained'))
            if home is not None:
                source=write(home,sid,text);stat=source.stat();source_path=str(source)
                checkpoint.update(offset=stat.st_size,size=stat.st_size,mtime_ns=stat.st_mtime_ns,inode=stat.st_ino,
                    path=str(source),title='',state='active',provenance_version=1,exhausted=True)
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(sid,json.dumps(dict(id=sid,path=source_path,state='active',session_type='main')),'active',json.dumps(checkpoint)))
            db.execute('INSERT INTO blocks VALUES(?,?,?)',(n,revision,text))
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?)',(n,'c'+sid,sid,n,0,n,json.dumps(dict(sources=[dict(char_start=0,char_end=n)]))))
        db.execute("INSERT INTO jobs(id,block_pk,content_hash,state,attempts,next_attempt,error,priority_ns,source_offset) SELECT 7,pk,content_hash,'failed',5,123,'old failure',99,12 FROM blocks WHERE pk=1")
        db.execute("INSERT INTO search_requests(id,started_at,tool,parameters,state) VALUES('request',1,'session_search','{}','started')")

def test_explicit_migration_resumes_after_interruption_and_preserves_legacy(setup,tmp_path):
    import hashlib
    from session_search.module_service import Service
    app,home,_=setup
    source=tmp_path/'legacy.sqlite3';legacy(source,home=home)
    import sqlite3
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as legacy_db:
        checkpoint=legacy_db.execute('SELECT checkpoint FROM sessions WHERE id=?',('s1',)).fetchone()[0]
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    core,access=sdk(app);service=Service(core);service.start()
    try:
        service.dispatch('session_search.configure',dict(codex_home=str(home)))
        plan=service.dispatch('session_search.migration_preview',dict(source=str(source)))
        execute=core.storage.execute;failed=False
        def fail(sql,parameters=(),**kwargs):
            nonlocal failed
            if not failed and 'INSERT INTO "{{module}}::chunks"' in sql:
                failed=True;raise RuntimeError('simulated disconnect')
            return execute(sql,parameters,**kwargs)
        core.storage.execute=fail
        with pytest.raises(RuntimeError,match='simulated disconnect'):
            service.dispatch('session_search.migration_apply',dict(plan=plan,confirmation=plan['plan_id']))
        assert service._read_json('migration_state',None)['state']=='importing'
        with pytest.raises(RuntimeError,match='incomplete'):
            service.dispatch('session_search.start',{})
        service.close();access.close()
        core,access=sdk(app);service=Service(core);service.start()
        assert service.config()['codex_home']==str(home)
        with pytest.raises(RuntimeError,match='plan_id'):
            service.dispatch('session_search.migration_apply',dict(plan=plan,confirmation='wrong'))
        result=service.dispatch('session_search.migration_apply',dict(plan=plan,confirmation=plan['plan_id']))
        assert result['counts']['sessions']==22 and result['queued']==22
        assert service.status()['stats']['sessions']==22
        assert service.status()['indexing'] is False
        row=service.db.execute('SELECT attempts,next_attempt,error,priority_ns,source_offset FROM jobs WHERE id=7').fetchone()
        assert list(row)==[5,123.,'old failure',99,12]
        assert service.db.execute('SELECT checkpoint FROM sessions WHERE id=?',('s1',)).fetchone()[0]==checkpoint
        assert service._read_json('migration_state',None)['state']=='completed'
        ids=[str(n) for n in range(1,23)]
        assert core.rag.vector_revisions(collection='blocks',object_ids=ids)['revisions']=={}
        service.dispatch('session_search.start',{})
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            status=service.status()
            assert status['error'] is None,status
            if status['stats'].get('vectors')==21 and status['stats'].get('jobs')=={'failed':1} and not status.get('errors'): break
            time.sleep(.05)
        else: raise AssertionError(status)
        service.dispatch('session_search.stop',{})
        lookup=core.rag.vector_revisions(collection='blocks',object_ids=ids)
        assert len(lookup['revisions'])==21 and lookup['missing']==['1'] and not lookup['conflicts']
        assert all(row['state']=='failed' for row in service.db.execute('SELECT state FROM jobs'))
        assert service.dispatch('session_search.search',dict(query='hello',limit=30))['matches']
        assert list(service.db.execute('SELECT attempts,next_attempt,error FROM jobs WHERE id=7').fetchone())==[5,123.,'old failure']
        assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    finally:service.close();access.close()


def test_vector_frame_overflow_splits_domain_preserving_top_k():
    from types import SimpleNamespace
    calls=[]
    def search_vector(**arguments):
        ids=arguments['object_ids'];calls.append(list(ids))
        if min(len(ids),arguments['limit'])>4: raise RuntimeError('Core result exceeds 1 MiB')
        return [dict(object_id=ident,_distance=-int(ident)) for ident in sorted(ids,key=int,reverse=True)[:arguments['limit']]]
    backend=SharedVectors(SimpleNamespace(rag=SimpleNamespace(search_vector=search_vector)),{})
    assert backend.search([1.,0.],list(range(101)),11)==[(n,-n) for n in range(100,89,-1)]
    assert len(calls)>1


def test_real_owner_restart_keeps_folder_selection_while_disabled(setup,legacy_adapter):
    app,home,created=setup
    write(home,'restart','hello привет')
    app.execute('session_search.configure',dict(codex_home=str(home)))
    app.execute('session_search.start')
    wait(app,lambda status:status['stats']['vectors']==1 and not status['index_pending'])
    assert app.query('session_search.search',dict(query='привет'))['matches']
    calls_before=len(legacy_adapter['requests'])
    app.execute('session_search.stop')
    root=app.layout.root
    first=app.module_manager.runtime.services['session_search'].process
    app.close();app.resources.close()
    assert first.poll() is not None
    for disabled in (False,True):
        resources=ProcessResources(Provider)
        restored=Application(root,resources=resources,discover=False)
        try:
            resources.start_services()
            if disabled:
                # Durable folder ownership must not silently restore the legacy
                # adapter even though the executable/socket remain available.
                with pytest.raises(ValueError,match='Unknown operation'):
                    restored.query('session_search.search',dict(query='disabled'))
                restored.execute('core.module_enable',dict(module='session_search'))
            status=restored.query('session_search.status')
            assert status['integration']=='core' and status['worker_alive']
            assert status['stats']['vectors']==1 and not status['indexing']
            result=restored.query('session_search.search',dict(query='hello'))
            assert [match['session']['id'] for match in result['matches']]==['restart']
            assert len(restored.query('session_search.history')['requests'])==(3 if disabled else 2)
            assert len(legacy_adapter['requests'])==calls_before
            process=restored.module_manager.runtime.services['session_search'].process
            if not disabled:
                restored.execute('core.module_disable',dict(module='session_search'))
                assert process.poll() is not None
        finally:
            restored.close();resources.close()
        assert process.poll() is not None
    assert legacy_adapter['binary'].is_file()


def test_stopped_selection_refreshes_persisted_counts_without_loading_provider(setup):
    app,home,_=setup
    write(home,'active','hello')
    write(home,'archived','hello',state='archived_sessions')
    app.execute('session_search.configure',dict(codex_home=str(home)))
    app.execute('session_search.selection',dict(active=True,archived=True,types=['main']))
    app.execute('session_search.start')
    wait(app,lambda status:status['stats']['sessions']==2 and status['stats']['vectors']==1 and not status['index_pending'])
    app.execute('session_search.stop')
    root=app.layout.root
    app.close();app.resources.close()
    providers=[]
    def factory(config):
        provider=Provider(config);providers.append(provider);return provider
    resources=ProcessResources(factory)
    restored=Application(root,resources=resources,discover=False)
    try:
        resources.start_services()
        assert restored.query('session_search.status')['stats']['sessions']==2
        for active,archived,expected in ((False,True,1),(False,False,0),(True,False,1)):
            restored.execute('session_search.selection',dict(active=active,archived=archived,types=['main']))
            status=restored.query('session_search.status')
            assert status['stats']['sessions']==expected
            assert status['stats']['vectors']==int(bool(expected))
            assert status['indexing'] is False
            assert status['model_name'] is None
            assert not providers
    finally:restored.close();resources.close()


def forbid_inventory(core):
    call=core.call
    def guarded(method,arguments):
        if method=='rag.vector_revisions':
            raise AssertionError('Full RAG inventory must not be requested')
        return call(method,arguments)
    core.call=guarded
    return core


def test_service_start_search_and_restart_never_request_full_inventory(setup):
    from session_search.module_service import Service
    app,home,_=setup
    write(home,'no-inventory','hello')
    core,access=sdk(app);forbid_inventory(core)
    service=Service(core);service.start()
    def wait_service():
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            status=service.status()
            assert status['error'] is None,status
            if status['stats'].get('vectors')==1 and not status.get('index_pending'): return
            time.sleep(.05)
        raise AssertionError(status)
    try:
        service.dispatch('session_search.configure',dict(codex_home=str(home),autostart=True))
        service.dispatch('session_search.start',{})
        wait_service()
        assert service.dispatch('session_search.search',dict(query='hello'))['matches']
        service.close();access.close()
        core,access=sdk(app);forbid_inventory(core)
        service=Service(core);service.start()
        wait_service()
        assert service.dispatch('session_search.search',dict(query='hello'))['matches']
        assert service.dispatch('session_search.history',{})['requests'][0]['state']=='completed'
    finally:service.close();access.close()


@pytest.mark.parametrize('phase',('before_put','after_put','after_prune','ack_failure'))
def test_durable_recovery_without_inventory_at_last_attempt(setup,phase):
    from session_search.module_service import SharedEmbedder,SharedVectors
    from session_search.module_store import ModuleStore
    class SimulatedCrash(BaseException): pass
    app,home,_=setup
    store,core,access=store_for(app);forbid_inventory(core)
    def reopen():
        core,access=sdk(app);forbid_inventory(core)
        embedder=SharedEmbedder(core)
        return ModuleStore(core,embedder.identity,embedder.dimensions,
            SharedVectors(core,embedder.config),chunking='conversation-blocks-v1:512'),core,access
    try:
        session,cp=ingest_one(store,home)
        pk=store.db.execute('SELECT block_pk FROM jobs').fetchone()[0]
        ingest_one(store,home,'confirmed-failure','a confirmed terminal failure')
        with store.db:
            store.db.execute('UPDATE jobs SET attempts=4 WHERE block_pk=?',(pk,))
            store.db.execute("UPDATE jobs SET state='failed',attempts=5,next_attempt=123456,error='confirmed failure' WHERE block_pk<>?",(pk,))
        job=store.claim_job();assert job['attempts']==5 and job['block_pk']==pk
        put=store.vector_backend.put
        def interrupted_put(*arguments):
            if phase=='before_put': raise SimulatedCrash()
            put(*arguments)
            if phase=='after_prune':
                Path(session['path']).unlink()
                store.reset_session(session['id'])
            if phase in ('after_put','after_prune'): raise SimulatedCrash()
        store.vector_backend.put=interrupted_put
        if phase=='ack_failure':
            execute=store.db.execute
            def acknowledgement_error(sql,parameters=()):
                if sql.startswith('INSERT OR REPLACE INTO vectors(rowid)'):
                    raise RuntimeError('simulated SQL acknowledgement failure')
                return execute(sql,parameters)
            store.db.execute=acknowledgement_error
            with pytest.raises(RuntimeError,match='acknowledgement'):
                store.complete_job(job['id'],job['content_hash'],[1.,0.])
            # This is the Indexer failure path: a confirmed failed last attempt
            # remains terminal, but its unacknowledged RAG write must be removed.
            store.fail_job(job['id'],'confirmed acknowledgement failure',now=100)
        else:
            with pytest.raises(SimulatedCrash):
                store.complete_job(job['id'],job['content_hash'],[1.,0.])
        assert store.db.execute('SELECT block_pk FROM vector_deletions').fetchone()[0]==pk
        store.close();access.close()
        store,core,access=reopen()
        # Same operation used by normal service initialization, with no listing.
        store.flush_vector_deletions()
        assert store.db.execute('SELECT COUNT(*) FROM vector_deletions').fetchone()[0]==0
        preserved=store.db.execute("SELECT attempts,next_attempt,error FROM jobs WHERE error='confirmed failure'").fetchone()
        assert list(preserved)==[5,123456.,'confirmed failure']
        profile=core.embeddings.profile()
        def rag_rows():
            return core.rag.search_vector(collection='blocks',object_ids=[str(pk)],vector=[1.,0.],profile=profile,limit=1)
        assert not rag_rows()
        if phase in ('before_put','after_put'):
            pending=store.db.execute('SELECT state,attempts FROM jobs WHERE id=?',(job['id'],)).fetchone()
            assert list(pending)==['pending',4]
            replay=store.claim_job();assert replay['attempts']==5
            assert store.complete_job(replay['id'],replay['content_hash'],[1.,0.])
            assert len(rag_rows())==1
            assert store.db.execute('SELECT COUNT(*) FROM vector_deletions').fetchone()[0]==0
            assert store.db.execute('SELECT COUNT(*) FROM vectors').fetchone()[0]==1
        elif phase=='ack_failure':
            failed=store.db.execute('SELECT state,attempts,next_attempt,error FROM jobs WHERE id=?',(job['id'],)).fetchone()
            assert list(failed)==['failed',5,132.,'confirmed acknowledgement failure']
            assert store.claim_job(now=1000) is None
        else:
            assert store.db.execute('SELECT 1 FROM blocks WHERE pk=?',(pk,)).fetchone() is None
            assert store.db.execute('SELECT 1 FROM jobs WHERE id=?',(job['id'],)).fetchone() is None
    finally:store.close();access.close()
