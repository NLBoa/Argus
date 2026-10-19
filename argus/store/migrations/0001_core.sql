-- 0001: courses, meetings, items and their sources, posts, collector health.
-- Instants are ISO-8601 UTC text ("2026-10-23T03:59:00Z"), so text order == time order.
-- Recurring meeting times are local wall-clock "HH:MM" (America/New_York).

CREATE TABLE courses (
  id                INTEGER PRIMARY KEY,
  code              TEXT NOT NULL UNIQUE,
  section           TEXT NOT NULL,
  title             TEXT,
  instructors       TEXT NOT NULL DEFAULT '[]',          -- JSON array
  canvas_id         INTEGER,
  gradescope_id     TEXT,
  piazza_network_id TEXT,
  accent            TEXT,
  active            INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE TABLE meetings (
  id          INTEGER PRIMARY KEY,
  course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  source      TEXT NOT NULL CHECK (source IN ('umdio', 'manual')),
  kind        TEXT NOT NULL,                                -- lecture | discussion | lab
  days        TEXT NOT NULL,                                -- weekday numbers, Mon=0: "0,2,4"
  start_local TEXT NOT NULL,
  end_local   TEXT NOT NULL,
  building    TEXT,
  room        TEXT,
  updated_at  TEXT NOT NULL,
  CHECK (start_local < end_local)
);
CREATE INDEX meetings_course ON meetings (course_id);

CREATE TABLE items (
  id                   INTEGER PRIMARY KEY,
  course_id            INTEGER NOT NULL REFERENCES courses(id),
  kind                 TEXT NOT NULL CHECK (kind IN ('assignment', 'quiz', 'exam', 'event')),
  title                TEXT NOT NULL,
  due_at               TEXT,
  due_source           TEXT,
  release_at           TEXT,
  late_due_at          TEXT,
  points               REAL,
  submitted            INTEGER,                              -- NULL = unknown
  graded               INTEGER,
  description          TEXT,
  description_hash     TEXT,
  date_conflict        TEXT,                                 -- JSON [[source, iso], ...]
  conflict_resolved_at TEXT,
  status               TEXT NOT NULL DEFAULT 'upcoming'
                       CHECK (status IN ('upcoming', 'in_progress', 'submitted', 'graded', 'missed')),
  user_done_at         TEXT,
  started_at           TEXT,
  removed_at           TEXT,                                 -- every source record is gone
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE INDEX items_due ON items (due_at) WHERE removed_at IS NULL;
CREATE INDEX items_course ON items (course_id);

CREATE TABLE item_sources (
  id             INTEGER PRIMARY KEY,
  item_id        INTEGER REFERENCES items(id) ON DELETE SET NULL,
  source         TEXT NOT NULL,
  external_id    TEXT NOT NULL,
  course_id      INTEGER NOT NULL REFERENCES courses(id),
  title          TEXT NOT NULL,
  kind           TEXT NOT NULL,
  due_at         TEXT,
  release_at     TEXT,
  late_due_at    TEXT,
  all_day        INTEGER NOT NULL DEFAULT 0,
  points         REAL,
  submitted      INTEGER,
  graded         INTEGER,
  description    TEXT,
  question_count INTEGER,
  url            TEXT,
  raw_hash       TEXT NOT NULL,
  present        INTEGER NOT NULL DEFAULT 1,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  changed_at     TEXT NOT NULL,
  UNIQUE (source, external_id)
);
CREATE INDEX item_sources_item ON item_sources (item_id);

CREATE TABLE posts (
  id            INTEGER PRIMARY KEY,
  course_id     INTEGER NOT NULL REFERENCES courses(id),
  platform      TEXT NOT NULL CHECK (platform IN ('piazza', 'canvas')),
  external_id   TEXT NOT NULL,
  via           TEXT NOT NULL,
  kind          TEXT NOT NULL,
  author_role   TEXT NOT NULL,
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,
  url           TEXT,
  pinned        INTEGER NOT NULL DEFAULT 0,
  posted_at     TEXT NOT NULL,
  edited_at     TEXT,
  content_hash  TEXT NOT NULL,
  scanned_hash  TEXT,                                        -- content_hash at last extraction
  first_seen_at TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (platform, external_id)
);
CREATE INDEX posts_course ON posts (course_id, posted_at);

CREATE TABLE sync_runs (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('ok', 'unchanged', 'error', 'auth_error', 'skipped')),
  error       TEXT,
  records     INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX sync_runs_source ON sync_runs (source, started_at);

CREATE TABLE source_state (
  source        TEXT NOT NULL,
  key           TEXT NOT NULL,
  payload_hash  TEXT,
  etag          TEXT,
  last_modified TEXT,
  data          TEXT,                                        -- source-specific JSON (e.g. post versions)
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (source, key)
);
