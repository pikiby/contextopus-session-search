# Session card format

```yaml
---
id: stable-card-id
type: session
title: Database coordination discussion
description: Decisions about coordinating concurrent database changes.
source: claude-desktop
session_id: native-session-id
source_uri: /absolute/path/to/original.jsonl
project: example-project
status: active
tags: [database, coordination]
contexts:
  - "[[modules/workspace/workspace_contexts/example-project/Context]]"
links: []
required_links: []
---
```

The Markdown body describes what can be found in the session, accepted/rejected decisions, verified results and outstanding work. `updated_at` is maintained on saves. Optional `covered_through` records a known native message ID, never a guessed count. Source names are open vocabulary, including terminal, editor and desktop variants. The unique source/session_id pair prevents duplicate cards; stable card IDs survive file moves.

Metadata remains separate from embedding input. `source`, `project`, `context`, `tag` and `status` filters restrict candidate identities before Core's shared relevance algorithm. Tags are labels; contexts and links are wikilinks. Explicit required_links must be read before dependent work and never arise automatically from ordinary links.
