"""Portable Core folder service: isolated SQL/RAG/child, no Unix daemon fixture."""
import json
from pathlib import Path
import runpy
import time

import pytest

from contextopus.startup import Application
from contextopus.core.resources import ProcessResources
from contextopus.core.embeddings import default_config
from contextopus.core.chunking import default_chunking

ROOT = Path(__file__).resolve().parents[1]


class Provider:
    def __init__(self, config):
        self.calls = []

    def embed(self, texts, purpose="document"):
        self.calls.append((list(texts), purpose))
        return [[1.0, 0.0] for _ in texts]

    def tokenizer(self):
        return lambda text, **kwargs: {"input_ids": list(range(len(text.split()) + 1))}

    def close(self):
        pass


@pytest.mark.parametrize("kind", ["folder", "zip"])
def test_folder_install_index_search_pause_and_restart_are_portable(tmp_path, kind):
    data_root = tmp_path / "root"
    builders = runpy.run_path(str(ROOT / "scripts/build-module.py"))
    release = (builders["build"](tmp_path / "module-release") if kind == "folder"
               else builders["build_zip"](tmp_path / "module.zip"))
    providers = []

    def factory(config):
        provider = Provider(config)
        providers.append(provider)
        return provider

    resources = ProcessResources(factory)
    app = Application(data_root, resources=resources, discover=False)
    config = default_config()
    config["profile"]["dimensions"] = 2
    app.index.embeddings = config
    app.index.chunking = default_chunking()
    app.index._generic_table(create=True)
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    transcript = home / "sessions/rollout-portable.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "portable", "cwd": str(tmp_path), "source": "cli"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Find the portable backup decision"}]}},
    ]
    original = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    transcript.write_bytes(original)
    try:
        resources.start_services()
        arguments = {"module": "session_search", "source": str(release)}
        preview = app.query("core.module_source_plan", arguments)
        # Registry reads may append normal analytics; preview changes no module code/schema.
        assert not (data_root / "core/storage/modules/session_search.sqlite3").exists()
        assert not (data_root / "modules/session_search").exists()
        arguments["source_revision"] = preview["revision"]
        assert app.execute("core.module_install", arguments)["state"] == "enabled"
        assert app.query("session_search.status")["integration"] == "core"
        app.execute("session_search.configure", {"codex_home": str(home)})
        app.execute("session_search.start")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = app.query("session_search.status")
            assert status["error"] is None, status
            if status["stats"]["vectors"] == 1 and not status["index_pending"]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(status)
        app.execute("session_search.stop")
        matches = app.query("session_search.search", {"query": "backup decision"})["matches"]
        assert matches[0]["session"]["id"] == "portable"
        assert transcript.read_bytes() == original
        app.execute("core.module_disable", {"module": "session_search"})
        app.execute("core.module_enable", {"module": "session_search"})
        app.module_manager.start_services()
        assert app.query("session_search.status")["stats"]["vectors"] == 1
        assert not app.query("session_search.status")["indexing"]
        assert len(providers) == 1
    finally:
        app.close()
        resources.close()
