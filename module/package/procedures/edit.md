---
operations:
  - session_search.read
  - session_search.save
  - session_search.delete
  - session_search.reconcile
  - session_search.status
---
# Edit session cards

Obtain session_search/work before authoring a description. Delete only on an explicit request, at the current file revision. Deleting a card never deletes its source conversation. External Markdown edits and deletions are reconciled through Core's common document plan. A failed or incomplete scan must not infer deletions. Reconciliation retires legacy block vectors, preserves legacy SQL/configuration and does not generate cards from old transcripts.
