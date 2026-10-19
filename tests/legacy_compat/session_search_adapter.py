"""Historical standalone daemon bridge retained only as a test fixture.

This is not the current Session Search folder module or a Core contribution.
It never opens session databases or JSONL; the standalone actor owns its index.
The fixture preserves the earlier adapter contract for compatibility coverage.
"""

from pathlib import Path
import importlib.util
import json
import os
import socket
import shutil
import sys
from contextopus.core.operations import OperationSpec

MAX_BYTES = 16 * 1024 * 1024


class SessionSearch:
    def __init__(self, socket_path=None, timeout=3, command=None):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        default = (
            Path(runtime) / "codex-session-index.sock"
            if runtime
            else Path.home() / ".cache/codex-session-index/index.sock"
        )
        self.socket_path = str(
            socket_path or os.environ.get("CODEX_SESSION_INDEX_SOCKET") or default
        )
        self.timeout = timeout
        if command is not None and (
            not isinstance(command, (list, tuple))
            or not command
            or any(
                not isinstance(part, str) or not part or "\0" in part
                for part in command
            )
        ):
            raise ValueError("Module command must be a nonempty argv list")
        self.command = tuple(command) if command is not None else None

    def launch_command(self):
        if self.command is not None:
            executable = shutil.which(self.command[0])
            return [executable, *self.command[1:]] if executable else None
        executable = shutil.which("session-search")
        if executable:
            return [executable, "tui"]
        if importlib.util.find_spec("session_search") is not None:
            return [sys.executable, "-m", "session_search.tui"]
        return None

    def available(self):
        return self.launch_command() is not None

    def request(self, payload):
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError(
                "The external session-search daemon requires Unix-domain sockets; "
                "this Python platform does not support AF_UNIX"
            )
        encoded = json.dumps(payload).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(encoded)
            with client.makefile("rb") as stream:
                raw = stream.readline(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
            raise RuntimeError("Incomplete or oversized session-search response")
        response = json.loads(raw)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Session-search request failed"))
        return response

    def status(self):
        try:
            return {
                "module": "session_search",
                "integration": "external_daemon",
                "core_rag_migrated": False,
                "available": self.available(),
                "launch_command": self.launch_command(),
                "socket_path": self.socket_path,
                "transport": {
                    "kind": "unix_socket",
                    "supported": hasattr(socket, "AF_UNIX"),
                },
                "daemon": self.request({"op": "status"}),
            }
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "module": "session_search",
                "integration": "external_daemon",
                "core_rag_migrated": False,
                "available": self.available(),
                "launch_command": self.launch_command(),
                "socket_path": self.socket_path,
                "transport": {
                    "kind": "unix_socket",
                    "supported": hasattr(socket, "AF_UNIX"),
                },
                "state": "unavailable",
                "reason": str(exc),
            }

    def search(self, query, state="all", limit=10):
        return self.request(
            {"op": "search", "query": query, "state": state, "limit": limit}
        )

    def launch_tui(self):
        command = self.launch_command()
        if command is None:
            raise RuntimeError(
                "Install session-search with its TUI dependencies to open its interface"
            )
        return {
            "ok": True,
            "argv": command,
            "terminal_owner": "client",
            "integration": "external_daemon",
        }

    def register(self, registry):
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        registry.register(
            OperationSpec(
                "session_search.status",
                "query",
                empty,
                lambda ctx: self.status(),
                "Read existing daemon status without starting it",
            )
        )
        registry.register(
            OperationSpec(
                "session_search.search",
                "query",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "state": {"enum": ["all", "active", "archived"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                lambda ctx, **args: self.search(**args),
                "Search the externally owned existing index",
            )
        )
        registry.register(
            OperationSpec(
                "session_search.open_tui",
                "execute",
                empty,
                lambda ctx: self.launch_tui(),
                "Open the installed session-search TUI",
            )
        )
