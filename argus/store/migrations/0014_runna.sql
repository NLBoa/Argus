-- 0014: Runna runs as assignments (D078). A run's source record carries Runna's length for the
-- workout and a good time to start; its length becomes an estimate with source `runna`.
ALTER TABLE item_sources ADD COLUMN duration_minutes INTEGER;
ALTER TABLE item_sources ADD COLUMN preferred_start TEXT;

-- estimates gains source 'runna'. A CHECK can't be altered, so the table is rebuilt in place as
-- 0009 rebuilt time_logs. Nothing references estimates, and UNIQUE (item_id, version) is its only
-- index.
CREATE TABLE estimates_new (
  id             INTEGER PRIMARY KEY,
  item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  version        INTEGER NOT NULL,
  source         TEXT NOT NULL CHECK (source IN ('ai', 'default', 'user', 'runna')),
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
INSERT INTO estimates_new (id, item_id, version, source, p50_hours, p80_hours, confidence,
                           breakdown, assumptions, reasoning, input_hash, model, prompt_version,
                           created_at, retracted_at)
  SELECT id, item_id, version, source, p50_hours, p80_hours, confidence, breakdown, assumptions,
         reasoning, input_hash, model, prompt_version, created_at, retracted_at FROM estimates;
DROP TABLE estimates;
ALTER TABLE estimates_new RENAME TO estimates;
