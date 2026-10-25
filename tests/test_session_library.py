import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from session_search.library import library

def test_existing_catalog_only_and_bounded_content():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.executescript('CREATE TABLE sessions(id TEXT,metadata TEXT,state TEXT); CREATE TABLE chunks(pk INTEGER,id TEXT,session_id TEXT,block_pk INTEGER,start_offset INTEGER,end_offset INTEGER); CREATE TABLE blocks(pk INTEGER,text TEXT);')
    for i in range(5):
        db.execute('INSERT INTO sessions VALUES(?,?,?)',(str(i),json.dumps({'title':'ALPHA '+str(i),'session_type':'main' if i%2 else 'subagent'}),'active'))
    for i in range(3):
        db.execute('INSERT INTO blocks VALUES(?,?)',(i,'User: Message '+str(i)))
        db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)',(i,str(i),'1',i,i,i+1))
    service=SimpleNamespace(db=db)
    result=library(service,'records',query='alpha',type='subagent',offset=1,limit=1)
    assert result['total']==3 and result['has_more']
    assert result['records'][0]['key']==['session_search','sessions','2']
    first=library(service,'record',id='1',limit=2)
    assert first['metadata']['pagination']['next_offset']==2
    assert 'Message 2' not in first['content']
    assert 'Message 2' in library(service,'record',id='1',offset=2)['content']
    assert not library(service,'record',id='1',offset=2)['metadata']['pagination']['has_more']

def test_semantic_filters_before_nearest_without_actor_start(tmp_path):
    from session_search.module_service import Service
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.executescript('CREATE TABLE sessions(id TEXT,metadata TEXT,state TEXT,checkpoint TEXT); CREATE TABLE chunks(pk INTEGER,id TEXT,session_id TEXT,block_pk INTEGER,start_offset INTEGER,end_offset INTEGER,metadata TEXT); CREATE TABLE blocks(pk INTEGER,text TEXT); CREATE TABLE vectors(rowid INTEGER);')
    for ident,kind in [('main-one','main'),('sub-one','subagent')]:
        path=tmp_path/ident
        path.write_text('test transcript')
        stat=path.stat()
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(ident,json.dumps({'id':ident,'path':str(path),'session_type':kind,'title':ident}),'active',json.dumps({'inode':stat.st_ino,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns})))
    for i,sid in enumerate(['main-one','sub-one']):
        db.execute('INSERT INTO blocks VALUES(?,?)',(i,'User: '+sid))
        db.execute('INSERT INTO vectors VALUES(?)',(i,))
        db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?)',(i,str(i),sid,i,i,i+1,'{}'))
    calls=[]
    def nearest(**arguments):
        calls.append(arguments)
        assert arguments['object_ids']==['0']
        return [{'object_id':'0','_distance':.1}]
    core=SimpleNamespace(embeddings=SimpleNamespace(profile=lambda:{'provider':{},'profile':{'dimensions':2}},embed_query=lambda **kw:[1.,0.]),
                         rag=SimpleNamespace(search_vector=nearest))
    service=object.__new__(Service)
    service.core,service.db,service._selection=core,db,{'active':True,'archived':False,'types':['main','subagent']}
    found=service.dispatch('session_search.library',{'action':'records','mode':'semantic','query':'test','type':'main'})
    assert found['records'][0]['key']==['session_search','sessions','main-one']
    assert found['total'] is None and found['diagnostics']['limits']['nearest_blocks']==20
    record=service.dispatch('session_search.library',{'action':'record','collection':'sessions','id':'main-one'})
    assert record['content']=='User: main-one'
    assert calls


def test_oversized_first_block_fails_without_repeating_page():
    import pytest
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.executescript('CREATE TABLE sessions(id TEXT,metadata TEXT,state TEXT); CREATE TABLE chunks(pk INTEGER,id TEXT,session_id TEXT,block_pk INTEGER,start_offset INTEGER,end_offset INTEGER); CREATE TABLE blocks(pk INTEGER,text TEXT);')
    db.execute('INSERT INTO sessions VALUES(?,?,?)',('oversized',json.dumps({'title':'Huge','session_type':'main'}),'active'))
    db.execute('INSERT INTO blocks VALUES(?,?)',(1,'x'*262145))
    db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)',(1,'chunk','oversized',1,0,1))
    with pytest.raises(ValueError,match='exceeds the 256 KiB read page bound'):
        library(SimpleNamespace(db=db),'record',id='oversized')
