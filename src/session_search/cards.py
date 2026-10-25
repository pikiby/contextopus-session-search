"""Session descriptions: Markdown authority, SQL projection, shared Core RAG.

No transcript reader, watcher, timer, model-generated summary or private index.
"""
import hashlib
import json
import re
from datetime import datetime, timezone


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Cards:
    directory = 'sessions'
    collection = 'cards'

    def __init__(self, core):
        self.core = core

    def rows(self):
        return self.core.storage.query('SELECT * FROM "{{module}}::cards"', [])['rows']

    def initialize(self):
        marker = self.core.storage.query('SELECT value FROM "{{module}}::metadata" WHERE key=?', ['cards_initialized'])['rows']
        if not marker:
            self.core.documents.prepare(directory=self.directory)
            self.core.storage.execute('INSERT INTO "{{module}}::metadata" VALUES (?,?)', ['cards_initialized', '1'])

    @staticmethod
    def validate(metadata, content):
        for field in ('id', 'title', 'description', 'source', 'session_id'):
            if not isinstance(metadata.get(field), str) or not metadata[field].strip():
                raise ValueError('Session card requires ' + field)
        if metadata.get('type') != 'session' or not isinstance(content, str) or not content.strip():
            raise ValueError('A session card requires type=session and a nonempty description body')
        for field in ('tags', 'contexts', 'links', 'required_links'):
            values = metadata.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(field + ' must be a list of nonempty strings')
            if field != 'tags' and any(not re.fullmatch(r'\[\[[^\[\]\n]+\]\]', v) for v in values):
                raise ValueError(field + ' must contain Obsidian wikilinks')
        for field in ('source_uri', 'project', 'status', 'covered_through', 'updated_at'):
            if field in metadata and not isinstance(metadata[field], str):
                raise ValueError(field + ' must be text')

    def capture(self):
        scan = self.core.documents.scan(directory=self.directory, collection=self.collection)
        if not scan.get('complete'):
            raise ValueError('Incomplete card scan; no deletion may be inferred')
        documents, identities = {}, set()
        for doc in scan['documents']:
            meta = doc['metadata']
            self.validate(meta, doc['text'])
            identity = (meta['source'], meta['session_id'])
            if identity in identities or meta['id'] in documents:
                raise ValueError('Duplicate session identity')
            identities.add(identity)
            documents[meta['id']] = doc
        return documents

    @staticmethod
    def summary(doc):
        meta = doc['metadata']
        return {**{k: meta.get(k) for k in ('id','title','description','source','session_id','source_uri','project','status','tags','contexts','updated_at')},
                'type': 'session', 'revision': doc['file_revision'], 'path': doc['path']}

    @staticmethod
    def matches(doc, filters):
        meta = doc['metadata']
        for name, value in (filters or {}).items():
            if name not in {'source','project','context','tag','status'}:
                raise ValueError('Unknown session filter: ' + name)
            if name in {'context','tag'}:
                if value not in meta.get('contexts' if name == 'context' else 'tags', []):
                    return False
            elif meta.get(name) != value:
                return False
        return True

    def read(self, id):
        documents = self.capture()
        if id not in documents:
            raise ValueError('Unknown session card: ' + id)
        doc = documents[id]
        return {**self.summary(doc), 'content': doc['text'], 'metadata': doc['metadata'],
                'links': doc['metadata'].get('links', []),
                'required_links': doc['metadata'].get('required_links', [])}

    def list(self, filters=None, offset=0, limit=50):
        docs = self.capture()
        values = [self.summary(doc) for doc in docs.values() if self.matches(doc, filters)]
        values.sort(key=lambda v: (v['title'].casefold(), v['id']))
        return {'records': values[offset:offset+limit], 'total': len(values), 'has_more': offset+limit < len(values)}

    def save(self, record, expected_revision=None):
        record = dict(record)
        content = record.pop('content')
        ident = record.setdefault('id', digest(encoded([record['source'], record['session_id']]))[:32])
        docs = self.capture()
        old = docs.get(ident)
        if (old['file_revision'] if old else None) != expected_revision:
            raise ValueError('Card changed; read its current revision')
        for other, doc in docs.items():
            if other != ident and (doc['metadata']['source'],doc['metadata']['session_id']) == (record['source'],record['session_id']):
                raise ValueError('This session already has a card: ' + other)
        if old and any(old['metadata'][k] != record[k] for k in ('source','session_id')):
            raise ValueError('Session identity cannot change')
        metadata = {**record, 'type': 'session', 'updated_at': datetime.now(timezone.utc).isoformat()}
        self.validate(metadata, content)
        slug = re.sub(r'[^\w.-]+', '-', record['title']).strip('.-')[:100] or 'session'
        path = old['location'] if old else slug + '--' + digest(ident)[:8] + '.md'
        self.core.documents.write(directory=self.directory, collection=self.collection, path=path,
                                  metadata=metadata, text=content, expected_revision=expected_revision)
        try:
            result = self.reconcile()
        except Exception as exc:
            result = {'index_status':'pending', 'error': str(exc)}
        return {**self.read(ident), 'index': result}

    def rag_document(self, doc):
        return {'key': {'collection': self.collection, 'id':doc['metadata']['id']},
                'revision': doc['revision'], 'text': doc['text'], 'location': doc['path'],
                'metadata': doc['metadata']}

    def reconcile(self):
        docs = self.capture()  # Validate everything before touching derived data.
        previous = {row['id']: row for row in self.rows()}
        plan = self.core.documents.plan(collection=self.collection, documents=list(docs.values()),
            previous=[json.loads(row['document']) for row in previous.values()], complete=True)
        for key in plan['delete']:
            self.core.rag.delete_document(key={'collection': self.collection, 'id':key['id']})
        # Retire only legacy vector projections. Source transcripts, SQL rows and
        # old configuration remain intact; no old transcript is made into a summary.
        marker = self.core.storage.query('SELECT value FROM "{{module}}::metadata" WHERE key=?', ['cards_retired_legacy_vectors'])['rows']
        if not marker:
            for row in self.core.storage.query('SELECT rowid FROM "{{module}}::vectors"', [])['rows']:
                self.core.rag.delete_document(key={'collection':'blocks','id':str(row['rowid'])})
            self.core.storage.execute('INSERT INTO "{{module}}::metadata" VALUES (?,?)', ['cards_retired_legacy_vectors','1'])
        with self.core.storage.transaction() as tx:
            for key in plan['delete']:
                tx.execute('DELETE FROM "{{module}}::cards" WHERE id=?', [key['id']])
            for ident, doc in docs.items():
                tx.execute('INSERT INTO "{{module}}::cards" (id,document) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document', [ident,encoded(doc)])
        if not docs:
            return {'index_status':'ready','cards':0,'embedded':0,'deleted':len(plan['delete'])}
        profile = self.core.embeddings.profile()
        profile_revision = digest(encoded(profile))
        errors, embedded = [], 0
        for ident, doc in docs.items():
            stamp = profile_revision + ':' + doc['revision']
            try:
                if previous.get(ident, {}).get('indexed_revision') != stamp:
                    vector = self.core.embeddings.embed_documents(texts=[doc['text']],profile=profile)[0]
                    # Do not acknowledge a stale snapshot if a person edited it meanwhile.
                    current = self.capture().get(ident)
                    if current is None or current['file_revision'] != doc['file_revision']:
                        raise ValueError('Card changed during indexing; reconcile again')
                    self.core.rag.put_vector(document=self.rag_document(doc),vector=vector,profile=profile)
                    embedded += 1
                else:
                    self.core.rag.update_document(document=self.rag_document(doc))
                self.core.storage.execute('UPDATE "{{module}}::cards" SET indexed_revision=?,index_error=NULL WHERE id=?', [stamp,ident])
            except Exception as exc:
                error = str(exc)[:500]
                errors.append({'id':ident,'error':error})
                self.core.storage.execute('UPDATE "{{module}}::cards" SET index_error=? WHERE id=?',[error,ident])
        return {'index_status':'pending' if errors else 'ready','cards':len(docs),'embedded':embedded,'deleted':len(plan['delete']),'errors':errors}

    def search(self, query, filters=None, selection=None):
        docs = self.capture()
        # Current file revisions and metadata eligibility are applied before top-N.
        rows = {r['id']:r for r in self.rows()}
        ids = [ident for ident, doc in docs.items() if self.matches(doc,filters)
               and rows.get(ident,{}).get('indexed_revision','') is not None
               and (rows.get(ident,{}).get('indexed_revision') or '').endswith(':'+doc['revision'])]
        response = self.core.rag.search(query=query,collection=self.collection,object_ids=ids,
                                       include_archived=True,**({'selection':selection} if selection is not None else {}))
        current = self.capture()
        result = []
        for hit in response['results']:
            ident = hit['key'][2]
            doc = current.get(ident)
            if doc and doc['revision']==hit['revision'] and self.matches(doc,filters):
                result.append({**self.summary(doc),'distance':hit['distance']})
        return {'results':result,'diagnostics':response['diagnostics'],
                'pending_count':sum(ident not in ids for ident,doc in docs.items() if self.matches(doc,filters))}

    def delete(self, id, expected_revision):
        doc = self.capture().get(id)
        if doc is None:
            raise ValueError('Unknown session card')
        self.core.documents.delete(directory=self.directory,path=doc['location'],expected_revision=expected_revision)
        return {'deleted':id,'index':self.reconcile(),'source_session_untouched':True}

    def status(self):
        docs = self.capture()
        rows = {r['id']:r for r in self.rows()}
        return {'module':'session_search','mode':'session_cards','cards':len(docs),'background_indexing':False,
                'pending_count':sum(not (rows.get(i,{}).get('indexed_revision') or '').endswith(':'+d['revision']) for i,d in docs.items()),
                'errors':[{'id':r['id'],'error':r['index_error']} for r in rows.values() if r['index_error']],
                'legacy_transcripts_preserved':True}
