# Non-English strings are intentional Unicode/UTF-8 search and JSONL fixtures.
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from session_search.tui_core_backend import Backend, launch_descriptor


class CoreBackendTests(unittest.TestCase):
    def application(self, snapshot=None, settings=None):
        app = Mock()
        snapshot = snapshot or {"module": "session_search", "integration": "core", "state": "stopped", "indexing": False,
            "stats": {"sessions": 2, "chunks": 5, "vectors": 3, "jobs": {"pending": 2}},
            "database_bytes": 8192, "search_metrics": {"calls": 7}}
        settings = settings or {"config": {"codex_home": "/fixture", "block_tokens": 512, "autostart": False},
            "selection": {"active": True, "archived": False, "types": ["main", "subagent"]}}
        app.query.side_effect = lambda name, arguments=None: snapshot if name.endswith(".status") else settings
        return app

    def test_stopped_counts_use_only_registered_queries(self):
        app = self.application()
        result = Backend(application=app).status()
        self.assertEqual((result["sessions"], result["chunks"], result["vectors"], result["pending"]), (2, 5, 3, 2))
        self.assertEqual(result["service"], "inactive")
        self.assertFalse(result["indexer_running"])
        self.assertTrue(result["index_available"])
        self.assertEqual(result["search_metrics"]["calls"], 7)
        self.assertEqual(result["database_bytes"], 8192)
        self.assertEqual([c.args[0] for c in app.query.call_args_list], ["session_search.status", "session_search.settings"])
        app.execute.assert_not_called()

    def test_legacy_snapshot_is_not_presented_as_core_during_replacement(self):
        # A switch between status and settings must not turn the old adapter's
        # shape into a stopped/connected folder-service snapshot.
        app = self.application(snapshot={"module": "session_search", "integration": "external_daemon",
            "state": "unavailable", "reason": "socket missing"})
        with self.assertRaisesRegex(RuntimeError, "legacy adapter"):
            Backend(application=app).status()
        app.query.assert_called_once_with("session_search.status")
        app.execute.assert_not_called()

    def test_unconfigured_does_not_claim_index_available(self):
        app = self.application(settings={"config": None, "selection": {"active": True, "archived": False, "types": ["main"]}})
        self.assertFalse(Backend(application=app).status()["index_available"])
        app.execute.assert_not_called()

    def test_incompatible_core_gives_feature_error(self):
        app = Mock()
        app.query.side_effect = ValueError("Unknown operation")
        with self.assertRaisesRegex(RuntimeError, "module services"):
            Backend(application=app).status()
        app.execute.assert_not_called()

    def test_controls_use_core_without_changing_source_or_types(self):
        app = self.application()
        backend = Backend(application=app)
        backend.action("start")
        backend.action("stop")
        backend.action("enable")
        backend.set_selection("archived", True)
        self.assertEqual([c.args for c in app.execute.call_args_list], [
            ("session_search.start",), ("session_search.stop",),
            ("session_search.configure", {"codex_home": "/fixture", "block_tokens": 512, "autostart": True}),
            ("session_search.selection", {"active": True, "archived": True, "types": ["main", "subagent"]})])
        with self.assertRaises(ValueError):
            backend.action("restart-systemd")
        with self.assertRaises(ValueError):
            backend.set_selection("main", True)

    def test_search_is_one_core_query(self):
        app = Mock()
        Backend(application=app).search("привет", state="archived", mode="literal", limit=12)
        app.query.assert_called_once_with("session_search.search", {"query": "привет", "state": "archived", "mode": "literal", "limit": 12})
        app.execute.assert_not_called()

    def test_uses_existing_core_remote_client_and_explicit_coordinates(self):
        with tempfile.TemporaryDirectory() as directory, patch("contextopus.integrations.remote.RemoteApplication") as remote:
            backend = Backend(directory, "selected", "http://127.0.0.1:9999/mcp")
            remote.assert_called_once_with(Path(directory).resolve(), context="selected", endpoint="http://127.0.0.1:9999/mcp")
            backend.close()
            remote.return_value.close.assert_called_once_with()
        app = self.application()
        Backend(application=app).close()
        app.close.assert_not_called()

    def test_missing_root_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaisesRegex(ValueError, "Existing Contextopus root"):
                Backend(missing)
            self.assertFalse(missing.exists())

    def test_launch_descriptor_keeps_root_and_context_across_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("modules/session_search/package", "modules/session_search/.releases/revision/package"):
                package = root / relative
                package.mkdir(parents=True)
                descriptor = launch_descriptor(package / "session_search/module_service.py", "work")
                self.assertEqual(descriptor["argv"][1:], [str(package / "tui_launch.py"), "--root", str(root), "--context", "work"])
        with self.assertRaises(ValueError):
            launch_descriptor(__file__)


class TuiCliTests(unittest.TestCase):
    def test_explicit_coordinates_and_client_close(self):
        from session_search.tui import main
        with patch("session_search.tui_core_backend.Backend") as backend, patch("session_search.tui.SessionSearchApp") as app:
            app.return_value.backend = backend.return_value
            main(["--root", "/existing", "--context", "selected", "--endpoint", "http://127.0.0.1:9999/mcp"])
            backend.assert_called_once_with("/existing", "selected", "http://127.0.0.1:9999/mcp")
            app.assert_called_once_with(backend.return_value)
            app.return_value.run.assert_called_once_with(inline=True, mouse=False)
            backend.return_value.close.assert_called_once_with()


class FolderPackageTests(unittest.TestCase):
    def test_build_is_allowlisted_and_refuses_replacement(self):
        import runpy
        root = Path(__file__).resolve().parents[1]
        build = runpy.run_path(str(root / "scripts/build-module.py"))["build"]
        with tempfile.TemporaryDirectory() as directory:
            output = build(Path(directory) / "session_search")
            manifest = json.loads((output / "module.json").read_text())
            self.assertEqual(manifest["service"]["factory"], "worker.py:create_service")
            self.assertTrue(manifest["rag"])
            self.assertNotIn("dependencies", manifest)
            self.assertEqual(manifest["tui"], "session_search.open_tui")
            self.assertTrue((output / "package/migrations").is_dir())
            for name in ("LICENSE", "NOTICE", "install.sh", "install.ps1"):
                self.assertEqual((output / name).read_bytes(), (root / name).read_bytes())
            self.assertTrue((output / "package/session_search/module_service.py").is_file())
            self.assertFalse((output / "package/session_search/embedding.py").exists())
            self.assertFalse((output / "package/session_search/core_module.py").exists())
            self.assertFalse(any(p.suffix in {".sqlite3", ".jsonl", ".pyc"} or p.name == ".venv" for p in output.rglob("*")))
            with self.assertRaisesRegex(ValueError, "already exists"):
                build(output)


    def test_zip_matches_folder_and_retains_self_contained_guide(self):
        import runpy
        import zipfile
        root = Path(__file__).resolve().parents[1]
        builders = runpy.run_path(str(root / "scripts/build-module.py"))
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            folder = builders["build"](directory / "folder")
            archive = builders["build_zip"](directory / "module.zip")
            with zipfile.ZipFile(archive) as bundle:
                expected = {"session_search/" + p.relative_to(folder).as_posix(): p.read_bytes()
                            for p in folder.rglob("*") if p.is_file()}
                self.assertEqual(set(bundle.namelist()), set(expected))
                for name, content in expected.items():
                    self.assertEqual(bundle.read(name), content)
                self.assertEqual(bundle.read("session_search/README.md"),
                                 (root / "docs/module.md").read_bytes())
            original = archive.read_bytes()
            import hashlib
            checksum = Path(str(archive) + ".sha256")
            self.assertEqual(checksum.read_text(), hashlib.sha256(original).hexdigest() + "  module.zip\n")
            with self.assertRaisesRegex(ValueError, "already exists"):
                builders["build_zip"](archive)
            self.assertEqual(archive.read_bytes(), original)
            archive.unlink()
            with self.assertRaisesRegex(ValueError, "already exists"):
                builders["build_zip"](archive)
            self.assertFalse(archive.exists())
