"""Folder-runtime portability without emulating native OS acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys

from session_search import indexer
from session_search.store import Store


class Vectors:
    def put(self, *args): pass
    def delete(self, *args): pass
    def search(self, *args): return []


class Embedder:
    count_tokens = staticmethod(len)
    encode = staticmethod(lambda texts: [[1.0, 0.0] for _ in texts])
    encode_query = staticmethod(lambda text: [1.0, 0.0])


def test_missing_affinity_uses_labelled_logical_count_without_changing_affinity(tmp_path, monkeypatch):
    monkeypatch.delattr(os, 'sched_getaffinity', raising=False)
    monkeypatch.setattr(os, 'cpu_count', lambda: 6)
    def forbidden(*args, **kwargs):
        raise AssertionError('Linux topology and affinity must not be used')
    monkeypatch.setattr(indexer, 'core_groups', forbidden)
    monkeypatch.setattr(os, 'sched_setaffinity', forbidden, raising=False)
    store = Store(tmp_path / 'index.sqlite3', 'test-only', 2, vector_backend=Vectors())
    try:
        actor = indexer.Indexer(tmp_path, store, Embedder(), selection_reader=lambda: {'active': True, 'archived': False})
        status = actor.status()
        assert (status['cpu_cores'], status['cpu_unit']) == (6, 'logical')
        assert status['stats']['sessions'] == 0
        assert actor.step(indexing_enabled=False) is False
    finally:
        store.close()


def test_unavailable_sysfs_falls_back_and_available_physical_count_is_preserved(monkeypatch):
    monkeypatch.setattr(os, 'sched_getaffinity', lambda pid: {0, 1, 2, 3}, raising=False)
    monkeypatch.setattr(os, 'cpu_count', lambda: 4)
    monkeypatch.setattr(indexer, 'core_groups', lambda **kwargs: [[0, 1], [2, 3]])
    assert indexer.cpu_topology() == (2, 'physical')
    def unavailable(**kwargs):
        raise ValueError('Physical CPU topology unavailable')
    monkeypatch.setattr(indexer, 'core_groups', unavailable)
    assert indexer.cpu_topology() == (4, 'logical')
    monkeypatch.setattr(os, 'cpu_count', lambda: None)
    assert indexer.cpu_topology() == (None, 'unknown')


def test_folder_import_and_actor_setup_do_not_require_fcntl_or_linux_affinity(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    code = r"""
import importlib.abc
import json
import os
from pathlib import Path
import sys
class NoFcntl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'fcntl':
            raise ModuleNotFoundError('Simulated platform without fcntl', name='fcntl')
sys.modules.pop('fcntl', None)
sys.meta_path.insert(0, NoFcntl())
if hasattr(os, 'sched_getaffinity'):
    del os.sched_getaffinity
sys.path.insert(0, sys.argv[1])
from session_search.module_service import Service
from session_search import server
from session_search.indexer import Indexer
from session_search.store import Store
class Vectors:
    def put(self, *args): pass
    def delete(self, *args): pass
    def search(self, *args): return []
class Embedder:
    count_tokens = staticmethod(len)
    encode = staticmethod(lambda texts: [[1.0, 0.0] for _ in texts])
    encode_query = staticmethod(lambda text: [1.0, 0.0])
root = Path(sys.argv[2])
store = Store(root / 'index.sqlite3', 'test-only', 2, vector_backend=Vectors())
try:
    actor = Indexer(root, store, Embedder(), selection_reader=lambda: {'active': True, 'archived': False})
    assert actor.step(indexing_enabled=False) is False
    assert actor.status()['stats']['sessions'] == 0
    assert actor.status()['cpu_unit'] in ('logical', 'unknown')
    assert 'fcntl' not in sys.modules
    print(json.dumps({'folder_imported': True, 'actor_initialized': True}))
finally:
    store.close()
"""
    completed = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(source), str(tmp_path)],
                               text=True, capture_output=True, check=True)
    assert json.loads(completed.stdout) == {'folder_imported': True, 'actor_initialized': True}
