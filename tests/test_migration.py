import json
import struct
import tempfile
import unittest
from pathlib import Path
from session_search.store import Store, _connect, read_stats
from session_search.search_history import initialize


class LegacyMigrationTests(unittest.TestCase):
    def test_old_database_requires_explicit_rebuild_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.sqlite3'
            db=_connect(path)
            db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE sessions(id TEXT PRIMARY KEY, metadata TEXT,state TEXT,checkpoint TEXT);
            CREATE TABLE chunks(pk INTEGER PRIMARY KEY,id TEXT,session_id TEXT REFERENCES sessions(id),text TEXT,start_offset INTEGER,end_offset INTEGER,content_hash TEXT,metadata TEXT);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,chunk_pk INTEGER REFERENCES chunks(pk),content_hash TEXT,state TEXT,attempts INTEGER,next_attempt REAL,error TEXT);
            CREATE VIRTUAL TABLE vectors USING vec0(embedding float[2]);''')
            db.execute('INSERT INTO metadata VALUES(?,?)',('embedding_identity',json.dumps({'model':'old','dimensions':2},sort_keys=True)))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',('s',json.dumps({'id':'s','state':'active'}),'active',json.dumps({'offset':999})))
            db.execute("INSERT INTO chunks VALUES(1,'c','s','text',0,10,'hash','{}')")
            db.execute('INSERT INTO vectors(rowid,embedding) VALUES(1,?)',(struct.pack('2f',1,0),))
            initialize(db)
            db.execute("INSERT INTO search_requests(id,started_at,tool,parameters,state) VALUES('history',0,'session_search','{}','started')")
            db.commit();db.close()
            self.assertEqual(read_stats(path)['stats']['vectors'],1)
            with self.assertRaisesRegex(ValueError,'rebuild index explicitly'): Store(path,'new',3)
            self.assertEqual(read_stats(path)['stats']['vectors'],1)
            store=Store(path,'new',3,rebuild=True)
            try:
                self.assertEqual(store.checkpoint('s'),{})
                self.assertEqual(store.stats()['vectors'],0)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM search_requests').fetchone()[0],1)
                self.assertEqual(store.db.execute('PRAGMA foreign_key_check').fetchall(),[])
            finally:store.close()
