# Standalone compatibility

The standalone `session-search` command remains a supported secondary path for
existing users. It has a stdio MCP frontend, one background indexer over a Unix
socket, its own SQLite/sqlite-vec index and pinned offline embedding model. The
folder module uses Core instead and has a separate artifact and lifecycle.
The current TUI requires Core; `session-search tui` opens the Core-aware client
and does not control this daemon.

## Build and install

Requires Python 3.12+ and existing `dpkg-deb` for packaging:

```bash
./scripts/build-deb.sh
sudo dpkg -i dist/session-search_0.8.0_all.deb
```

The package installs Python code in `/usr/lib/session-search/session_search`,
`/usr/bin/session-search`, documentation and the systemd user unit
`/usr/lib/systemd/user/codex-session-index.service`. It has no maintainer hooks;
installation does not create an environment, download weights, migrate an index
or start/restart a service.

The launcher uses an existing interpreter at
`~/.local/share/codex-session-index/venv/bin/python`, overridden with the absolute
`SESSION_SEARCH_PYTHON` path. Indexer dependencies are pinned in
`requirements-indexer.txt`, including CPU PyTorch `2.6.0+cpu`. TUI requirements are
in `requirements-tui.txt`. Obtaining those dependencies is a separate environment
setup step; neither builder nor launcher runs pip.

The offline model is `Qwen/Qwen3-Embedding-0.6B`, fixed revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, from the standard Hugging Face cache:

```text
~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
```

No weights are shipped. `--model-path` only accepts that pinned snapshot in the
standard cache. The indexer disables network downloads and remote model code.

## Run and connect

For a fresh installation with no overriding user unit:

```bash
systemctl --user daemon-reload
systemctl --user enable --now codex-session-index.service
session-search status
```

Inspect `systemctl --user cat codex-session-index.service` before updating an
existing installation: a unit in `~/.config/systemd/user` overrides the packaged
unit. Back up an existing override outside the systemd directory before replacing
it. Retain custom drop-ins or compare them explicitly. An interpreter override
belongs in an `Environment=SESSION_SEARCH_PYTHON=/absolute/path` user drop-in.
The shipped unit uses `MemoryHigh=4G`, `MemoryMax=5G`, `MemorySwapMax=0`, no CPU quota
and `UMask=0077`; it does not configure startup before login.

The default database is `~/.local/share/codex-session-index/index.sqlite3`.
The default socket is `$XDG_RUNTIME_DIR/codex-session-index.sock`, falling back to
`~/.cache/codex-session-index/index.sock`; `CODEX_SESSION_INDEX_SOCKET` overrides
it. Do not run two indexers against one database. A manual indexer can be started
with `session-search index` and stopped using Ctrl+C in its terminal.

Example Codex MCP configuration:

```toml
[mcp_servers.session_search]
command = "/usr/bin/session-search"
args = ["mcp"]
```

Set `SESSION_SEARCH_PYTHON` or `CODEX_SESSION_INDEX_SOCKET` in that server's env
section when needed. Restart the client's MCP process after changing its config.
Each client runs a lightweight stdio process; all share one indexer and model.

| MCP tool | Purpose |
| --- | --- |
| `session_search` | `query`, `mode=semantic\|literal`, `state=active\|archived\|all`, `limit` 1–200, optional snippets. |
| `session_info` | Inspect a session by ID, name, filename or path. |
| `session_resume_command` | Return the command to open an active session. |
| `index_status` | Read queue and indexer availability. |
| `session_list` | Metadata filters with `offset` and `limit` 1–1000. |
| `session_cleanup_preview` | Exact archive/unarchive/delete selection and plan. |
| `session_cleanup_apply` | Apply unchanged `plan` with `confirmation=plan_id` after human approval. |

`codex_home` defaults to `CODEX_HOME` or `~/.codex`. Semantic limits count unique
blocks; returned source occurrences can exceed that number. Literal limits count
sessions. Search state intersects the stored active/archived source selection.
Metadata listing/preview is independent of embedding selection and works without
a model or running indexer. Apply requires the serialized indexer actor.

The plan approval, fingerprint, native-cascade and partial-failure rules described
in the module guide also apply to standalone management. It does not merge sessions.
Native command checks used Codex CLI `0.162.1`; other versions need verification.

## Index behavior and settings

Consecutive user/assistant messages form blocks, default 512 tokens. A trailing
partial block is searchable and replaced as text grows. Checkpoints retain that
partial block and source byte/character coordinates. Identical text shares one
embedding with all occurrences retained. Deleting the last occurrence removes
the block, vector and job. Newer sessions and later blocks have queue priority;
already running embedding calls finish before a new selection takes effect.

The default vector has 1024 normalized coordinates. Explicit
`--dimensions 256|512|1024` and `--block-tokens 512|1024|2048` select other profiles.
Reduced dimensions save storage and comparison work, but still compute the full
model representation. The bounded standalone comparison is documented separately;
it does not establish an optimum for an entire history or for the Core module.

Selection is saved in `~/.config/session-search/sessions.json` (respecting
`XDG_CONFIG_HOME`). Active sessions are selected by default; archived sessions are excluded.
The module additionally defaults to main-session selection; standalone selection
only restricts types when a type filter is configured. Disabled sources retain vectors and jobs. The standalone
`--cores N` CLI and CPU settings file remain available, but no TUI allocation
control is exposed. The indexer selects one logical CPU per physical core from
Linux sysfs. Explicit `--cores` takes precedence over the saved CPU setting.

A profile/schema mismatch requires an explicit derived-index rebuild. Stop the
service first, then run:

```bash
systemctl --user stop codex-session-index.service
session-search index --rebuild-index
```

This remains a running indexer; stop it with Ctrl+C before starting the service
again. On a profile mismatch it resets blocks/chunks/jobs/vectors and read
positions, retaining JSONL, session inventory and search history. A matching
profile is not reset. Package installation never performs this operation.

Search history records request status, parameters, duration and results. Metrics
cover the last 24 hours. Only the indexer writes history; a failed history write
is reported separately and does not replace a successful search response. Known
secret formats are masked, but arbitrary secrets cannot be recognized reliably.
History is not embedded and remains after a session is deleted.

## Verify, update and remove

```bash
session-search status
systemctl --user status codex-session-index.service
journalctl --user -u codex-session-index.service -n 30 --no-pager
```

`index_pending=true` means processing is incomplete. A running service or a built
`.deb` alone does not prove a complete index or a successful installation.

For an update, install the new `.deb`, compare any user unit with the packaged
version, then explicitly restart the indexer and MCP clients. Running processes
are not replaced by package installation. Removing the package requires stopping
and disabling the user service before `sudo dpkg -r session-search`. External
environments, model weights, user units, SQLite and source sessions remain.
The database contains conversation text and belongs outside source control.
