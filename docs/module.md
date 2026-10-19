# Session Search module guide

Session Search is a persistent Contextopus folder module, ID `session_search`,
version `0.8.0`. Core owns the process lifecycle, shared embeddings, RAG and module
SQL. Workspace is optional. The module does not run the standalone stdio MCP,
Unix socket, private model or systemd unit.

## Compatibility

Requires Core `>=1.5,<2` and Python 3.12+. Core supplies persistent module
services, the child SDK, per-module SQLite, token counting and shared retrieval.
Session Search is versioned and distributed independently of Core.

The child service has no separately declared Python dependencies. Core supplies
its SDK and model resources. The TUI additionally needs Textual and Core's Python
client in the interpreter that launches it.

## Build and installation

From the source checkout, build a fresh folder or ZIP:

```bash
python3 scripts/build-module.py /tmp/session-search-build/session_search
python3 scripts/build-module.py --zip /tmp/session-search-build/session-search-0.8.0.zip
```

The ZIP contains `session_search/module.json`, `session_search/package/` and this
guide as `session_search/README.md`. The builder never replaces existing output,
installs packages or starts a service. It includes code and SQL migrations, with
no user data, model weights, caches or standalone service configuration.

For a new install, use Core's common folder/ZIP source installation:

```bash
contextopus --root /path/to/existing/root modules install session_search --source /path/to/session-search-0.8.0.zip
```

A built `session_search/` folder is also accepted as `--source`. Add `--plan` for
read-only validation. The included `install.sh` and `install.ps1` delegate to
this same command. Omit the root flag when using Core's bound root.

Core previews the source, prepares code, applies declared migrations and enables
the contribution after readiness checks. An existing module target is refused;
use the update operation below for upgrades. A successful source-install exit
means the module is enabled, not that indexing has completed. Source installation
does not import a previous standalone index or change model settings.

## Configure and search through Core MCP

Use Core's existing shared MCP entry point. Queries use `read`, mutations use
`write`; keep the selected `work_root` and `context` in the outer call.
Replace the example paths with existing directories.

Configure the source without starting indexing:

```json
{
  "operation": "session_search.configure",
  "arguments": {
    "codex_home": "/path/to/existing/.codex",
    "block_tokens": 512,
    "autostart": false
  },
  "work_root": "/path/to/existing/root",
  "context": "selected-context"
}
```

Then call `write` with `operation="session_search.start"` and `arguments={}`.
Check `read` with `operation="session_search.status"`. New installations remain
unconfigured and do not index until configured and started; `autostart=true`
explicitly enables indexing when the service starts later.

Search with `read`:

```json
{
  "operation": "session_search.search",
  "arguments": {
    "query": "How was the backup restored?",
    "mode": "semantic",
    "state": "active",
    "limit": 20
  },
  "work_root": "/path/to/existing/root",
  "context": "selected-context"
}
```

`mode` is `semantic` or `literal`; `state` is `active`, `archived` or `all`.
Queries intersect the module's source selection; they cannot re-enable an
excluded category. Semantic `limit` counts unique blocks, so several source
occurrences can refer to the same match. Literal `limit` counts sessions.

| Query operations | Purpose |
| --- | --- |
| `session_search.status`, `session_search.settings` | Read runtime state, counters and configuration without enabling indexing. |
| `session_search.search`, `session_search.history` | Search conversations and inspect search history. |
| `session_search.info`, `session_search.resume_command` | Inspect a session and obtain its resume command. |
| `session_search.list` | Select sessions by metadata independently of embedding selection. |
| `session_search.cleanup_preview` | Build an exact archive, unarchive or delete plan. |
| `session_search.migration_preview` | Inspect an explicitly supplied old catalog without importing it. |
| `session_search.open_tui` | Obtain the Core-aware TUI launch descriptor. |

| Mutation operations | Purpose |
| --- | --- |
| `session_search.configure` | Set existing Codex home, block size and indexing autostart. |
| `session_search.selection` | Set `active`, `archived` and `types`. Defaults: active, main sessions only. |
| `session_search.start`, `session_search.stop` | Resume or pause indexing while keeping the service and counters accessible. |
| `session_search.cleanup_apply` | Execute the exact confirmed preview plan. |
| `session_search.migration_apply` | Explicitly import an eligible old catalog using its reviewed plan. |

Consult the discovered operation schemas for required arguments and limits.
Migration is optional and separate from installation; a fresh index requires no
old catalog. Stopping indexing retains completed vectors and pending jobs.

## Terminal interface

Open Session Search from Core's module UI. For manual development, use an
existing Core/Textual interpreter and explicit coordinates:

```bash
PYTHONPATH=src /path/to/core/python -m session_search.tui --root /path/to/existing/root --context selected-context
```

The TUI uses Core's `RemoteApplication`; the source command above runs from the
checkout. Without `--root`, it uses Core's normal read-only root selection and
does not create a missing root. It preserves embedding speed, progress, estimated
remaining time and search-history metrics. Arrow keys select controls, Enter
applies, `s` toggles indexing and `q` closes the interface. MCP is shown as
connected only after the folder module returns `integration="core"`; an old daemon-adapter response is rejected.

## Storage, recovery and updates

Core stores module tables in
`<root>/core/storage/modules/session_search.sqlite3`. The module owns its versioned
SQL migrations; Core coordinates access. Shared vectors belong to Core RAG;
source Codex JSONL stays outside the module artifact. Blocks preserve message
roles, turn IDs, JSONL byte offsets and character spans for inspection.

Identical blocks share embeddings while preserving every session occurrence.
The incomplete trailing block is indexed and replaced when text is appended.
Restart recovery requeues interrupted jobs and replays targeted vector-deletion
intents. Confirmed errors and retry backoff remain. Startup does not enumerate
all RAG revisions or perform a full SQL/RAG audit; no user-facing full-audit
operation is currently implemented.

Build the new module into a fresh folder and update through Core:

```bash
contextopus --root /path/to/existing/root modules update session_search --source /path/to/new/session_search
```

This maps to `core.module_update` with `module` and `source`. Core stages code,
checks storage compatibility and retains previous releases. Use this operation
instead of overwriting a running package. Disable through
`modules disable session_search` retains module data. Neither installation nor
update implicitly imports or deletes a standalone catalog or source transcript.

## Managing sessions

Metadata filters include state, exact IDs, working directory/subtree, exact JSONL
path, creation and last-message date bounds, byte size, session types and recorded
model names. All supplied conditions use AND; a models list matches any exact
recorded model. ISO 8601 date bounds are inclusive and default to UTC. Missing
dates do not match date filters. Filesystem mtime is not a last-message timestamp.

List and preview first. Show the complete selected IDs/paths, count, bytes and
blocked descendants, then obtain approval of that exact plan. Apply receives the
unchanged `plan` and its `plan_id` as `confirmation`; that token is a consistency
check and does not itself represent human approval.

Plans check fingerprints and reject changed files, symlinks and ambiguous IDs.
Native delete can cascade through spawn edges: unselected descendants block the
plan, and selected descendants are deleted before parents. `CODEX_THREAD_ID`
protects the calling session when supplied; it does not detect every open client.
Stop external writes to selected sessions before applying a plan.

Check `cleanup.ok`, `completed`, `count` and `error`. Failures can leave partial
results; inspect state before retrying. Archiving preserves embeddings; deleting
a session removes only its occurrences and unreferenced vectors. Search history
remains. Session merging is not implemented. Native command behavior was checked
with Codex CLI `0.162.1` in an isolated Codex home; other versions need verification.

## License

Apache-2.0; see the included `LICENSE` and retained attribution in `NOTICE`.
