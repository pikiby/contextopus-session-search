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


BLOCK_TOKENS = 512
BLOCK_VERSION = 'conversation-blocks-v1'


def read_increment(path, offset=0, state=None, *, max_chars=None,
                   max_tokens=BLOCK_TOKENS, token_counter=None, max_bytes=1024 * 1024):
    """Append messages to a stable tail; return upserts, never duplicate prefixes.

    Source spans cover canonical JSONL records, plus character ranges in cleaned
    message text. Final blocks remain unchanged; the tail ID survives appends.
    State and returned upserts must be committed atomically with next_offset.
    """
    max_chars = max_chars if max_chars is not None else max_tokens * 8
    if max_chars < 64 or max_tokens < 16 or offset < 0 or max_bytes < 1:
        raise ValueError('Invalid chunk limit or offset')
    state = copy.deepcopy(state or {})
    state.setdefault('session', {})
    state.setdefault('block_number', 0)
    chunks, errors = {}, []
    initial_offset = offset

    def fits(text):
        return len(text) <= max_chars and (not token_counter or token_counter(text) <= max_tokens)

    def publish(final=False):
        tail = state.get('tail')
        if not tail:
            return
        tail['final'] = final
        tail['text_hash'] = hashlib.sha256(tail['text'].encode()).hexdigest()
        chunks[tail['id']] = copy.deepcopy(tail)
        if final:
            state.pop('tail')
            state['block_number'] += 1

    def append(text, role, start, end):
        consumed = 0
        while text:
            tail = state.get('tail')
            base = tail['text'] + '\n' if tail else ''
            label = role.capitalize() + ': '
            candidate = base + label + text
            size = len(text)
            if not fits(candidate):
                lo, hi = 0, min(len(text), max_chars)
                while lo < hi:
                    mid = (lo+hi+1)//2
                    if fits(base+label+text[:mid]):
                        lo = mid
                    else:
                        hi = mid-1
                size = lo
                if not size:
                    if tail:
                        publish(final=True)
                        continue
                    raise ValueError('Chunk budget cannot hold one character and role')
                boundary = max(text.rfind(' ',0,size),text.rfind('\n',0,size))
                if boundary >= size*3//4:
                    size = boundary+1
                while size and not fits(base+label+text[:size]):
                    size -= 1
                if not size:
                    raise ValueError('Chunk budget cannot hold token boundary')
            if not tail:
                sid = state['session']['id']
                key = f"{sid}:{BLOCK_VERSION}:{state['block_number']}"
                tail = dict(id=hashlib.sha256(key.encode()).hexdigest(),session_id=sid,
                            start_offset=start,end_offset=end,part=state['block_number'],
                            turn_id=state.get('turn_id'),role='conversation',text='',sources=[])
                state['tail'] = tail
            tail['text'] = base+label+text[:size]
            tail['end_offset'] = end
            tail['sources'].append(dict(role=role,turn_id=state.get('turn_id'),start_offset=start,
                                        end_offset=end,char_start=consumed,char_end=consumed+size))
            consumed += size
            text = text[size:]
            publish(final=bool(text))

    with open(path, 'rb') as stream:
        stream.seek(offset)
        while offset-initial_offset < max_bytes:
            start = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b'\n'):
                break
            end = stream.tell()
            offset = end
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                errors.append(dict(start_offset=start,end_offset=end,error='invalid_json'))
                continue
            payload = item.get('payload')
            if not isinstance(payload,dict):
                continue
            kind = item.get('type')
            if kind == 'session_meta':
                if not state['session'].get('id'):
                    state['session'] = {k:payload[k] for k in ('id','timestamp','cwd','parent_thread_id','forked_from_id') if k in payload}
                continue
            if kind == 'turn_context' or (kind == 'event_msg' and payload.get('type') == 'task_started'):
                if payload.get('turn_id'):
                    state['turn_id'] = payload['turn_id']
                continue
            if kind != 'response_item' or payload.get('type') != 'message':
                continue
            role = payload.get('role')
            if role not in ('user','assistant') or payload.get('channel') in ('analysis','summary'):
                continue
            content = payload.get('content',[])
            text = '\n'.join(c['text'] for c in content if isinstance(c,dict) and c.get('type') in ('input_text','output_text') and isinstance(c.get('text'),str))
            text = _clean_user(text) if role == 'user' else text.strip()
            if not text:
                continue
            if not state['session'].get('id'):
                raise ValueError('Canonical message encountered before session_meta.id')
            append(text,role,start,end)
    return dict(session=state['session'],next_offset=offset,state=state,chunks=list(chunks.values()),errors=errors)
