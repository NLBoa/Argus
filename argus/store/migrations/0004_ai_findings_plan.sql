-- 0004: AI accounting and cache, findings, student overrides, estimates, plan.

CREATE TABLE ai_calls (
  id             INTEGER PRIMARY KEY,
  task           TEXT NOT NULL,                      -- extract | estimate | plan_note | ...
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  input_hash     TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('ok', 'invalid', 'refused', 'error', 'budget', 'cache_hit')),
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_read     INTEGER NOT NULL DEFAULT 0,
  cache_write    INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL NOT NULL DEFAULT 0,
  error          TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX ai_calls_created ON ai_calls (created_at);

CREATE TABLE ai_cache (
  key        TEXT PRIMARY KEY,                       -- sha256(model, prompt version, rendered input)
  task       TEXT NOT NULL,
  response   TEXT NOT NULL,                          -- validated JSON
  created_at TEXT NOT NULL
);

CREATE TABLE findings (
  id           INTEGER PRIMARY KEY,
  post_id      INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  course_id    INTEGER NOT NULL REFERENCES courses(id),
  kind         TEXT NOT NULL CHECK (kind IN ('new_assessment', 'scope', 'date_change', 'cancellation', 'logistics', 'other')),
  title        TEXT NOT NULL,
  date         TEXT,                                 -- UTC instant, NULL when not resolvable
  date_only    INTEGER NOT NULL DEFAULT 0,           -- the post gave a day but no time
  scope        TEXT,
  quote        TEXT NOT NULL,
  confidence   REAL NOT NULL,
  item_id      INTEGER REFERENCES items(id) ON DELETE SET NULL,
  status       TEXT NOT NULL CHECK (status IN ('applied', 'pending', 'conflict', 'linked', 'stored', 'confirmed', 'dismissed', 'superseded')),
  reason       TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  resolved_at  TEXT
);
CREATE INDEX findings_status ON findings (status);
CREATE INDEX findings_post ON findings (post_id);

CREATE TABLE item_overrides (
  item_id    INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  due_at     TEXT,                                   -- student-confirmed due time (wins over sources)
  cancelled  INTEGER NOT NULL DEFAULT 0,
  reason     TEXT NOT NULL,
  created_at TEXT NOT NULL
);

ALTER TABLE items ADD COLUMN scope TEXT;
ALTER TABLE items ADD COLUMN logistics TEXT;

CREATE TABLE estimates (
  id             INTEGER PRIMARY KEY,
  item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  version        INTEGER NOT NULL,
  source         TEXT NOT NULL CHECK (source IN ('ai', 'default', 'user')),
  p50_hours      REAL NOT NULL,
  p80_hours      REAL NOT NULL,
  confidence     TEXT,
  breakdown      TEXT NOT NULL DEFAULT '[]',         -- JSON [{step, hours}]
  assumptions    TEXT NOT NULL DEFAULT '[]',         -- JSON [str]
  reasoning      TEXT,
  input_hash     TEXT NOT NULL,
  model          TEXT,
  prompt_version TEXT,
  created_at     TEXT NOT NULL,
  retracted_at   TEXT,                               -- a user override the student removed
  UNIQUE (item_id, version),
  CHECK (p80_hours >= p50_hours AND p50_hours > 0)
);

CREATE TABLE plan_blocks (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER REFERENCES items(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN ('work', 'review')),
  start_at   TEXT NOT NULL,
  end_at     TEXT NOT NULL,
  label      TEXT,
  created_at TEXT NOT NULL,
  CHECK (end_at > start_at)
);
CREATE INDEX plan_blocks_start ON plan_blocks (start_at);

CREATE TABLE plan_state (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  inputs_hash TEXT NOT NULL,
  explanation TEXT NOT NULL,
  planned_at  TEXT NOT NULL
);
