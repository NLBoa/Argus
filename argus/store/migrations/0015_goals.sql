-- 0015: the goals page (D079). Goal and week files read from the vault (Goals/), and goal
-- sessions read from FlowSavvy. Argus never writes the files; this keeps the last valid parse.

CREATE TABLE goal_docs (
  path          TEXT PRIMARY KEY,                -- vault-relative, e.g. Goals/Projects.md
  kind          TEXT NOT NULL CHECK (kind IN ('goal', 'week')),
  doc_key       TEXT,                            -- the tag or week key of the last valid parse
  text_hash     TEXT NOT NULL,                   -- sha256 of the file as last read
  good_json     TEXT,                            -- the last valid parse (JSON); NULL if never valid
  good_at       TEXT,                            -- when that version was read
  error         TEXT,                            -- the file's current problem; NULL when valid
  error_line    INTEGER,
  error_path    TEXT,
  read_at       TEXT NOT NULL,
  missing_since TEXT                             -- the file left the vault; cleared if it returns
);

CREATE TABLE goal_sessions (
  external_id TEXT PRIMARY KEY,                  -- FlowSavvy itemId, or itemId@occurrenceDate
  tag         TEXT NOT NULL,
  what        TEXT NOT NULL,
  start_at    TEXT NOT NULL,
  end_at      TEXT NOT NULL,
  minutes     INTEGER NOT NULL CHECK (minutes >= 0),
  completed   INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
  done_when   TEXT,                              -- the "Done when:" line of the task's notes
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  gone_at     TEXT,                              -- FlowSavvy confirmed deleted or cancelled, not done
  moved_at    TEXT,                              -- still exists, but left the fetch window not done
  raw_hash    TEXT NOT NULL
);
CREATE INDEX goal_sessions_start ON goal_sessions (start_at);
