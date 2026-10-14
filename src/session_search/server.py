"""Stdio MCP for Codex session search with local request history."""
import time
import uuid
import logging
from contextvars import ContextVar

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from . import indexer
from .settings import read_selection

_search_results = ContextVar("search_results", default=None)

SERVER_NAME = "session-search"
SERVER_VERSION = "0.7.5"


SESSION_FILE_RE = re.compile(
    r"^rollout-(?P<created>.+)-(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)


COMMON_SESSION_FILTER_SCHEMA = {
    "codex_home": {
        "type": "string",
        "description": "Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    },
    "state": {
        "type": "string",
        "enum": ["active", "archived", "all"],
        "description": "Session storage to inspect.",
    },
}


def write_message(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def text_content(text):
    return [{"type": "text", "text": text}]


def normalize(value):
    return str(value or "").casefold()


def required(arguments, name):
    value = arguments.get(name)
    if value is None or value == "":
        raise ValueError(f"Argument '{name}' is required")
    return str(value)


def enum_value(arguments, name, allowed, default):
    value = arguments.get(name) or default
    if value not in allowed:
        raise ValueError(f"Argument '{name}' must be one of: {', '.join(allowed)}")
    return value


def optional_int(arguments, name, default, minimum, maximum):
    value = arguments.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Argument '{name}' must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Argument '{name}' must be between {minimum} and {maximum}")
    return parsed


def codex_home(arguments):
    value = arguments.get("codex_home") or os.environ.get("CODEX_HOME") or "~/.codex"
    path = Path(str(value)).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"CODEX_HOME directory not found: {path}")
    return path


def active_sessions_dir(home):
    return home / "sessions"


def archived_sessions_dir(home):
    return home / "archived_sessions"


def session_index_path(home):
    return home / "session_index.jsonl"


def load_session_index(home):
    path = session_index_path(home)
    if not path.is_file():
        return {}

    index = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(record.get("id") or "").lower()
                if not session_id:
                    continue
                index[session_id] = {
                    "thread_name": record.get("thread_name"),
                    "updated_at": record.get("updated_at"),
                }
    except OSError:
        return {}
    return index


def relative_to_or_abs(path, root):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def session_state_for_path(path, home):
    active_root = active_sessions_dir(home).resolve()
    archive_root = archived_sessions_dir(home).resolve()
    resolved = path.resolve()
    if resolved == active_root or active_root in resolved.parents:
        return "active"
    if resolved == archive_root or archive_root in resolved.parents:
        return "archived"
    raise ValueError(f"Path is outside Codex session storage: {path}")


def iter_session_paths(home, state):
    roots = []
    if state in {"active", "all"}:
        roots.append(active_sessions_dir(home))
    if state in {"archived", "all"}:
        roots.append(archived_sessions_dir(home))

    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            yield path


def filename_parts(path):
    match = SESSION_FILE_RE.match(path.name)
    if not match:
        return {
            "id": None,
            "created_at": None,
            "name": path.stem,
        }
    return {
        "id": match.group("id").lower(),
        "created_at": match.group("created"),
        "name": path.stem,
    }


def first_json_record(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    return json.loads(line)
    except (OSError, json.JSONDecodeError):
        return None
    return None


def meta_from_session_file(path, session_index=None):
    parts = filename_parts(path)
    record = first_json_record(path)
    payload = {}
    if record and record.get("type") == "session_meta":
        payload = record.get("payload") or {}

    session_id = str(payload.get("id") or parts.get("id") or "")
    session_id = session_id.lower() or None
    file_name = parts.get("name")
    index_record = (session_index or {}).get(session_id) if session_id else None
    thread_name = None
    thread_name_updated_at = None
    if index_record:
        thread_name = index_record.get("thread_name") or None
        thread_name_updated_at = index_record.get("updated_at")
    fallback_name = session_id or file_name
    name_source = "thread_name" if thread_name else ("id" if session_id else "file_name")
    return {
        "id": session_id,
        "name": thread_name or fallback_name,
        "file_name": file_name,
        "thread_name": thread_name,
        "thread_name_updated_at": thread_name_updated_at,
        "name_source": name_source,
        "created_at": payload.get("timestamp") or parts.get("created_at"),
        "cwd": payload.get("cwd"),
        "originator": payload.get("originator"),
        "cli_version": payload.get("cli_version"),
        "source": payload.get("source"),
        "thread_source": payload.get("thread_source"),
        "forked_from_id": payload.get("forked_from_id"),
        "parent_thread_id": payload.get("parent_thread_id"),
        "model_provider": payload.get("model_provider"),
    }


def compact_text(value, limit=240):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def content_text(content):
    pieces = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
            elif isinstance(item, str):
                pieces.append(item)
    elif isinstance(content, str):
        pieces.append(content)
    return "\n".join(piece for piece in pieces if piece)


def user_message_from_record(record):
    payload = record.get("payload") or {}
    if record.get("type") == "event_msg" and payload.get("type") == "user_message":
        return payload.get("message")
    if record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        return content_text(payload.get("content"))
    return None


def is_context_message(text):
    value = str(text or "").lstrip()
    return value.startswith("# AGENTS.md instructions") or value.startswith("<environment_context>")


def scan_user_messages(path):
    first_message = None
    last_message = None
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = user_message_from_record(record)
                if not message or is_context_message(message):
                    continue
                count += 1
                if first_message is None:
                    first_message = message
                last_message = message
    except OSError:
        pass
    return {
        "first_user_message": compact_text(first_message) if first_message else None,
        "last_user_message": compact_text(last_message) if last_message else None,
        "user_message_count": count,
    }


def session_summary(path, home, include_preview=False, pins=None, session_index=None):
    stat = path.stat()
    state = session_state_for_path(path, home)
    index = load_session_index(home) if session_index is None else session_index
    meta = meta_from_session_file(path, index)
    pin_ids = pinned_ids(home) if pins is None else pins
    result = {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "file_name": meta.get("file_name"),
        "thread_name": meta.get("thread_name"),
        "thread_name_updated_at": meta.get("thread_name_updated_at"),
        "name_source": meta.get("name_source"),
        "state": state,
        "pinned": bool(meta.get("id") and meta.get("id") in pin_ids),
        "created_at": meta.get("created_at"),
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "size_bytes": stat.st_size,
        "cwd": meta.get("cwd"),
        "originator": meta.get("originator"),
        "source": meta.get("source"),
        "thread_source": meta.get("thread_source"),
        "forked_from_id": meta.get("forked_from_id"),
        "parent_thread_id": meta.get("parent_thread_id"),
        "cli_version": meta.get("cli_version"),
        "path": str(path),
        "relative_path": relative_to_or_abs(path, home),
    }
    if include_preview:
        result.update(scan_user_messages(path))
    return result


def find_session_paths(home, state, target):
    target_text = str(target or "").strip()
    if not target_text:
        raise ValueError("Session target is empty")

    candidate_path = Path(target_text).expanduser()
    if candidate_path.is_absolute() or "/" in target_text:
        path = candidate_path.resolve()
        session_state_for_path(path, home)
        if not path.is_file():
            raise ValueError(f"Session file not found: {path}")
        if state != "all" and session_state_for_path(path, home) != state:
            raise ValueError(f"Session is not {state}: {path}")
        return [path]

    matches = []
    target_lower = target_text.casefold()
    session_index = load_session_index(home)
    for path in iter_session_paths(home, state):
        summary = session_summary(path, home, include_preview=False, session_index=session_index)
        values = [
            summary.get("id"),
            summary.get("name"),
            summary.get("thread_name"),
            summary.get("file_name"),
            Path(summary.get("path")).name,
        ]
        if any(value and str(value).casefold() == target_lower for value in values):
            matches.append(path)
    return matches


def resolve_single_session(home, state, target):
    matches = find_session_paths(home, state, target)
    if not matches:
        raise ValueError(f"Session not found: {target}")
    if len(matches) > 1:
        names = ", ".join(str(path) for path in matches[:5])
        raise ValueError(f"Session target is ambiguous: {target}; matches: {names}")
    return matches[0]


def searchable_fields_for_path(path, home, session_index=None):
    summary = session_summary(path, home, include_preview=False, session_index=session_index)
    for key in [
        "id",
        "name",
        "thread_name",
        "file_name",
        "cwd",
        "originator",
        "source",
        "thread_source",
        "cli_version",
        "path",
        "relative_path",
    ]:
        value = summary.get(key)
        if value:
            yield key, str(value)


def searchable_texts_from_record(record):
    payload = record.get("payload") or {}
    if record.get("type") == "session_meta":
        for key in ["id", "timestamp", "cwd", "originator", "cli_version", "source", "thread_source", "model_provider"]:
            value = payload.get(key)
            if value:
                yield key, str(value)
    elif record.get("type") == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        if message and not is_context_message(message):
            yield "user_message", str(message)
    elif record.get("type") == "response_item" and payload.get("type") == "message":
        text = content_text(payload.get("content"))
        if text and not is_context_message(text):
            role = payload.get("role") or "message"
            yield f"{role}_message", text
    elif record.get("type") == "response_item" and payload.get("type") == "function_call":
        name = payload.get("name")
        if name:
            yield "tool_call", str(name)


def search_session_file(path, home, query, include_snippets, session_index=None):
    query_text = normalize(query)
    matched_fields = []
    snippets = []

    for field, value in searchable_fields_for_path(path, home, session_index=session_index):
        if query_text in normalize(value):
            matched_fields.append(field)
            if include_snippets:
                snippets.append({"field": field, "text": compact_text(value)})

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for field, value in searchable_texts_from_record(record):
                    if query_text in normalize(value):
                        if field not in matched_fields:
                            matched_fields.append(field)
                        if include_snippets and len(snippets) < 3:
                            snippets.append({"field": field, "text": compact_text(value)})
                if len(snippets) >= 3 and include_snippets:
                    break
    except OSError:
        pass

    return matched_fields, snippets


def tool_session_info(arguments):
    home = codex_home(arguments)
    state = enum_value(arguments, "state", ["active", "archived", "all"], "all")
    path = resolve_single_session(home, state, required(arguments, "session"))
    include_messages = arguments.get("include_messages", True) is not False
    result = session_summary(path, home, include_preview=include_messages, session_index=load_session_index(home))
    result["resume_command"] = f"codex resume {result.get('id') or result.get('name')}"
    return text_content(json.dumps(result, ensure_ascii=False, indent=2))


def tool_session_search(arguments):
    from .search_history import sanitize
    started_at, started = time.time(), time.monotonic()
    event_id = uuid.uuid4().hex
    token = _search_results.set([])
    def log(event):
        try:
            indexer.request_index({'op': 'search_history', 'event': sanitize(event),
                                   'codex_home': str(codex_home(arguments))}, timeout=2)
        except (OSError, ValueError, RuntimeError):
            # A history write failure must not replace the search result.
            logging.warning('Search history unavailable request_id=%s', event_id)
    log(dict(phase='start', id=event_id, started_at=started_at, parameters=arguments))
    try:
        content = _tool_session_search(arguments)
        payload = json.loads(content[0]['text'])
        results = _search_results.get() or [dict(session_id=item.get('id')) for item in payload.get('sessions', [])]
        event = dict(phase='finish', id=event_id, finished_at=time.time(), duration_seconds=time.monotonic()-started,
                     result_count=len(payload.get('sessions', [])), results=results)
    except Exception as exc:
        event = dict(phase='finish', id=event_id, finished_at=time.time(), duration_seconds=time.monotonic()-started,
                     error=type(exc).__name__ + ': ' + str(exc), results=[])
        log(event)
        raise
    else:
        log(event)
        return content
    finally:
        _search_results.reset(token)


def _tool_session_search(arguments):
    mode = enum_value(arguments, "mode", ["semantic", "literal"], "semantic")
    if mode == "semantic":
        return tool_session_semantic_search(arguments)
    home = codex_home(arguments)
    query = required(arguments, "query")
    state = enum_value(arguments, "state", ["active", "archived", "all"], "all")
    limit = optional_int(arguments, "limit", 20, 1, 200)
    include_snippets = arguments.get("include_snippets") is True
    pin_ids = pinned_ids(home)
    session_index = load_session_index(home)

    selection = read_selection()
    matches = []
    for path in iter_session_paths(home, state):
        if not selection[session_state_for_path(path, home)]:
            continue
        matched_fields, snippets = search_session_file(
            path,
            home,
            query,
            include_snippets,
            session_index=session_index,
        )
        if not matched_fields:
            continue
        item = session_summary(path, home, include_preview=False, pins=pin_ids, session_index=session_index)
        item["matched_fields"] = matched_fields
        if include_snippets:
            item["snippets"] = snippets
        matches.append(item)

    matches.sort(key=lambda item: item.get("mtime") or "", reverse=True)
    result = {
        "codex_home": str(home),
        "query": query,
        "state": state,
        "total": len(matches),
        "limit": limit,
        "sessions": matches[:limit],
        "session_selection": selection,
    }
    return text_content(json.dumps(result, ensure_ascii=False, indent=2))


def tool_session_semantic_search(arguments):
    home = codex_home(arguments)
    query = required(arguments, "query")
    state = enum_value(arguments, "state", ["active", "archived", "all"], "all")
    limit = optional_int(arguments, "limit", 20, 1, 200)
    try:
        result = indexer.request_index({"op": "search", "query": query, "state": state,
                                       "limit": limit, "codex_home": str(home)})
    except (OSError, TimeoutError, RuntimeError) as exc:
        raise ValueError(f"Session indexer unavailable: {exc}. Check codex-session-index.service") from exc
    if not result.get("ok"):
        raise ValueError(result.get("error", "Session indexer failed"))
    selection = read_selection()
    selected_matches = [match for match in result.get('matches', [])
                        if selection.get(match['state'], False) and state in ('all', match['state'])]
    if _search_results.get() is not None:
        _search_results.set([dict(session_id=m['session']['id'], chunk_id=m['id'], distance=m['distance']) for m in selected_matches])
    names = load_session_index(home)
    sessions = {}
    for match in selected_matches:
        metadata = match["session"]
        sid = metadata["id"]
        if sid not in sessions:
            entry = names.get(sid, {})
            name = entry.get("thread_name") if isinstance(entry, dict) else entry
            sessions[sid] = {
                "id": sid, "name": name or metadata.get("title") or sid,
                "cwd": metadata.get("cwd"), "path": metadata.get("path"),
                "state": match["state"], "distance": match["distance"],
                "resume_command": f"codex resume {sid}", "matched_fields": ["semantic_text"],
            }
            if arguments.get("include_snippets") is True:
                sessions[sid]["snippets"] = []
        if arguments.get("include_snippets") is True:
            sessions[sid]["snippets"].append({
                "text": match["text"], "chunk_id": match["id"],
                "start_offset": match["start_offset"], "end_offset": match["end_offset"],
                "distance": match["distance"],
                "source": {**{key: match.get(key) for key in ("turn_id", "role", "part", "question_start_offset")},
                           "sources": match.get("sources", [])},
            })
    payload = {"codex_home": str(home), "query": query, "mode": "semantic", "state": state,
               "session_selection": selection, "limit": limit, "total": len(sessions), "sessions": list(sessions.values()),
               "index": result.get("stats", {}), "index_pending": result.get("index_pending"),
               "note": "Distances rank candidates, not answer confidence. Read source context to verify task status. total counts returned sessions, not all relevant sessions."}
    return text_content(json.dumps(payload, ensure_ascii=False, indent=2))


def tool_session_resume_command(arguments):
    home = codex_home(arguments)
    state = enum_value(arguments, "state", ["active", "archived", "all"], "active")
    path = resolve_single_session(home, state, required(arguments, "session"))
    summary = session_summary(path, home, include_preview=False, session_index=load_session_index(home))
    target = summary.get("id") or summary.get("name")
    result = {
        "codex_home": str(home),
        "session": target,
        "state": summary.get("state"),
        "command": f"codex resume {target}",
        "note": "Archived sessions must be restored before resume.",
    }
    return text_content(json.dumps(result, ensure_ascii=False, indent=2))


def success(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def pinned_ids(home):
    # Preserve read-only metadata; no pin-management tools are exposed.
    path = home / "session_pins.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read session pins file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid session pins file: {path}")
    pins = data.get("pins") or {}
    if not isinstance(pins, dict):
        raise ValueError(f"Invalid session pins file pins object: {path}")
    return {str(sid).lower() for sid in pins if sid}


def tool_index_status(arguments):
    home = codex_home(arguments)
    try:
        result = indexer.request_index({"op": "status", "codex_home": str(home)})
    except (OSError, TimeoutError, RuntimeError) as exc:
        raise ValueError(f"Session indexer unavailable: {exc}. Check codex-session-index.service") from exc
    if not result.get("ok"):
        raise ValueError(result.get("error", "Session indexer failed"))
    return text_content(json.dumps(result, ensure_ascii=False, indent=2))



TOOLS = {
"session_info": {
        "description": "Return metadata for one Codex session selected by id, renamed session name, file name or path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_SESSION_FILTER_SCHEMA,
                "session": {"type": "string", "description": "Session id, renamed session name, file name or path."},
                "include_messages": {
                    "type": "boolean",
                    "description": "Include compact first/last user message previews. Default true.",
                },
            },
            "required": ["session"],
            "additionalProperties": False,
        },
        "handler": tool_session_info,
    },
"session_search": {
        "description": "Search conversation fragments by meaning using the independent local indexer. Set include_snippets=true to inspect evidence. mode=literal preserves exact substring search over names, paths and transcript. Semantic results can be incomplete while indexing; verify task state in source history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_SESSION_FILTER_SCHEMA,
                "query": {"type": "string", "description": "Search query."},
                "mode": {"type": "string", "enum": ["semantic", "literal"], "description": "Default semantic; literal uses existing substring search without indexer."},
                "limit": {"type": "integer", "description": "Maximum fragments in semantic mode or sessions in literal mode. Default 20, max 200."},
                "include_snippets": {
                    "type": "boolean",
                    "description": "Return matching fragments and source byte offsets (semantic), or compact excerpts (literal). Default false.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": tool_session_search,
    },
"session_resume_command": {
        "description": "Return the codex resume command for a selected active session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_SESSION_FILTER_SCHEMA,
                "session": {"type": "string", "description": "Session id, renamed session name, file name or path."},
            },
            "required": ["session"],
            "additionalProperties": False,
        },
        "handler": tool_session_resume_command,
    },
"index_status": {"description": "Return indexer queue and readiness status.", "inputSchema": {"type": "object", "properties": {"codex_home": COMMON_SESSION_FILTER_SCHEMA["codex_home"]}, "additionalProperties": False}, "handler": tool_index_status}
}


def handle_request(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return error(request_id, -32600, "Invalid Request")
    if request_id is None:
        return None
    params = message.get("params", {})
    if not isinstance(params, dict):
        return error(request_id, -32602, "params must be an object")
    if method == "initialize":
        return success(request_id, {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "ping":
        return success(request_id, {})
    if method == "tools/list":
        return success(request_id, {"tools": [{"name": name, "description": definition["description"], "inputSchema": definition["inputSchema"]} for name, definition in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            return error(request_id, -32602, f"Unknown tool: {name}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return error(request_id, -32602, "arguments must be an object")
        try:
            content = TOOLS[name]["handler"](arguments)
        except (ValueError, OSError) as exc:
            return success(request_id, {"content": text_content(str(exc)), "isError": True})
        return success(request_id, {"content": content, "isError": False})
    return error(request_id, -32601, f"Method not found: {method}")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            write_message(error(None, -32700, f"Parse error: {exc}"))
            continue
        response = handle_request(message)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()

