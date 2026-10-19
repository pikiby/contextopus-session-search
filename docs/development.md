# Development and verification

Session Search owns its source, module manifest and migrations, tests and build
scripts. It uses Core through declared APIs. Core and Workspace remain separate
repositories; this module does not require a sibling checkout or Workspace.

## Source layout

| Path | Responsibility |
| --- | --- |
| `src/session_search/module_service.py`, `module_store.py` | Persistent child service, Core SQL and shared embeddings/RAG adapters. |
| `module/module.json`, `module/migrations/`, `module/package/` | Module contract, SQL schema and child/TUI entry points. |
| `src/session_search/reader.py`, `indexer.py`, `store.py` | Conversation blocks, deduplication, checkpoints and durable jobs. Shared by both delivery paths. |
| `src/session_search/session_management.py`, `session_types.py` | Metadata inventory, source classification and exact native-operation plans. |
| `src/session_search/tui.py`, `tui_core_backend.py` | Core-aware terminal interface. |
| `src/session_search/embedding.py`, `server.py`, `tui_backend.py` | Standalone model/MCP and retained daemon-backend compatibility code. |
| `src/session_search/core_module.py`, `migration.py` | Earlier in-process integration compatibility; excluded from the folder artifact. |
| `scripts/build-module.py` | Primary module folder/ZIP builder. |
| `scripts/build-deb.sh`, `packaging/`, `requirements-*.txt` | Secondary standalone package and environment requirements. |
| `tests/` | Shared logic, standalone compatibility, module lifecycle and packaging coverage. |

The Python wheel entry point is retained for earlier integration compatibility.
Install the folder artifact for the current persistent module architecture.
The earlier in-process adapter retains full catalog recovery through bounded,
snapshot-based Core revision pages; it no longer calls the unbounded revision
lookup. The primary folder service instead recovers through durable jobs/outbox
and does not perform a full startup inventory.

## Environment and tests

Use an existing Python 3.12+ environment with pytest, Textual and the Core 1.5+
package (including its retrieval dependencies). This repository
does not create environments or install dependencies automatically. The
standalone indexer has separate pinned requirements; see `standalone.md`.

For an installed matching Core development package:

```bash
PYTHONPATH=src /path/to/existing/python -m pytest -q tests
```

To test against an explicitly selected Core source checkout instead:

```bash
PYTHONPATH=src:/path/to/contextopus-core/src /path/to/existing/python -m pytest -q tests
```

The Core source path is an explicit integration-test input, not a fixed relative
path embedded in this repository. Folder-service tests use a temporary root,
Core SQL/RAG and real child service with fake embeddings. They cover startup,
selection, deduplication, search/history, restart recovery and lifecycle behavior.
Other tests retain standalone MCP/indexer, native session-management and `.deb`
coverage. Tests do not index the user's working transcripts or download a model.

## Build artifacts

```bash
python3 scripts/build-module.py /tmp/session-search-build/session_search
python3 scripts/build-module.py --zip /tmp/session-search-build/session-search-0.8.0.zip
./scripts/build-deb.sh /tmp/session-search-build/session-search_0.8.0_all.deb
```

The module builder uses a deliberate source allowlist and copies SQL migrations,
wrappers and its self-contained guide. The optional ZIP has one top-level
`session_search/` folder. Both modes refuse existing output. The `.deb` builder
requires existing `dpkg-deb`; it packages standalone compatibility separately.
Builders do not install or activate either product path.

The manifest currently declares module version `0.8.0`, Core `>=1.5,<2` and SQL
schema version `1`. Passing fixture checks establishes source behavior, not
real-model performance on a particular machine.

## Language and CI

Product documentation, code comments and shipped messages are English. Russian
strings and emoji in selected tests are intentional Unicode/UTF-8 fixtures for
search, token prompts, streaming blocks and JSONL offsets; changing them would
change the cases being tested.

CI explicitly checks out Core `v1.5.0`, installs its test dependencies plus
`sqlite-vec==0.1.9`, runs the complete Linux suite and builds module/standalone
assets. Separate Windows and macOS jobs exercise the portable folder module;
legacy Unix-daemon coverage remains in the Linux job. These tests use temporary
roots, synthetic conversations and fake embeddings, with no model download.
The manual `test_live_indexer.py` script is a separate real-model experiment and
is not run by normal pytest discovery.

Tagging and publication are handled by the release coordinator. CI uploads build
artifacts; it does not publish releases or alter an installed data root.
