"""Preserved tests for the historical standalone daemon bridge."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from contextopus.core.operations import OperationRegistry
from .session_search_adapter import SessionSearch


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="External daemon uses Unix-domain sockets"
)
def test_status_existing_socket_never_searches_or_launches():
    # Darwin's sockaddr_un is short; pytest's nested temporary root may exceed it.
    with tempfile.TemporaryDirectory(
        prefix="css-", dir="/tmp" if os.name != "nt" else None
    ) as directory:
        path = Path(directory) / "s"
        observed = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen()
            server.settimeout(5)

            def respond():
                client, _ = server.accept()
                with client:
                    request = json.loads(client.makefile("rb").readline())
                    observed.append(request)
                    client.sendall(b'{"ok":true,"indexed":7}\n')

            thread = threading.Thread(target=respond)
            thread.start()
            result = SessionSearch(path).status()
            thread.join(5)
        assert not thread.is_alive()
    assert observed == [{"op": "status"}]
    assert result["daemon"]["indexed"] == 7
    assert result["core_rag_migrated"] is False


def test_registration_validates_query_arguments(tmp_path):
    registry = OperationRegistry()
    module = SessionSearch(tmp_path / "missing.sock")
    module.register(registry)
    status = registry.dispatch("query", "session_search.status")
    assert status["state"] == "unavailable"
    assert status["reason"]
    assert "session_search.open_tui" in registry.describe()["operations"]["execute"]


def executable_fixture(tmp_path, name):
    # A native executable is discoverable on each OS, unlike a POSIX shell stub.
    suffix = ".exe" if os.name == "nt" else ""
    command = tmp_path / (name + suffix)
    shutil.copyfile(sys.executable, command)
    command.chmod(0o755)
    return command


def test_separate_venv_discovers_installed_executable(tmp_path, monkeypatch):
    command = executable_fixture(tmp_path, "session-search")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    module = SessionSearch(tmp_path / "absent.sock")
    assert module.available()
    descriptor = module.launch_tui()
    assert Path(descriptor["argv"][0]).samefile(command)
    assert descriptor["argv"][1:] == ["tui"]
    assert descriptor["terminal_owner"] == "client"


def test_declared_command_is_argv_without_shell_interpolation(tmp_path):
    command = executable_fixture(tmp_path, "launcher with spaces")
    payload = "$(touch /tmp/should-not-exist); echo surprise"
    module = SessionSearch(command=[str(command), payload])
    argv = module.launch_tui()["argv"]
    assert Path(argv[0]).samefile(command)
    assert argv[1:] == [payload]
    with pytest.raises(ValueError):
        SessionSearch(command="session-search tui; echo surprise")


def test_unsupported_socket_platform_reports_unavailable_without_connecting(
    monkeypatch, tmp_path
):
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)

    def unexpected_socket(*args, **kwargs):
        pytest.fail("Unsupported daemon transport must not attempt a connection")

    monkeypatch.setattr(socket, "socket", unexpected_socket)
    module = SessionSearch(
        tmp_path / "missing.sock", command=[sys.executable, "-m", "session_search.tui"]
    )
    result = module.status()
    assert result["state"] == "unavailable"
    assert "does not support AF_UNIX" in result["reason"]
    assert result["transport"] == {"kind": "unix_socket", "supported": False}
    # Discovery and launch descriptors are independently portable operations.
    assert module.available()
    assert module.launch_tui()["argv"] == [sys.executable, "-m", "session_search.tui"]
    with pytest.raises(RuntimeError, match="Unix-domain sockets"):
        module.search("example")
