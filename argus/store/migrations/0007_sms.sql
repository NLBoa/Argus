-- 0007: SMS replies and snoozes.

CREATE TABLE inbound_messages (
  id           INTEGER PRIMARY KEY,
  message_sid  TEXT NOT NULL UNIQUE,                  -- Twilio's id: a replayed webhook is answered once
  body         TEXT NOT NULL,
  command      TEXT,                                  -- JSON: what Argus understood, or NULL
  via          TEXT NOT NULL CHECK (via IN ('rules', 'ai', 'none')),
  status       TEXT NOT NULL CHECK (status IN ('done', 'unclear', 'failed')),
  reply        TEXT NOT NULL,
  item_ids     TEXT NOT NULL DEFAULT '[]',            -- JSON: what [n] means in the reply (TODAY lists items)
  created_at   TEXT NOT NULL
);

CREATE TABLE snoozes (
  item_id     INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  until       TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
