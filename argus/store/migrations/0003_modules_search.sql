-- 0003: Canvas modules, current grade, and the full-text search index.

CREATE TABLE modules (
  id         INTEGER PRIMARY KEY,
  course_id  INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  canvas_id  INTEGER NOT NULL,
  name       TEXT NOT NULL,
  position   INTEGER NOT NULL,
  state      TEXT,
  unlock_at  TEXT,
  items      TEXT NOT NULL DEFAULT '[]',           -- JSON [{title, type, url, indent}]
  digest     TEXT,                                 -- AI study digest (Phase 5), NULL until written
  updated_at TEXT NOT NULL,
  UNIQUE (course_id, canvas_id)
);

ALTER TABLE courses ADD COLUMN grade_score REAL;
ALTER TABLE courses ADD COLUMN grade_letter TEXT;
ALTER TABLE courses ADD COLUMN grade_updated_at TEXT;

-- Rebuilt after every sync cycle (repo.rebuild_search_index). kind: item | module | post | concept | digest.
CREATE VIRTUAL TABLE search_index USING fts5(
  title, body,
  kind UNINDEXED, ref_id UNINDEXED, course UNINDEXED, url UNINDEXED,
  tokenize = 'porter unicode61 remove_diacritics 2'
);
