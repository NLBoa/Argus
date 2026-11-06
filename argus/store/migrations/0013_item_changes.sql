-- 0013: the item change log, for "what changed since T?" (the Claude connector, D073).
-- apply_merge appends a row when it creates an item, moves its due date, removes it, or brings it back.

CREATE TABLE item_changes (
  id       INTEGER PRIMARY KEY,
  item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  change   TEXT NOT NULL CHECK (change IN ('new', 'due', 'removed', 'restored')),
  old_due  TEXT,                                   -- due: the date before; removed: the date when removed
  new_due  TEXT,                                   -- new / due / restored: the date after
  at       TEXT NOT NULL
);
CREATE INDEX item_changes_at ON item_changes (at);
CREATE INDEX item_changes_item ON item_changes (item_id);

-- Existing items: "new" when created (with today's date, the best known), "removed" when removed.
INSERT INTO item_changes (item_id, change, new_due, at)
  SELECT id, 'new', due_at, created_at FROM items;
INSERT INTO item_changes (item_id, change, old_due, new_due, at)
  SELECT id, 'removed', due_at, due_at, removed_at FROM items WHERE removed_at IS NOT NULL;
