"""Persisted session storage selection shared by search and indexing."""
import json
import os
from pathlib import Path
import tempfile

DEFAULT_SELECTION = {'active': True, 'archived': False}


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'session-search/sessions.json'


def read_selection(path=None):
    path = Path(path) if path is not None else config_path()
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return dict(DEFAULT_SELECTION)
    if not isinstance(value, dict) or any(type(value.get(state)) is not bool for state in DEFAULT_SELECTION):
        raise ValueError('Invalid session selection')
    return {state: value[state] for state in DEFAULT_SELECTION}


def save_selection(state, enabled, path=None):
    if state not in DEFAULT_SELECTION or type(enabled) is not bool:
        raise ValueError('Select active or archived sessions with a boolean value')
    path = Path(path) if path is not None else config_path()
    selection = read_selection(path)
    selection[state] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.sessions-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(selection, stream)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return selection
