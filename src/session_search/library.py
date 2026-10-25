"""Read existing session catalog without starting its indexer or discovering files."""
import json
from .module_service import SharedEmbedder, SharedVectors
from .module_store import ModuleStore


def library(service, action, collection=None, query='', mode='text', type=None, offset=0, limit=50, id=None):
    if action == 'collections':
        return {'collections':[{'id':'sessions','title':'Indexed sessions'}]}
    if collection and collection != 'sessions':
        raise ValueError('Unknown Session Search collection')
    db = service.db
    def decode(row):
        metadata = json.loads(row['metadata'])
        title = metadata.get('title') or metadata.get('name') or metadata.get('first_user_message') or row['id']
        description = metadata.get('description') or metadata.get('cwd') or ''
        return {'key':['session_search','sessions',row['id']], 'title':str(title),
                'description':str(description), 'type':metadata.get('session_type','unknown'),
                'status':row['state'], 'path':metadata.get('path'), '_metadata':metadata}
    values = [decode(row) for row in db.execute('SELECT id,metadata,state FROM sessions ORDER BY id')]
    if action == 'record':
        record = next((item for item in values if item['key'][2]==id),None)
        if record is None:
            raise ValueError('Unknown session catalog record')
        # Blocks retain canonical messages already captured by this module. One
        # page is <=100 blocks and <=256 KiB, comfortably below the SDK frame.
        total = db.execute('SELECT COUNT(*) FROM chunks WHERE session_id=?',(id,)).fetchone()[0]
        rows = db.execute('SELECT c.id,c.start_offset,c.end_offset,b.text FROM chunks c JOIN blocks b ON b.pk=c.block_pk WHERE c.session_id=? ORDER BY c.start_offset,c.pk LIMIT ? OFFSET ?', (id,limit,offset))
        parts, consumed, size = [], 0, 0
        for row in rows:
            text = row['text']
            if size+len(text.encode('utf-8')) > 262144:
                if not consumed:
                    raise ValueError('Session conversation block exceeds the 256 KiB read page bound; content was not returned')
                break
            parts.append(text)
            size += len(text.encode('utf-8'))
            consumed += 1
        metadata = record.pop('_metadata')
        metadata = {**metadata,'content_source':'existing catalog conversation blocks',
                    'pagination':{'offset':offset,'limit':limit,'returned':consumed,'total':total,
                                  'has_more':offset+consumed<total,'next_offset':offset+consumed if offset+consumed<total else None}}
        return {**record,'content':'\n\n'.join(parts),'metadata':metadata,'links':[]}
    if action != 'records':
        raise ValueError('Unknown library action')
    types = sorted({item['type'] for item in values})
    if type:
        values = [item for item in values if item['type']==type]
    if mode == 'semantic' and query.strip() and values:
        # Reuse the module's existing pre-nearest state/type/source validation.
        # Reading never initializes ModuleStore, repairs outbox or tracks history.
        embedder = SharedEmbedder(service.core)
        store = object.__new__(ModuleStore)
        store.db = db
        store.vector_backend = SharedVectors(service.core,embedder.config)
        selection = dict(service._selection)
        if type:
            selection['types'] = [type] if type in selection.get('types',[]) else []
        matches = store.search(embedder.encode_query(query),limit=20,selection=selection)
        by_id = {item['key'][2]:item for item in values}
        best = {}
        for match in matches:
            sid = match['session']['id']
            if sid in by_id and (sid not in best or match['distance']<best[sid]['distance']):
                best[sid] = {**by_id[sid],'distance':match['distance']}
        values = sorted(best.values(),key=lambda item:(item['distance'],item['key']))
        for item in values:
            item.pop('_metadata',None)
        return {'records':values[offset:offset+limit], 'total':None,'has_more':offset+limit<len(values),'types':types,
                'diagnostics':{'scope':{'module':'session_search','collection':'sessions','selection':selection},
                               'limits':{'nearest_blocks':20},'returned_sessions':len(values)},
                'warnings':['Semantic search uses the existing module limit of 20 nearest blocks; session totals are unknown.']}
    if query:
        needle = query.casefold()
        values = [item for item in values if needle in (item['title']+'\n'+item['description']).casefold()]
    for item in values:
        item.pop('_metadata',None)
    return {'records':values[offset:offset+limit], 'total':len(values),'has_more':offset+limit<len(values),'types':types}
