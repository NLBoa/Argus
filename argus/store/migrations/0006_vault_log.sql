-- 0006: the log of edits Argus made in notes opened to it (`argus-edit: allow`), for the
-- weekly note, and when targeted practice was added for a confusion-inbox entry.

CREATE TABLE vault_edit_log (
  id         INTEGER PRIMARY KEY,
  path       TEXT NOT NULL,
  change     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX vault_edit_log_at ON vault_edit_log (created_at);

ALTER TABLE inbox_entries ADD COLUMN practice_at TEXT;
