-- Keep legacy transcript tables intact; the card catalog is a separate projection.
CREATE TABLE "{{module}}::cards"(id TEXT PRIMARY KEY, document TEXT NOT NULL, indexed_revision TEXT, index_error TEXT);
