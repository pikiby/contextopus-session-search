"""Request-driven card module. Core owns the process, files, SQL and RAG."""
import threading
from .cards import Cards


class Service:
    def __init__(self, core):
        self.cards = Cards(core)
        self.lock = threading.RLock()

    def start(self):
        if not self.cards.core.rag.capabilities().get("object_ids"):
            raise RuntimeError("Session cards require the Core shared RAG object_ids extension; update Core first")
        self.cards.initialize()

    def close(self):
        pass

    def dispatch(self, operation, arguments, context):
        operations = {name: getattr(self.cards,name) for name in ('status','save','read','list','search','reconcile','delete')}
        operations['library'] = self.library
        name = operation.removeprefix('session_search.')
        if operation != 'session_search.' + name or name not in operations:
            raise ValueError('Unknown session card operation')
        with self.lock:
            return operations[name](**arguments)

    @staticmethod
    def wire(item):
        return {**item,'key':['session_search','cards',item['id']]}

    def library(self, action, collection=None, query='', mode='text', type=None, offset=0, limit=50, id=None, filters=None):
        if action == 'collections':
            return {'collections':[{'id':'cards','title':'Session cards'}],
                    'filters':[{'name':name,'label':label} for name,label in (
                        ('source','Source / client'),('project','Project'),('context','Context wikilink'),('tag','Tag'),('status','Status'))]}
        if collection not in (None,'','cards'):
            raise ValueError('Unknown session card collection')
        if action == 'record':
            item = self.cards.read(id)
            docs = self.cards.capture()
            targets = {}
            for ident,doc in docs.items():
                path = doc['path']
                for alias in (path,path.removesuffix('.md'),doc['location'],doc['location'].removesuffix('.md'),
                              'modules/session_search/data/sessions/'+doc['location'],
                              'modules/session_search/data/sessions/'+doc['location'].removesuffix('.md')):
                    targets[alias] = ident
            links=[]
            for ref in dict.fromkeys(item['links']+item['metadata'].get('contexts',[])+item['required_links']):
                target=ref[2:-2].split('|',1)[0].split('#',1)[0]
                if target in targets:
                    found=docs[targets[target]]['metadata']
                    links.append({'key':['session_search','cards',found['id']], 'title':found['title'],'description':found['description']})
                else:
                    links.append({'reference':ref,'title':ref[2:-2].split('|')[-1]})
            return {**self.wire(item),'links':links}
        if action != 'records':
            raise ValueError('Unknown library action')
        if type and type != 'session':
            return {'records':[],'total':0,'has_more':False,'types':['session']}
        diagnostics = None
        warnings = []
        if mode == 'semantic' and query.strip():
            result = self.cards.search(query,filters)
            items = result['results']
            diagnostics = result['diagnostics']
            if result['pending_count']:
                warnings.append(f"{result['pending_count']} cards need reconciliation before semantic retrieval.")
        else:
            items=[self.cards.summary(d) for d in self.cards.capture().values() if self.cards.matches(d,filters)]
            if query:
                items=[x for x in items if query.casefold() in (x['title']+' '+x['description']).casefold()]
            items.sort(key=lambda x:(x['title'].casefold(),x['id']))
        return {'records':[self.wire(x) for x in items[offset:offset+limit]],'total':len(items),
                'has_more':offset+limit<len(items),'types':['session'],'diagnostics':diagnostics,'warnings':warnings}


def create_service(core):
    return Service(core)
