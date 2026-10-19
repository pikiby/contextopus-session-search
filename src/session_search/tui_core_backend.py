"""TUI presentation adapter for the existing Core registry/client.

No local owner, socket daemon, model, database or service manager is opened here.
"""
from pathlib import Path
import sys

MODULE = "session_search"


class Backend:
    def __init__(self, root=None, context=None, endpoint="http://127.0.0.1:8765/mcp", *, application=None):
        self._owns_application = application is None
        if application is None:
            from contextopus.integrations.remote import RemoteApplication
            if root is None:
                from contextopus.interfaces.cli.main import default_root
                root = default_root()
            root = Path(root).expanduser().resolve()
            if not root.is_dir():
                raise ValueError("Existing Contextopus root required; select it with --root")
            application = RemoteApplication(root, context=context, endpoint=endpoint)
        self.application = application

    def settings(self):
        return self.application.query(MODULE + ".settings")

    def status(self):
        try:
            snapshot = self.application.query(MODULE + ".status")
            if not isinstance(snapshot, dict) or snapshot.get("integration") != "core" or snapshot.get("module") != MODULE:
                raise RuntimeError("The legacy adapter or an incompatible session-search implementation is selected; complete folder-module installation and readiness before opening this TUI")
            settings = self.settings()
        except Exception as exc:
            raise RuntimeError("Session-search Core module unavailable; enable the folder module on a Core with module services, child SQL transactions and shared embeddings/RAG: " + str(exc)) from exc
        config = settings.get("config") or {}
        selection = settings.get("selection") or snapshot.get("session_selection") or {"active": True, "archived": False, "types": ["main"]}
        stats = snapshot.get("stats") or {}
        jobs = stats.get("jobs")
        running = bool(snapshot.get("indexing"))
        error = snapshot.get("error")
        result = dict(snapshot, service="failed" if error else "active" if running else "inactive",
                      indexer_running=running, indexer_managed=True,
                      index_available=bool(config), service_error=error,
                      autostart="enabled" if config.get("autostart") else "disabled",
                      session_selection=selection, mcp_transport="Core", mcp_connected=True)
        result.update({key: stats.get(key) for key in ("sessions", "chunks", "vectors")})
        result["pending"] = sum(jobs.get(key, 0) for key in ("pending", "queued", "retry", "running")) if isinstance(jobs, dict) else None
        return result

    def action(self, action):
        if action in {"start", "stop"}:
            value = self.application.execute(MODULE + "." + action)
        elif action in {"enable", "disable"}:
            config = self.settings().get("config")
            if not config:
                return {"ok": False, "output": "Configure session_search through Core first"}
            value = self.application.execute(MODULE + ".configure", {
                "codex_home": config["codex_home"], "block_tokens": config.get("block_tokens", 512),
                "autostart": action == "enable"})
        else:
            raise ValueError("Unknown indexing action")
        return {"ok": True, "result": value}

    def set_selection(self, state, enabled):
        if state not in {"active", "archived"} or type(enabled) is not bool:
            raise ValueError("Select active or archived sessions with a boolean value")
        settings = self.settings()
        selection = dict(settings.get("selection") or {"active": True, "archived": False, "types": ["main"]})
        selection.setdefault("types", ["main"])
        selection[state] = enabled
        return {"ok": True, "result": self.application.execute(MODULE + ".selection", selection)}

    def search(self, query, state="all", mode="semantic", limit=20):
        return self.application.query(MODULE + ".search", {"query": query, "state": state, "mode": mode, "limit": limit})

    def close(self):
        if self._owns_application:
            self.application.close()


def launch_descriptor(package_file, context=None):
    """Resolve the actual installed folder, including versioned module releases."""
    package = next((p for p in Path(package_file).resolve().parents if p.name == "package"), None)
    if package is None:
        raise ValueError("TUI must be launched from a prepared folder module")
    directory = next((p for p in package.parents if p.name == MODULE and p.parent.name == "modules"), None)
    if directory is None:
        raise ValueError("Cannot identify the module's Contextopus root")
    argv = [sys.executable, str(package / "tui_launch.py"), "--root", str(directory.parent.parent)]
    if context is not None:
        argv += ["--context", context]
    return {"argv": argv}
