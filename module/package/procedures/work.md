---
operations:
  - session_search.save
  - session_search.read
  - session_search.list
  - session_search.search
  - session_search.reconcile
  - session_search.status
---
# Maintain a session card

Use this procedure on an explicit skill invocation or when the current host/user instructions require it. No message counter, timer, automatic Workspace finish hook or transcript watcher exists. Do not change host instructions to impose this procedure without a request.

1. Establish the actual source/client and native session ID from trusted host information. Never invent an ID, URL, transcript path or session title. When identity is unavailable, ask for it before saving. Locate an existing card by source and session_id (list metadata, or read a known card); one source/session identity has one card.
2. Read the existing card before an update. Use the conversation already available to describe what can be found in the session: significant topics, decisions, results, unresolved questions and the current state of work. Preserve still-useful earlier topics. Distinguish rejected proposals and intentions from accepted decisions and verified results. Do not copy the transcript or secrets. Do not claim to cover unavailable earlier messages.
3. Save through session_search.save with a meaningful title, short description, source, session_id and Markdown content. Keep native coordinates, tags, project and Obsidian links in metadata. Existing cards require their current revision. Identity cannot be changed by an update. If available, covered_through identifies the last message considered; omit it rather than inventing a marker.
4. Check the save/index result. A saved file with pending indexing remains saved; fix the reported cause and retry session_search.reconcile. Never rewrite the source conversation.

Ordinary links (including contexts) describe relationships, not inherited instructions. required_links are explicit reading requirements; follow them through the owning module or authorized reader before dependent work. Do not infer required links from past use of a context. A session description is a search aid and historical evidence, not a replacement for current project rules or knowledge.

Cards live in modules/session_search/data/sessions/ as Markdown with YAML. Core owns file revisions, SQLite projections, embeddings and semantic selection. External file edits/moves/deletions are applied through session_search.reconcile. Queries do not silently generate summaries or modify records. Legacy transcript tables remain preserved but are not searched by these operations.
