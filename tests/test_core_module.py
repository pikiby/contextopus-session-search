import json
import time
import sqlite3
from pathlib import Path
try:
    import pytest
except ImportError:
    import unittest
    raise unittest.SkipTest("Core integration tests require the existing Core test environment")
pytest.importorskip("contextopus")
pytest.importorskip("lancedb")
from contextopus.core.storage import StorageLayout
from contextopus.core.rag import RagIndex, DocumentKey, IndexDocument
from contextopus.core.resources import ProcessResources
from contextopus.core.embeddings import default_config
from contextopus.core.chunking import default_chunking
from session_search.core_module import Runtime, CoreEmbedder, CoreVectors, SessionSource, vector_revisions
from session_search.store import Store
from session_search.indexer import Indexer

class Provider:
    def __init__(self, config): self.config=config; self.calls=[]
    def embed(self,texts,purpose='document'):
        self.calls.append((texts,purpose)); return [[1.,0.] for t in texts]
    def tokenizer(self): return lambda t,**kw: {'input_ids':list(range(len(t)))}
    def close(self): pass

@pytest.fixture
def setup(tmp_path):
    cfg=default_config();cfg['profile']['dimensions']=2
    layout=StorageLayout(tmp_path/'root'); resources=ProcessResources(Provider)
    index=RagIndex(layout,resources,cfg,default_chunking())
    # Fixture initializes the shared profile, not any live data or model.
    index._generic_table(create=True)
    home=tmp_path/'codex';(home/'sessions').mkdir(parents=True)
    yield layout,resources,index,home
    resources.close()

def write(home,sid='one',text='hello'):
    p=home/'sessions'/('rollout-'+sid+'.jsonl')
    rows=[dict(type='session_meta',payload=dict(id=sid,cwd='/example',source='cli')),
          dict(type='response_item',payload=dict(type='message',role='user',content=[dict(type='input_text',text=text)]))]
    p.write_text(''.join(json.dumps(r)+'\n' for r in rows));return p

def drain(actor):
    for i in range(100):
        if not actor.step():return
    raise AssertionError('not idle')

def test_blocks_shared_and_tail_update(setup):
    layout,res,index,home=setup
    p=write(home);write(home,'two')
    d=layout.root/'modules/session_search';d.mkdir(parents=True)
    e=CoreEmbedder(index);store=Store(d/'catalog.sqlite3',e.identity,e.dimensions,vector_backend=CoreVectors(index,e.config))
    try:
        a=Indexer(home,store,e,selection_path=d/'sessions.json');drain(a)
        assert store.stats()['vectors']==1
        assert len(a.handle_request(dict(op='search',query='hello'))['matches'])==2
        write(home,'one','changed');a.mark(p);drain(a)
        assert store.stats()['vectors']==2
        p.unlink();a.reconcile();drain(a);store.flush_vector_deletions()
        assert store.stats()['vectors']==1
        assert len(index.read_rows(index.table))==1
        index.register_source('session_search',SessionSource(d))
        assert len(index.search('hello',module='session_search'))==1
        assert len(res.providers._entries)==1
    finally:store.close()

def test_runtime_stop_counts_and_restart(setup):
    layout,res,index,home=setup;write(home)
    r=Runtime(layout,res);r.configure(str(home));r.start()
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if r.status().get('stats',{}).get('vectors')==1:break
            time.sleep(.05)
        assert r.status()['stats']['vectors']==1,r.status()
        r.stop();assert r.status()['indexing'] is False
        assert r.request(dict(op='search',query='hello'))['matches']
        r.close()
        assert Runtime(layout,res).status()['stats']['vectors']==1
    finally:r.close()

def test_profile_change_rejected(setup):
    layout,res,index,home=setup
    b=CoreVectors(index,index.profile())
    profile=layout.rag/'active.json';value=json.loads(profile.read_text());value['config']['profile']['revision']='different';profile.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='profile changed'):b.put(1,'text','rev',[1.,0.])


def test_recovery_after_interrupted_vector_write(setup):
    layout,res,index,home=setup
    d=layout.root/'modules/session_search';d.mkdir(parents=True)
    e=CoreEmbedder(index);backend=CoreVectors(index,e.config)
    store=Store(d/'catalog.sqlite3',e.identity,2,vector_backend=backend)
    try:
        write(home);a=Indexer(home,store,e,selection_path=d/'sessions.json');drain(a)
        pk=store.db.execute('SELECT rowid FROM vectors').fetchone()[0]
        backend.delete(pk)
        assert store.stats()['vectors']==1
        store.recover_vectors(vector_revisions(index))
        assert store.stats()['vectors']==0
        drain(a)
        assert store.stats()['vectors']==1
        # A vector committed before the SQLite transaction must not leak to generic search.
        backend.put(999, 'orphan', 'r', [1.,0.])
        index.register_source('session_search',SessionSource(d))
        assert len(index.search('hello',module='session_search'))==1
        store.recover_vectors(vector_revisions(index))
        assert '999' not in vector_revisions(index)
    finally:store.close()


def test_main_filter_before_top_k_and_unknown_not_main(setup):
    layout,res,index,home=setup
    r=Runtime(layout,res);r.configure(str(home));write(home,'main')
    p=write(home,'child');p.write_text(p.read_text().replace('"source": "cli"', '"source": {"subagent": {"thread_spawn": {"parent_thread_id": "main"}}}'))
    try:
        r.start()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            st=r.status()
            if st.get('stats',{}).get('vectors')==1 and not st.get('index_pending'):break
            time.sleep(.05)
        matches=r.request(dict(op='search',query='hello',limit=1))['matches']
        assert [m['session']['id'] for m in matches]==['main']
        r.selection(True,False,['subagent'])
        matches=r.request(dict(op='search',query='hello',limit=1))['matches']
        assert [m['session']['id'] for m in matches]==['child']
        assert matches[0]['session']['parent_thread_id']=='main'
    finally:r.close()


def test_registry_shared_runtime_and_history(setup):
    from types import SimpleNamespace
    from contextopus.core.operations import OperationRegistry
    from session_search.core_module import register
    layout,res,index,home=setup;write(home)
    def app():return SimpleNamespace(layout=layout,resources=res,index=index,registry=OperationRegistry())
    one,two=app(),app()
    runtime=register(one)
    assert register(two) is runtime
    runtime.configure(str(home));runtime.start()
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if runtime.status().get('stats',{}).get('vectors')==1:break
            time.sleep(.05)
        runtime.stop()
        result=one.registry.dispatch('query','session_search.search',dict(query='hello'))
        assert result['matches'] and result['search_request_id']
        assert runtime.enabled.is_set() is False
        assert len(res.providers._entries)==1
        with sqlite3.connect(runtime.path) as db:
            assert db.execute('SELECT COUNT(*) FROM search_requests').fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM search_results').fetchone()[0]==1
    finally:runtime.close()


def test_source_classification():
    from session_search.session_types import classify
    assert classify({})['session_type']=='unknown'
    assert classify(dict(source='cli'))['session_type']=='main'
    assert classify(dict(source='exec'))['session_type']=='exec'
    assert classify(dict(source={'subagent':{'other':'guardian'}}))['session_type']=='guardian'
    assert classify(dict(forked_from_id='x'))['session_type']=='unknown'


def test_compatibility_inventory_consumes_bounded_pages():
    from unittest.mock import Mock, call
    index = Mock()
    index.vector_revisions_page.side_effect = [
        dict(revisions={"1": "a"}, conflicts=[], next_cursor="next"),
        dict(revisions={"2": "b"}, conflicts=[], next_cursor=None),
    ]
    assert vector_revisions(index) == {"1": "a", "2": "b"}
    assert index.vector_revisions_page.call_args_list == [
        call("session_search", "blocks", cursor=None),
        call("session_search", "blocks", cursor="next"),
    ]
    index.vector_revisions.assert_not_called()


def test_compatibility_inventory_rejects_conflicts_before_recovery():
    from unittest.mock import Mock
    index = Mock()
    index.vector_revisions_page.return_value = dict(
        revisions={}, conflicts=["1"], next_cursor=None)
    with pytest.raises(ValueError, match="Conflicting RAG revisions"):
        vector_revisions(index)
