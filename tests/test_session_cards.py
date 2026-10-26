"""End-to-end folder-module tests with real Core SQL/files/RAG and child RPC."""
import json
from pathlib import Path
import runpy

import pytest
from contextopus.startup import Application
from contextopus.core.resources import ProcessResources
from contextopus.core.embeddings import default_config
from contextopus.interfaces.web.backend import LibraryBackend

ROOT = Path(__file__).resolve().parents[1]


class Provider:
    def __init__(self): self.calls=[]
    def embed(self,texts,purpose='document'):
        self.calls.append((texts,purpose))
        return [[1.,0.] if 'database' in t.lower() else [0.,1.] for t in texts]
    def tokenizer(self): return lambda text,**kwargs: {'input_ids':text.split()}
    def close(self): pass


@pytest.fixture
def app(tmp_path):
    build=runpy.run_path(str(ROOT/'scripts/build-module.py'))['build']
    release=build(tmp_path/'release')
    provider=Provider()
    resources=ProcessResources(provider_factory=lambda config:provider)
    with Application(tmp_path/'root',resources=resources,discover=False) as application:
        config=default_config();config['profile']['dimensions']=2
        application.index.embeddings=config
        application.index._generic_table(create=True)
        preview=application.query('core.module_source_plan',{'module':'session_search','source':str(release)})
        application.execute('core.module_install',{'module':'session_search','source':str(release),'source_revision':preview['revision']})
        application.module_manager.start_services()
        application.test_provider=provider
        yield application
    resources.close()


def save(app, source='claude-desktop', sid='session-1', **kw):
    record={'title':'Database discussion','description':'Database decisions','source':source,'session_id':sid,
            'content':'# Database\n\nDiscussed database recovery. Restore is still unverified.',
            'contexts':['[[modules/workspace/workspace_contexts/demo/Context]]'],'tags':['database'],'status':'active'}
    record.update(kw)
    return app.execute('session_search.save',{'record':record})


def test_card_install_save_search_and_restart(app):
    saved=save(app)
    assert saved['index']['index_status']=='ready',saved
    p=Path(saved['path']);assert p.is_file() and p.parent.name=='sessions'
    assert 'source: claude-desktop' in p.read_text()
    assert app.test_provider.calls[0][0]==[saved['content']]
    result=app.query('session_search.search',{'query':'database recovery'})
    assert result['results'][0]['id']==saved['id']
    assert 'content' not in result['results'][0] and 'excerpt' not in result['results'][0]
    assert result['diagnostics']['algorithm']=='distance_jump'
    with pytest.raises(Exception,match='revision'):save(app)
    with pytest.raises(Exception):app.query('session_search.save',{'record':{}})
    app.execute('core.module_disable',{'module':'session_search'})
    app.execute('core.module_enable',{'module':'session_search'})
    app.module_manager.start_services()
    assert app.query('session_search.read',{'id':saved['id']})['revision']==saved['revision']
    for name in ('work','search','edit'):
        procedure=app.procedure('session_search/'+name)
        assert procedure['operations'] and not procedure.get('unavailable_operations')
    assert not app.query('session_search.status')['background_indexing']


def test_filters_before_limit_and_library(app):
    save(app)
    target=save(app,'codex','session-2',project='other',tags=['ops'],status='done')
    result=app.query('session_search.search',{'query':'database','filters':{'source':'codex','project':'other','tag':'ops','status':'done'},'selection':{'algorithm':'nearest_n','parameters':{'N':1}}})
    assert [x['id'] for x in result['results']]==[target['id']]
    assert result['diagnostics']['counts']['eligible_documents']==1
    library=LibraryBackend(app)
    assert library.call('session_search','collections')['filters']
    page=library.call('session_search','records',mode='semantic',query='database',filters={'source':'codex'})
    assert page['records'][0]['source']=='codex'
    record=library.call('session_search','record',collection='cards',id=target['id'])
    assert record['content']==target['content'] and record['metadata']['session_id']=='session-2'
    assert record['links'][0]['reference'].startswith('[[modules/workspace')


def test_external_edit_move_delete_no_transcript_access(app,tmp_path):
    original=tmp_path/'original.jsonl';original.write_text('private original')
    saved=save(app,source_uri=str(original))
    p=Path(saved['path']);p.write_text(p.read_text().replace('Discussed database recovery.','Discussed gardening.'))
    assert app.query('session_search.search',{'query':'database'})['results']==[]
    result=app.execute('session_search.reconcile')
    assert result['embedded']==1
    renamed=p.with_name('renamed.md');p.rename(renamed)
    before=len(app.test_provider.calls)
    assert app.execute('session_search.reconcile')['embedded']==0
    assert len(app.test_provider.calls)==before
    assert app.query('session_search.read',{'id':saved['id']})['path']==str(renamed)
    renamed.unlink()
    assert app.execute('session_search.reconcile')['deleted']==1
    assert app.query('session_search.list')['total']==0
    assert original.read_text()=='private original'


def test_failed_scan_and_index_retry(app,monkeypatch):
    saved=save(app)
    p=Path(saved['path'])
    p.write_text(p.read_text().replace('Discussed database','Changed database'))
    provider=app.test_provider
    original=provider.embed
    def fail(*a,**k):raise RuntimeError('embedding offline')
    monkeypatch.setattr(provider,'embed',fail)
    result=app.execute('session_search.reconcile')
    assert result['index_status']=='pending'
    assert p.is_file()
    monkeypatch.setattr(provider,'embed',original)
    assert app.execute('session_search.reconcile')['index_status']=='ready'
    duplicate=p.with_name('duplicate.md');duplicate.write_bytes(p.read_bytes())
    with pytest.raises(Exception,match='Duplicate'):app.execute('session_search.reconcile')
    duplicate.unlink()
    p.unlink();p.parent.rmdir()
    with pytest.raises(Exception):app.execute('session_search.reconcile')


def test_delete_revision_duplicate_identity_and_no_legacy_operations(app):
    saved=save(app)
    with pytest.raises(Exception,match='already has a card'):save(app,id='second')
    with pytest.raises(Exception,match='revision'):
        app.execute('session_search.delete',{'id':saved['id'],'expected_revision':'stale'})
    result=app.execute('session_search.delete',{'id':saved['id'],'expected_revision':saved['revision']})
    assert result['source_session_untouched']
    names=set(sum(app.registry.describe()['operations'].values(), []))
    assert 'session_search.start' not in names and 'session_search.open_tui' not in names


def test_legacy_catalog_is_preserved_and_not_returned(app):
    db=app.module_storage.for_module('session_search')
    db.execute('session_search','INSERT INTO "{{module}}::sessions" VALUES (?,?,?,?)',['old','{}','active','{}'])
    app.execute('session_search.reconcile')
    assert app.query('session_search.list')['total']==0
    assert db.query('session_search','SELECT id FROM "{{module}}::sessions"')['rows'][0]['id']=='old'


def test_zip_contains_only_card_runtime(tmp_path):
    import zipfile
    builder=runpy.run_path(str(ROOT/'scripts/build-module.py'))
    archive=builder['build_zip'](tmp_path/'cards.zip')
    with zipfile.ZipFile(archive) as z:
        names=set(z.namelist())
        assert 'session_search/package/session_search/cards.py' in names
        assert 'session_search/package/skills/sessions/SKILL.md' in names
        assert 'session_search/package/migrations/002-session-cards.sql' in names
        assert not any(n.endswith(('/indexer.py','/module_service.py','/tui.py')) for n in names)
        manifest=json.loads(z.read('session_search/module.json'))
        assert manifest['version']=='0.9.1'
    assert Path(str(archive)+'.sha256').is_file()
