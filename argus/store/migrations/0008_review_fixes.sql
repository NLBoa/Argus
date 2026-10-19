-- 0008: fixes from the Phase 7 review.
-- When an official source later changes that date, the override is dropped: official wins again.
ALTER TABLE item_overrides ADD COLUMN basis TEXT;  -- official due date at confirmation ('none' if undated); NULL until the next merge fills it

-- Review R4: failed sends retry with backoff until they expire, so the last attempt time matters.
ALTER TABLE notifications ADD COLUMN last_attempt_at TEXT;
