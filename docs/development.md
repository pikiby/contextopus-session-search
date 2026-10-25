# Development and verification

Session Search 0.9 uses session cards. The folder module is the supported build;
it uses Core document reconciliation, module SQL storage and shared RAG.

## Source layout

- `src/session_search/cards.py`: Markdown authority, validation, revision checks,
  SQL projection, reconciliation and semantic retrieval.
- `src/session_search/card_service.py`: request-driven Core child service.
- `module/module.json`: operations, storage, skills and Library contribution.
- `module/package/procedures/` and `skills/`: agent-maintained session descriptions.
- `module/migrations/002-session-cards.sql`: card projection; the original schema
  remains available so upgrades preserve existing data.
- `scripts/build-module.py`: explicit allowlist for the folder and ZIP artifact.

Older transcript-indexing sources and standalone packaging remain in the source
checkout for now. They are not included in the new folder module and are not its
supported runtime. Historical tests of the old manifest and operation set do not
validate the new card interface. Do not start an old indexer against an upgraded
root. The TUI has been removed.

## Local verification

Use Python 3.12+, pytest and Core >=1.7,<2, which includes the shared RAG
`object_ids` filter capability.
The module checks it when starting and reports that Core must be updated.

```console
PYTHONPATH=src:/path/to/contextopus-core/src python -m pytest -q tests/test_session_cards.py tests/test_installers.py
python scripts/build-module.py --zip /tmp/session-search-0.9.0.zip
```

Card tests install the actual folder module into a temporary Core root and use
real child RPC, SQL, file reconciliation and vector retrieval with deterministic
test embeddings. They cover identity, filters before result selection, external
edits/moves/deletions, revisions, failed embedding retry and legacy-data
preservation. They neither read user transcripts nor download a model. Passing
these checks does not measure real-model semantic quality.

Core's web tests cover HTTP input validation and the module-driven filter UI.
A source build does not update the working installation or restart services.

## CI and release

CI runs the card and installer suites on Linux, Windows and macOS. It checks out
Core `main` by default; a manual run can select an explicit matching Core ref.
The local Core capability must be published before remote CI can pass. Pin a
coordinated Core release when publishing this module. Linux builds the folder
ZIP; legacy standalone packages are not release assets of the card module.

Product documentation, skills and messages are English. Unicode fixture data is
intentional. Builds do not publish releases or change installed roots.
