-- 0012: an item the AI was asked to estimate but gave no estimate for (D069): the description
-- doesn't cover the whole task (`incomplete`, never asked again for the same input), or the call
-- failed (`failed`, retried a day later). One row per item, replaced on each attempt.
CREATE TABLE estimate_misses (
  item_id        INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  input_hash     TEXT NOT NULL,
  outcome        TEXT NOT NULL CHECK (outcome IN ('incomplete', 'failed')),
  reason         TEXT NOT NULL,
  prompt_version TEXT,
  created_at     TEXT NOT NULL
);
