# Contextopus Session Search

**Find past work by meaning, without indexing entire conversations.** Agents maintain searchable session cards: topics, decisions, results and where work stopped. Each card links back to its original session and related Contextopus records.

## Install with Core

Requires **Core >=1.7,<2**. Download the [0.9.0 module](https://github.com/pikiby/contextopus-session-search/releases/download/v0.9.0/session-search-0.9.0.zip), verify its checksum and install it:

```console
contextopus --root /path/to/contextopus-data modules install session_search --source /path/to/session-search-0.9.0.zip
```

For an existing installation use Core's module update preview/apply workflow. Do not replace its data directory.

## Use it

Apply your coding assistant's normal Contextopus integration to deliver the module skills.

- `$sessions`: create or update the current session's description.
- `$sessions-search`: search session cards by meaning and metadata.
- `$sessions-edit`: edit or explicitly delete a card.

Skill syntax depends on the client. You can also ask an agent to use the named skill. A user may put update requirements in Workspace rules, AGENTS.md, CLAUDE.md or another host instruction file. There is no timer, message counter, watcher or automatic `finish` integration.

## Files and search

Cards are ordinary Markdown with YAML under `modules/session_search/data/sessions/`. YAML carries stable card and native session IDs, source/client, title, description, source URI/path, project, tags, context links and optional explicit required links. The Markdown describes significant topics, results and outstanding work. Context and session links use Obsidian syntax and do not automatically inherit instructions.

Core indexes the description body and uses metadata filters before semantic selection. Session Search uses the same RAG and module storage lifecycle as other modules. It has no private embedding model. Read cards in the Core web library:

```console
contextopus --root /path/to/contextopus-data web
```

The library shows session descriptions, source/client, native ID, metadata filters and connections. It does not display an imported copy of the conversation.

## Editing and preservation

Agent saves use revision checks. External Markdown edits, moves and deletions are applied with `session_search.reconcile`. Failed indexing leaves the saved file intact and reports pending work; reconciliation retries it. Missing or invalid source directories fail closed.

Updating from the transcript-indexing module preserves existing SQL tables, configuration and original session files. Legacy transcript blocks are not exposed by the new search; their derived vectors are retired during reconciliation. No summaries are invented from legacy rows. Existing sessions need agent-authored cards to appear here.

The module has no TUI and does not archive, resume or delete original conversations. Source files, deep links and native IDs are references; opening them depends on the originating client.

## Client delivery

Core 1.7 client packages share one installer and one persistent data root.
Skills use full canonical names under the `contextopus` plugin namespace, or a
`contextopus-` prefix for native file delivery. Short aliases are user-selected.
See the [client package guide](https://github.com/pikiby/contextopus-core/blob/main/docs/client-packages.md)
for package formats, setup and verification limits.

## Current release

[0.9.0: Searchable session cards](https://github.com/pikiby/contextopus-session-search/releases/tag/v0.9.0) · [Downloads](https://github.com/pikiby/contextopus-session-search/releases/tag/v0.9.0)
