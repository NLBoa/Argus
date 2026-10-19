-- 0005: study material, concepts and practice, exams, the vault's region ledger and proposals.

CREATE TABLE materials (
  id             INTEGER PRIMARY KEY,
  course_id      INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  module_id      INTEGER,                              -- Canvas module id, when known
  title          TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('pdf', 'pptx', 'page', 'syllabus', 'inbox', 'text')),
  url            TEXT NOT NULL,                        -- where the student opens it (never copied to the vault)
  pages          TEXT NOT NULL DEFAULT '[]',           -- JSON [{ref: "slide 3", text}]
  content_hash   TEXT NOT NULL,
  fetched_at     TEXT NOT NULL,
  UNIQUE (course_id, url)
);

CREATE TABLE concepts (
  id             INTEGER PRIMARY KEY,
  course_id      INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  module_id      INTEGER,
  slug           TEXT NOT NULL,
  name           TEXT NOT NULL,
  summary        TEXT NOT NULL DEFAULT '',
  related        TEXT NOT NULL DEFAULT '[]',           -- JSON [slug]
  sources        TEXT NOT NULL DEFAULT '[]',           -- JSON [{ref, title, url}]
  body           TEXT NOT NULL DEFAULT '{}',           -- JSON: the verified note sections
  priority       REAL NOT NULL DEFAULT 0,              -- raised by confusion inbox entries
  input_hash     TEXT,
  model          TEXT,
  prompt_version TEXT,
  generated_at   TEXT,
  verified_at    TEXT,
  UNIQUE (course_id, slug)
);

CREATE TABLE practice_items (
  id          INTEGER PRIMARY KEY,
  concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN ('question', 'flashcard')),
  prompt      TEXT NOT NULL,
  answer      TEXT NOT NULL,
  source_ref  TEXT NOT NULL,
  origin      TEXT NOT NULL DEFAULT 'generated' CHECK (origin IN ('generated', 'inbox')),
  verified    INTEGER NOT NULL DEFAULT 0,
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL,
  UNIQUE (concept_id, prompt)
);

CREATE TABLE practice_attempts (
  id               INTEGER PRIMARY KEY,
  practice_item_id INTEGER NOT NULL REFERENCES practice_items(id) ON DELETE CASCADE,
  result           TEXT NOT NULL CHECK (result IN ('correct', 'shaky', 'incorrect')),
  confidence       INTEGER CHECK (confidence BETWEEN 1 AND 3),
  created_at       TEXT NOT NULL
);
CREATE INDEX practice_attempts_item ON practice_attempts (practice_item_id, created_at);

CREATE TABLE exams (
  item_id     INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  scope       TEXT,
  logistics   TEXT,
  concept_ids TEXT NOT NULL DEFAULT '[]',              -- JSON [concept id]
  format      TEXT,                                   -- e.g. "8 short answers, 2 proofs"
  updated_at  TEXT NOT NULL
);

CREATE TABLE vault_regions (
  path       TEXT NOT NULL,                            -- relative to the vault root
  region_id  TEXT NOT NULL,                            -- 'frontmatter' for Argus-owned keys
  hash       TEXT NOT NULL,                            -- what Argus last wrote there
  commit_sha TEXT,
  written_at TEXT NOT NULL,
  PRIMARY KEY (path, region_id)
);

CREATE TABLE proposals (
  id          INTEGER PRIMARY KEY,
  path        TEXT NOT NULL,
  region_ids  TEXT NOT NULL,                           -- JSON
  reason      TEXT NOT NULL,
  base_hash   TEXT NOT NULL,                           -- the note this proposal applies to, exactly
  before_text TEXT NOT NULL,
  after_text  TEXT NOT NULL,
  diff        TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('open', 'applied', 'rejected', 'expired', 'stale', 'superseded')),
  created_at  TEXT NOT NULL,
  decided_at  TEXT,
  applied_at  TEXT
);
CREATE INDEX proposals_open ON proposals (status, path);

CREATE TABLE inbox_entries (
  id           INTEGER PRIMARY KEY,
  path         TEXT NOT NULL,
  text_hash    TEXT NOT NULL UNIQUE,
  text         TEXT NOT NULL,
  concept_id   INTEGER REFERENCES concepts(id) ON DELETE SET NULL,
  processed_at TEXT NOT NULL
);

CREATE TABLE vault_runs (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,
  finished_at   TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('ok', 'conflict', 'error', 'skipped')),
  error         TEXT,
  commit_sha    TEXT,
  files_written INTEGER NOT NULL DEFAULT 0
);
