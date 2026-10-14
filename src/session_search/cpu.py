"""Physical CPU topology and persistent indexer core selection."""
import json
import os
from pathlib import Path
import tempfile


def core_groups(root='/sys/devices/system/cpu', allowed=None):
    groups = {}
    for path in Path(root).glob('cpu[0-9]*'):
        cpu = int(path.name[3:])
        if allowed is not None and cpu not in allowed:
            continue
        if (path / 'online').exists() and (path / 'online').read_text().strip() != '1':
            continue
        topology = path / 'topology'
        key = (int((topology / 'physical_package_id').read_text()),
               int((topology / 'core_id').read_text()))
        if min(key) < 0:
            raise ValueError('Physical CPU topology unavailable')
        groups.setdefault(key, []).append(cpu)
    if not groups:
        raise ValueError('Physical CPU topology unavailable')
    return [sorted(groups[key]) for key in sorted(groups)]


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'session-search/cpu.json'


def read_cores(path=None):
    path = Path(path) if path is not None else config_path()
    try:
        value = json.loads(path.read_text())['cores']
    except FileNotFoundError:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('Invalid physical core setting')
    return value


def save_cores(count, path=None):
    path = Path(path) if path is not None else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.cpu-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'cores': count}, stream)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
