"""Incremental canonical Codex conversation extraction; no inference dependencies."""
import copy
import hashlib
import json
import re


_CONTEXT = re.compile(r"<(environment_context|permissions instructions|collaboration_mode|skills_instructions|system_reminder|turn_aborted|user_instructions|INSTRUCTIONS)\b[^>]*>.*?</\1>", re.S | re.I)
_INJECTED_PREFIXES = ("# AGENTS.md instructions for ", "<environment_context>", "<user_instructions>", "This is a summary of the conversation", "You are an AI assistant")


def _clean_user(text):
    text = text.strip()
    # These records are harness-created user messages, not user requests.
    if text.startswith("# AGENTS.md instructions for "):
        end = text.find("</INSTRUCTIONS>")
        return _CONTEXT.sub("", text[end + len("</INSTRUCTIONS>"):]).strip() if end >= 0 else ""
    text = _CONTEXT.sub("", text).strip()
    if text.startswith(_INJECTED_PREFIXES):
        return ""
    return text


def _pieces(text, max_chars, max_tokens, counter):
    """Deterministic Unicode-safe splits, optionally bounded by actual tokens."""
    while text:
        size = min(len(text), max_chars)
        if counter:
            while size and counter(text[:size]) > max_tokens:
                size //= 2
            if not size:
                raise ValueError("A single character exceeds max_tokens")
        if size < len(text):
            boundary = max(text.rfind("\n", 0, size), text.rfind(" ", 0, size))
            if boundary >= size // 2:
                size = boundary + 1
        yield text[:size]
        text = text[size:]


def read_increment(path, offset=0, state=None, *, max_chars=1536,
                   max_tokens=384, token_counter=None, max_bytes=1024 * 1024):
    """Read newline-terminated records from offset and return serializable state.

    Caller must reset offset/state on replacement or truncation. Byte coordinates
    cover the canonical source record; contextual user text can come from an
    earlier record (question_start_offset). No event-message text is indexed.
    Without a token_counter max_chars is the hard limit, not a token guarantee.
    """
    if max_chars < 64 or max_tokens < 16 or offset < 0 or max_bytes < 1:
        raise ValueError("Invalid chunk limit or offset")
    state = copy.deepcopy(state or {})
    state.setdefault("session", {})
    chunks, errors = [], []
    initial_offset = offset
    with open(path, "rb") as stream:
        stream.seek(offset)
        while True:
            if offset - initial_offset >= max_bytes:
                break
            start = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            end = stream.tell()
            offset = end
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                errors.append({"start_offset": start, "end_offset": end, "error": "invalid_json"})
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = item.get("type")
            if kind == "session_meta":
                # Fork rollouts may include copied parent metadata after their own header.
                if not state["session"].get("id"):
                    state["session"] = {k: payload[k] for k in ("id", "timestamp", "cwd", "parent_thread_id", "forked_from_id") if k in payload}
                continue
            if kind == "turn_context" or (kind == "event_msg" and payload.get("type") == "task_started"):
                if payload.get("turn_id"):
                    state["turn_id"] = payload["turn_id"]
                continue
            if kind != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant") or payload.get("channel") in ("analysis", "summary"):
                continue
            content = payload.get("content", [])
            text = "\n".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") in ("input_text", "output_text") and isinstance(c.get("text"), str))
            text = _clean_user(text) if role == "user" else text.strip()
            if not text:
                continue
            sid = state["session"].get("id")
            if not sid:
                raise ValueError("Canonical message encountered before session_meta.id")
            if role == "user":
                state["question"] = text[:max_chars // 4]
                state["question_start_offset"] = start
                state["question_turn_id"] = state.get("turn_id")
            prefix = ""
            if role == "assistant" and state.get("question"):
                question = state["question"]
                prefix = "User: " + question + "\nAssistant: "
                # Context must not consume the whole token budget.
                while prefix and token_counter and token_counter(prefix) > max_tokens // 3:
                    question = question[:len(question) // 2]
                    prefix = "User: " + question + "\nAssistant: " if question else ""
            budget = max_chars - len(prefix)
            # Count the complete embedding text: tokenizer boundaries across the
            # context prefix need not have additive token counts.
            counter = (lambda segment: token_counter(prefix + segment)) if token_counter else None
            for part, segment in enumerate(_pieces(text, budget, max_tokens, counter)):
                body = prefix + segment
                digest = hashlib.sha256(body.encode()).hexdigest()
                key = f"{sid}:{start}:{end}:{part}"
                chunks.append({"id": hashlib.sha256(key.encode()).hexdigest(), "session_id": sid,
                               "turn_id": state.get("turn_id"), "role": role,
                               "start_offset": start, "end_offset": end, "part": part,
                               "question_start_offset": state.get("question_start_offset") if prefix else None,
                               "text": body, "text_hash": digest})
    return {"session": state["session"], "next_offset": offset, "state": state,
            "chunks": chunks, "errors": errors}
