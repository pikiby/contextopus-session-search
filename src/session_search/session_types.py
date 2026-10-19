"""Classify declared Codex provenance, never infer main from missing data."""
import json

TYPES = ("main", "subagent", "guardian", "exec", "unknown")


def classify(metadata):
    source = metadata.get("source")
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            pass
    parent = metadata.get("parent_thread_id")
    if source in ("cli", "vscode"):
        kind = "main"
    elif source == "exec":
        kind = "exec"
    elif isinstance(source, dict) and isinstance(source.get("subagent"), dict):
        sub = source["subagent"]
        if isinstance(sub.get("thread_spawn"), dict):
            parent = sub["thread_spawn"].get("parent_thread_id", parent)
            kind = "subagent"
        elif sub.get("other") == "guardian":
            kind = "guardian"
        else:
            kind = "unknown"
    else:
        kind = "unknown"
    return dict(session_type=kind, parent_thread_id=parent)


def read_provenance(path):
    with open(path, 'rb') as stream:
        for _ in range(20):
            raw = stream.readline()
            if not raw or not raw.endswith(b'\n'):
                break
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if isinstance(item, dict) and item.get('type') == 'session_meta':
                meta = item.get('payload', {})
                if isinstance(meta, dict):
                    return dict(classify(meta), source=meta.get('source'), forked_from_id=meta.get('forked_from_id'))
    return dict(session_type='unknown', parent_thread_id=None)
