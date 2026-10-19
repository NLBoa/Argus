-- 0002: notifications (idempotent sends), time logs (timer + manual), rule mutes.

CREATE TABLE notifications (
  id                INTEGER PRIMARY KEY,
  key               TEXT NOT NULL UNIQUE,               -- first of its idempotency keys
  rule              TEXT NOT NULL,
  channel           TEXT,                               -- sms | ntfy (set when sent)
  status            TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
  title             TEXT NOT NULL,
  body              TEXT NOT NULL,
  item_ids          TEXT NOT NULL DEFAULT '[]',         -- JSON: message item numbers -> item ids
  priority          INTEGER NOT NULL DEFAULT 3,         -- 1 (min) .. 5 (urgent)
  counts_toward_cap INTEGER NOT NULL DEFAULT 1,
  attempts          INTEGER NOT NULL DEFAULT 0,
  error             TEXT,
  expires_at        TEXT,                               -- retries stop after this
  created_at        TEXT NOT NULL,
  sent_at           TEXT
);
CREATE INDEX notifications_sent ON notifications (sent_at);

-- Every idempotency key a notification covers (a batched message covers several items).
-- Keys look like "<item_id>:<rule>:<due_at>" so a moved due date re-arms the reminder.
CREATE TABLE notification_keys (
  key             TEXT PRIMARY KEY,
  notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE
);

CREATE TABLE time_logs (
  id         INTEGER PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id),
  started_at TEXT NOT NULL,
  ended_at   TEXT,                                      -- NULL while the timer runs
  source     TEXT NOT NULL CHECK (source IN ('timer', 'sms', 'manual')),
  note       TEXT,
  created_at TEXT NOT NULL,
  CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX time_logs_item ON time_logs (item_id);
-- At most one running timer.
CREATE UNIQUE INDEX time_logs_one_running ON time_logs ((1)) WHERE ended_at IS NULL;

CREATE TABLE rule_mutes (
  rule        TEXT PRIMARY KEY,
  muted_until TEXT,                                     -- NULL = until unmuted
  created_at  TEXT NOT NULL
);
