"""Thin installers delegate arguments and source previews to Core."""
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrapper is tested on Unix")
def test_shell_installer_preserves_paths_bound_root_plan_and_exit_code(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "contextopus"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$INSTALL_TEST_LOG"\nexit "${INSTALL_TEST_EXIT:-0}"\n')
    fake.chmod(0o755)
    log = tmp_path / "args"
    env = dict(os.environ, PATH=str(binary), INSTALL_TEST_LOG=str(log), INSTALL_TEST_EXIT="7")
    source = tmp_path / "module with spaces.zip"
    target = tmp_path / "root with spaces"
    result = subprocess.run(["/bin/sh", str(ROOT / "install.sh"), "--root", str(target), "--source", str(source), "--plan"], env=env)
    assert result.returncode == 7
    assert log.read_text().splitlines() == ["--root", str(target), "modules", "install", "session_search", "--source", str(source), "--plan"]
    assert not target.exists()
    env["INSTALL_TEST_EXIT"] = "0"
    result = subprocess.run(["/bin/sh", str(ROOT / "install.sh"), "--source", str(source)], env=env)
    assert result.returncode == 0
    assert log.read_text().splitlines() == ["modules", "install", "session_search", "--source", str(source)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrapper is tested on Unix")
def test_shell_installer_missing_core_points_to_starter(tmp_path):
    result = subprocess.run(["/bin/sh", str(ROOT / "install.sh")], env=dict(os.environ, PATH=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode == 127
    assert "contextopus-core/releases" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_powershell_delegates_to_isolated_core_executable(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("Native PowerShell is required for this wrapper test")
    import json
    import shlex
    binary = tmp_path / "bin"
    binary.mkdir()
    helper = tmp_path / "record_arguments.py"
    helper.write_text("import json,os,sys\nfrom pathlib import Path\nPath(os.environ['INSTALL_TEST_LOG']).write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(7)\n")
    if os.name == "nt":
        fake = binary / "contextopus.cmd"
        fake.write_text('@echo off\n"' + sys.executable + '" "' + str(helper) + '" %*\n')
    else:
        fake = binary / "contextopus"
        fake.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(helper)) + ' "$@"\n')
        fake.chmod(0o755)
    source = runpy.run_path(str(ROOT / "scripts/build-module.py"))["build"](tmp_path / "module")
    target = tmp_path / "root with spaces"
    log = tmp_path / "arguments.json"
    env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""), INSTALL_TEST_LOG=str(log))
    result = subprocess.run([shell, "-NoProfile", "-File", str(source / "install.ps1"), "-Root", str(target), "-Plan"], env=env, capture_output=True, text=True)
    assert result.returncode == 7, result.stdout + result.stderr
    assert json.loads(log.read_text()) == ["--root", str(target), "modules", "install", "session_search", "--source", str(source), "--plan"]
    assert not target.exists()
