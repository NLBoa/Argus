-- 0009: work windows and a ranked queue replace planned sessions (D057-D062).

-- When the student works. Seeded once from config.yaml; after that Settings owns them (D057).
CREATE TABLE work_windows (
  id         INTEGER PRIMARY KEY,
  day        INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6),  -- the day it starts, Mon=0
  start_time TEXT NOT NULL,                                 -- HH:MM local
  end_time   TEXT NOT NULL,                                 -- at or before start: past midnight
  created_at TEXT NOT NULL,
  CHECK (start_time <> end_time)
);

-- The student's say over the queue (D058): pins (kept until done, or "move" pins that expire with
-- their window) and "Not tonight" (skip one window).
CREATE TABLE queue_prefs (
  entry       TEXT PRIMARY KEY,                                  -- item:<id> | study:<exam item id>
  item_id     INTEGER REFERENCES items(id) ON DELETE CASCADE,
  pin_rank    INTEGER,
  pin_kind    TEXT NOT NULL DEFAULT 'pin' CHECK (pin_kind IN ('pin', 'move')),
  pin_until   TEXT,
  skip_window TEXT,
  updated_at  TEXT NOT NULL
);

-- "About 2 h left": the student's own remaining-time estimate, append-only (D060).
CREATE TABLE remaining_marks (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  hours      REAL NOT NULL CHECK (hours >= 0),
  at         TEXT NOT NULL,
  source     TEXT NOT NULL CHECK (source IN ('web', 'sms')),
  created_at TEXT NOT NULL
);
CREATE INDEX remaining_marks_item ON remaining_marks (item_id, at);

-- How each window's time is split, kept from just before it starts until it ends (D061).
CREATE TABLE window_plans (
  window_id  TEXT PRIMARY KEY,                 -- local start, e.g. 2026-10-19T19:00
  starts_at  TEXT NOT NULL,
  ends_at    TEXT NOT NULL,
  allocation TEXT NOT NULL DEFAULT '[]',       -- JSON [[entry, hours], ...] in queue order
  updated_at TEXT NOT NULL
);

-- time_logs gains `checkin` (answers to "did you work on it?") and `legacy` (credit carried over
-- from planned sessions under D030), plus the window an answer is about. Nothing references
-- time_logs, so it is rebuilt in place; both indexes are recreated.
CREATE TABLE time_logs_new (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id),
  started_at TEXT NOT NULL,
  ended_at   TEXT,                                      -- NULL while the timer runs
  source     TEXT NOT NULL CHECK (source IN ('timer', 'sms', 'manual', 'checkin', 'legacy')),
  note       TEXT,
  window_id  TEXT,                                      -- checkin: the window it answers for
  created_at TEXT NOT NULL,
  CHECK (ended_at IS NULL OR ended_at >= started_at)
);
INSERT INTO time_logs_new (id, item_id, started_at, ended_at, source, note, created_at)
  SELECT id, item_id, started_at, ended_at, source, note, created_at FROM time_logs;
DROP TABLE time_logs;
ALTER TABLE time_logs_new RENAME TO time_logs;
CREATE INDEX time_logs_item ON time_logs (item_id);
CREATE UNIQUE INDEX time_logs_one_running ON time_logs ((1)) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX time_logs_checkin ON time_logs (item_id, window_id) WHERE source = 'checkin';

-- Planned sessions are gone. Their passed work blocks are converted to `legacy` time once, by the
-- worker (it needs the clock); 0010 drops the table after that has run (D060).
ALTER TABLE plan_blocks RENAME TO plan_blocks_legacy;
ALTER TABLE plan_state ADD COLUMN model_since TEXT;
ALTER TABLE plan_state ADD COLUMN legacy_converted_at TEXT;
ALTER TABLE plan_state ADD COLUMN windows_seeded_at TEXT;

-- Start nudges are replaced by the window message (D061): never retry an unsent old one
-- (expired at creation, so the retry loop skips it).
UPDATE notifications
   SET status = 'failed', error = 'superseded by the work-window message (D061)', expires_at = created_at
 WHERE rule = 'start_nudge' AND status IN ('pending', 'failed');
