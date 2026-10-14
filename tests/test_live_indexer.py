"""Explicit isolated live smoke: run with production Python; never real history."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
MODULE_ROOT = Path(__file__).resolve().parents[1]
from session_search.indexer import request_index


def record(kind, **payload):
    return (json.dumps({'type': kind, 'payload': payload}, ensure_ascii=False)+'\n').encode()


def message(text):
    return record('response_item', type='message', role='user',
                  content=[{'type': 'input_text', 'text': text}])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', default='session_search.server', help='MCP module to verify')
    args = parser.parse_args()
    stage = MODULE_ROOT
    evidence = {'fixture_only': True, 'checks': {}}
    with tempfile.TemporaryDirectory(prefix='codex-index-live-') as tmp:
        root = Path(tmp)
        home = root/'codex'; (home/'sessions').mkdir(parents=True); (home/'archived_sessions').mkdir()
        sock = root/'index.sock'; db = root/'index.sqlite'
        sid = '11111111-1111-4111-8111-111111111111'
        archid = '22222222-2222-4222-8222-222222222222'
        path = home/'sessions'/f'rollout-2026-10-12T00-00-00-{sid}.jsonl'
        archived = home/'archived_sessions'/f'rollout-2026-10-12T00-00-01-{archid}.jsonl'
        meta = record('session_meta', id=sid, cwd='/fixture/postgres')
        path.write_bytes(meta+message('Configure PostgreSQL logical replication for Debezium and check table publications.'))
        archived.write_bytes(record('session_meta', id=archid, cwd='/fixture/linux')+
                             message('Проверить сбой сна Linux Mint и восстановление окон рабочего стола.'))
        (home/'session_index.jsonl').write_text(json.dumps({'id':sid,'thread_name':'Fixture PostgreSQL'})+'\n')
        config = root/'config'
        (config/'session-search').mkdir(parents=True)
        (config/'session-search/sessions.json').write_text(json.dumps({'active':True,'archived':True}))
        env = dict(os.environ, XDG_CONFIG_HOME=str(config))
        process = None
        log = root/'indexer.log'
        def start():
            nonlocal process
            output = open(log,'ab')
            process = subprocess.Popen([sys.executable, '-m', 'session_search.indexer', '--codex-home',str(home),
                '--database',str(db),'--socket',str(sock),'--threads','2'],stdout=output,stderr=output,
                env=dict(env,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1'))
            output.close()
        def stop():
            nonlocal process
            if process is not None:
                process.terminate()
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=10)
                process = None
        def rpc(payload):
            return request_index(dict(payload,codex_home=str(home)), sock, timeout=15)
        def settle(expected_sessions=None, expected_vectors=None, changed_path=None):
            deadline = time.monotonic()+40
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise AssertionError('Indexer exited: '+log.read_text()[-1500:])
                try:
                    result=rpc({'op':'status'})
                    stats=result['stats']
                    current = True
                    if changed_path is not None:
                        with sqlite3.connect(db) as connection:
                            cp = connection.execute('SELECT checkpoint FROM sessions WHERE id=?',(sid,)).fetchone()
                        cp = json.loads(cp[0]) if cp else {}
                        current = cp.get('mtime_ns') == changed_path.stat().st_mtime_ns and cp.get('exhausted')
                    if (current) and (not result['index_pending'] and not result['errors'] and
                        (expected_sessions is None or stats['sessions']==expected_sessions) and
                        (expected_vectors is None or stats['vectors']==expected_vectors)):
                        return stats
                except (OSError,RuntimeError): pass
                time.sleep(.15)
            raise AssertionError('Indexer did not settle: '+log.read_text()[-1500:])
        def search(query,state='all'):
            return rpc({'op':'search','query':query,'state':state,'limit':10})['matches']
        def snapshot():
            with sqlite3.connect(db) as connection:
                return connection.execute('SELECT id,checkpoint FROM sessions ORDER BY id').fetchall()
        try:
            start(); initial=settle(2,2)
            result=search('Где была задача настроить логическую репликацию PostgreSQL?')
            assert result[0]['session']['id']==sid
            assert result[0]['text'].startswith('User: Configure PostgreSQL')
            evidence['checks']['cross_language_ru_to_en']=True
            result=search('Find the task about Linux Mint suspend failure','archived')
            assert result[0]['session']['id']==archid
            evidence['checks']['cross_language_en_to_ru_archived_filter']=True
            with path.open('ab') as stream:
                stream.write(record('event_msg',type='task_started',turn_id='clickhouse-turn')+
                             message('Create a ClickHouse read-only user and grant access to dashboard tables.'))
            settle(2,2,changed_path=path)
            assert 'ClickHouse' in search('Найти задачу создать пользователя ClickHouse')[0]['text']
            evidence['checks']['observer_append_embedded']=True
            cp=snapshot();stop();start();after=settle(2,2)
            assert snapshot()==cp
            evidence['checks']['restart_checkpoint_and_vector_count_unchanged']=True
            moved=home/'archived_sessions'/path.name;path.rename(moved)
            # Restart gives a bounded startup reconciliation instead of sleeping60s.
            stop();start();settle(2,2)
            assert search('репликация PostgreSQL','active')==[]
            assert search('репликация PostgreSQL','archived')[0]['session']['id']==sid
            evidence['checks']['archive_rename_state_filter']=True
            moved.write_bytes(meta+message('Fix the Linux network DNS resolver configuration.'))
            settle(2,2,changed_path=moved)
            assert 'DNS' in search('Найти задачу исправить настройки DNS')[0]['text']
            evidence['checks']['truncate_replacement_removes_old_vectors']=True
            archived.unlink();stop();start();settle(1,1)
            evidence['checks']['deleted_file_startup_reconciliation']=True
            if args.server:
                request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'session_search',
                    'arguments':{'codex_home':str(home),'query':'Как исправить настройки DNS?','state':'archived','limit':5,'include_snippets':True}}}
                output=subprocess.run([sys.executable,'-m',args.server],input=json.dumps(request)+'\n',text=True,
                    capture_output=True,timeout=20,env=dict(env,CODEX_SESSION_INDEX_SOCKET=str(sock)))
                assert output.returncode==0,output.stderr
                response=json.loads(output.stdout.strip().splitlines()[-1])
                assert not response['result'].get('isError'),response
                payload=json.loads(response['result']['content'][0]['text'])
                assert 'DNS' in json.dumps(payload),payload
                evidence['checks']['mcp_tools_call_semantic_search']=True
            evidence['initial_stats']=initial; evidence['post_restart_stats']=after
        finally:
            stop()
        assert not sock.exists(),'Socket survived orderly shutdown'
        evidence['checks']['process_and_socket_cleaned']=True
    (stage/'live-verification.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence))


if __name__=='__main__':
    main()
