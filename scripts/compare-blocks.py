#!/usr/bin/env python3
"""Offline bounded comparison. Manifest and source text remain outside repo."""
import argparse, hashlib, json, math, resource, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from session_search.embedding import Embedder
from session_search.reader import read_increment
from session_search.store import Store

def reduced(vector, dimensions):
    part = vector[:dimensions]
    norm = math.sqrt(sum(x*x for x in part))
    return [x/norm for x in part]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    args.output.mkdir(parents=True, exist_ok=True)
    model = Embedder(threads=args.threads, dimensions=1024, max_length=2048)
    queries = [model.encode_query(q['question']) for q in manifest['queries']]
    report = {'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
              'model': model.identity, 'threads': args.threads, 'queries': manifest['queries'],
              'corpus': manifest['corpus'], 'rows': []}
    for budget in (512, 1024, 2048):
        started = time.perf_counter()
        all_chunks, sessions = [], []
        for source in manifest['corpus']:
            result = read_increment(source['path'], source['start_offset'],
                {'session': {'id': source['session_id']}}, max_tokens=budget,
                token_counter=model.count_tokens, max_bytes=source['end_offset']-source['start_offset'])
            if result['errors']:
                raise ValueError(result['errors'])
            all_chunks.extend(result['chunks'])
            sessions.append((dict(result['session'], path=source['path']), result['chunks']))
        unique = {c['text']: None for c in all_chunks}
        for text in unique:
            unique[text] = model.encode([text])[0]
        embedding_seconds = time.perf_counter()-started
        for dimensions in (256, 512, 1024):
            dbpath = args.output / f'blocks-{budget}-dim-{dimensions}.sqlite3'
            if dbpath.exists():
                raise ValueError(f'Refusing existing database: {dbpath}')
            store_started = time.perf_counter()
            store = Store(dbpath, model.identity, dimensions, chunking=f'comparison-{budget}')
            for session, chunks in sessions:
                store.ingest(session, {}, chunks)
            while job := store.claim_job():
                store.complete_job(job['id'], job['content_hash'], reduced(unique[job['text']], dimensions))
            storage_seconds = time.perf_counter()-store_started
            hits, timings, per_query = 0, [], []
            for query, vector in zip(manifest['queries'], queries):
                begin = time.perf_counter()
                results = store.search(reduced(vector, dimensions), limit=5)
                timings.append(time.perf_counter()-begin)
                rank = None
                for i, result in enumerate(results, 1):
                    if result['session']['id'] != query['session_id']:
                        continue
                    if any(s['start_offset']==query['record_offset'] and
                           s['char_start']<=query['char_start'] and s['char_end']>=query['char_end']
                           for s in result.get('sources', [])):
                        rank = i
                        break
                hits += rank is not None
                per_query.append({'question': query['question'], 'rank': rank})
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            row = dict(block_tokens=budget, dimensions=dimensions, vectors=store.stats()['vectors'],
                       median_block_chars=statistics.median(len(c['text']) for c in all_chunks),
                       embedding_seconds=embedding_seconds, storage_seconds=storage_seconds,
                       indexing_seconds=embedding_seconds+storage_seconds,
                       peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
                       database_bytes=store.database_bytes(), recall_at_5=hits/len(queries),
                       hits=hits, query_count=len(queries),
                       hit_at_1=sum(q['rank']==1 for q in per_query)/len(queries),
                       mrr_at_5=sum(1/q['rank'] for q in per_query if q['rank'])/len(queries),
                       mean_search_ms=1000*sum(timings)/len(timings), queries=per_query)
            store.close()
            report['rows'].append(row)
            (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({k:v for k,v in row.items() if k!='queries'}), flush=True)
if __name__ == '__main__':
    main()
