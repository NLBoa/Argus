-- 0011: choices the student makes in Settings that config.yaml only gives defaults for.
-- Keys: window_notice_minutes (how long before a work window its text comes).
CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
