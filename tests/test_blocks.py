# Non-English strings are intentional Unicode/UTF-8 search and JSONL fixtures.
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from session_search.reader import read_increment
from session_search.store import Store, read_stats
from session_search.indexer import Indexer
from session_search.settings import save_selection
from test_indexer import Embedder, message, record


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home/'sessions').mkdir()
        (self.home/'archived_sessions').mkdir()
        self.store = Store(self.home/'index.sqlite3','fake',2)
        self.addCleanup(self.store.close)
        self.settings = self.home/'selection.json'
        self.actor = Indexer(self.home,self.store,Embedder(),selection_path=self.settings,block_tokens=64)

    def drain(self):
        for _ in range(100):
            if not self.actor.step():
                return
        self.fail('queue did not drain')

    def add(self,sid,text,state='active'):
        folder='sessions' if state=='active' else 'archived_sessions'
        path=self.home/folder/f'rollout-{sid}.jsonl'
        path.write_bytes(record('session_meta',id=sid)+message('user',text))
        self.actor.mark(path)
        return path

    def test_shared_vector_fork_tail_change_and_last_reference_removal(self):
        a=self.add('a','identical text')
        b=self.add('b','identical text')
        self.drain()
        self.assertEqual(self.store.stats()['vectors'],1)
        self.assertEqual(len(self.store.search([1,0])),2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],2)
        a.write_bytes(a.read_bytes()+message('assistant','new answer'))
        self.actor.mark(a); self.drain()
        self.assertEqual(self.store.stats()['vectors'],2)
        self.assertEqual(len(self.store.search([1,0])),2)
        b.unlink(); self.actor.reconcile(); self.drain()
        self.assertEqual(self.store.stats()['vectors'],1)
        a.unlink(); self.actor.reconcile(); self.drain()
        self.assertEqual(self.store.stats()['vectors'],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0],0)

    def test_disable_both_and_reenable_retains_index(self):
        a=self.add('a','normal')
        b=self.add('b','archive','archived')
        self.drain()
        self.assertEqual(self.store.stats()['sessions'],1)
        save_selection('archived',True,self.settings); self.drain()
        self.assertEqual(self.store.stats()['vectors'],2)
        save_selection('active',False,self.settings); self.drain()
        self.assertEqual(len(self.actor.handle_request(dict(op='search',query='x'))['matches']),1)
        self.assertEqual(self.store.stats()['vectors'],2)
        save_selection('archived',False,self.settings); self.drain()
        self.assertFalse(self.actor.handle_request(dict(op='search',query='x'))['matches'])
        self.assertEqual(self.actor.status(force=True)['stats']['chunks'],0)
        a.write_bytes(a.read_bytes()+message('assistant','new text'))
        self.actor.mark(a); self.drain()
        self.assertEqual(self.store.stats()['vectors'],2)
        save_selection('active',True,self.settings); self.drain()
        self.assertIn('new text',self.actor.handle_request(dict(op='search',query='x'))['matches'][0]['text'])

    def test_move_to_disabled_archive_preserves_vector_but_hides_result(self):
        a=self.add('a','normal'); self.drain()
        target=self.home/'archived_sessions'/a.name
        a.rename(target); self.actor.reconcile(); self.drain()
        self.assertEqual(self.store.stats()['vectors'],1)
        self.assertFalse(self.actor.handle_request(dict(op='search',query='x'))['matches'])
        self.assertEqual(self.store.sessions()[0]['state'],'archived')

    def test_single_job_for_shared_text_and_disabled_reference(self):
        def put(sid,state):
            self.store.ingest(dict(id=sid,state=state),{},[dict(id=sid,text='shared',start_offset=0,end_offset=1)])
        put('a','active'); put('b','archived')
        self.assertEqual(self.store.stats()['jobs'],{'pending':1})
        job=self.store.claim_job(selection={'active':False,'archived':True})
        self.assertEqual(job['session_id'],'b')
        self.assertIsNone(self.store.claim_job())
        self.assertTrue(self.store.complete_job(job['id'],job['content_hash'],[1,0]))
        self.assertEqual(len(self.store.search([1,0],limit=1,selection={'active':True,'archived':False})),1)

    def test_stopped_read_is_read_only_and_does_not_recover_job(self):
        self.store.ingest({'id':'a'}, {},[dict(id='a',text='text',start_offset=0,end_offset=1)])
        job=self.store.claim_job()
        before=self.store.db.execute('SELECT * FROM jobs').fetchall()
        snapshot=read_stats(self.store.path,{'active':True,'archived':False})
        self.assertEqual(snapshot['stats']['jobs'],{'running':1})
        self.assertEqual(before,self.store.db.execute('SELECT * FROM jobs').fetchall())
        self.assertTrue(snapshot['database_bytes']>0)
        missing=self.home/'absent.sqlite'
        with self.assertRaises(sqlite3.OperationalError): read_stats(missing)
        self.assertFalse(missing.exists())

    def test_tail_restart_full_scan_equivalence_large_unicode(self):
        path=self.add('a','начало '*50)
        first=read_increment(path,max_tokens=64,token_counter=len)
        upserts={c['id']:c for c in first['chunks']}
        fixed={c['id']:c for c in first['chunks'] if c['final']}
        with path.open('ab') as f: f.write(message('assistant','🌍 ответ '*50))
        second=read_increment(path,first['next_offset'],json.loads(json.dumps(first['state'])),max_tokens=64,token_counter=len)
        upserts.update({c['id']:c for c in second['chunks']})
        full=read_increment(path,max_tokens=64,token_counter=len)
        self.assertEqual(list(upserts.values()),full['chunks'])
        self.assertTrue(all(upserts[k]==v for k,v in fixed.items()))
        self.assertTrue(all(len(c['text'])<=64 for c in upserts.values()))

    def test_explicit_rebuild_preserves_history_and_session_inventory(self):
        self.add('a','text'); self.drain()
        self.store.db.execute("INSERT INTO search_requests(id,started_at,tool,parameters,state) VALUES('history',0,'session_search','{}','started')")
        self.store.db.commit()
        with self.assertRaises(ValueError): Store(self.store.path,'other',3)
        other=Store(self.store.path,'other',3,rebuild=True)
        try:
            self.assertEqual(other.stats()['vectors'],0)
            self.assertEqual(other.stats()['sessions'],1)
            self.assertEqual(other.checkpoint('a'),{})
            self.assertEqual(other.db.execute('SELECT COUNT(*) FROM search_requests').fetchone()[0],1)
        finally: other.close()
