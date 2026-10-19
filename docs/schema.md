# Schema

The database is SQLite in WAL mode with foreign keys on. Migrations are numbered SQL files in `argus/store/migrations/`
(`0001_…sql` and onward, with no gaps). `argus db migrate` and every process start apply them, and each applies atomically.
Applied versions are recorded in `schema_migrations`.

**Encoding.** Instants are ISO-8601 UTC text (`2026-10-23T03:59:00Z`, second precision), so string
order is time order. Recurring times (meetings) are local wall-clock `HH:MM` in America/New_York.
Booleans are `0`/`1`, and `NULL` means unknown where noted. JSON columns are noted.

| Table | Migration | Purpose |
|---|---|---|
| `courses` | 0001 | One row per configured class. Rows are upserted from `config.yaml` and deactivated (`active=0`) when removed |
| `meetings` | 0001 | Class meeting times (`days` = weekday numbers, Mon=0). Replaced per (course, source) |
| `items` | 0001 | Merged assignments, quizzes, exams, and events: the resolved fields plus derived `status` |
| `item_sources` | 0001 | One row per source record, `UNIQUE(source, external_id)`. Links to an item. `present=0` when a complete fetch no longer lists it |
| `posts` | 0001 | Piazza posts, instructor answers, Canvas announcements. `UNIQUE(platform, external_id)`, so the API and email agree |
| `sync_runs` | 0001 | One row per collector run: status, redacted error, record count, duration |
| `source_state` | 0001 | Per (source, key): last payload hash, ETag, Last-Modified, and source-specific JSON `data` |
| `notifications` | 0002 | Every reminder Argus tried to send: rule, text, `item_ids` (JSON; message number n maps to element n-1), status `pending`/`sent`/`failed`, channel, attempts, `expires_at` (when retries stop) |
| `notification_keys` | 0002 | The idempotency keys each notification covers (`<item_id>:<rule>:<due_at>`; a batched message covers several). `key` is the primary key, so no key is ever sent twice |
| `time_logs` | 0002 | Time spent: `source` `timer`/`sms`/`manual`; `ended_at` NULL while a timer runs (at most one, by a partial unique index) |
| `rule_mutes` | 0002 | Muted reminder rules, optionally until a time |
| `modules` | 0003 | Canvas modules per course: name, position, student `state`, items as JSON `[{title, type, url, indent}]` (links only; files stay in Canvas), and `digest` (AI study digest, Phase 5) |
| `courses.grade_*` | 0003 | Current grade from Canvas enrollments (`grade_score`, `grade_letter`, `grade_updated_at`) |
| `search_index` | 0003 | FTS5 (porter, unicode61, diacritics removed) over items, modules, and posts. Columns `title`, `body`; unindexed `kind`, `ref_id`, `course`, `url`. Rebuilt after every sync cycle |

Later phases add `findings`, `concepts`, `practice_items`, `practice_attempts`,
`exams`, `estimates`, `plan_blocks`, `inbound_messages`, `vault_regions`, and `proposals`
(the `modules` table landed in 0003),
each in its own migration, documented here when it lands.

## items
| Column | Notes |
|---|---|
| `kind` | `assignment` · `quiz` · `exam` · `event` (the most serious kind any source reports) |
| `due_at`, `due_source` | Earliest official due date (Canvas, Gradescope). Without one: manual, then finding |
| `late_due_at`, `release_at` | Latest late-due time after `due_at`; earliest release |
| `submitted`, `graded` | Tri-state: `1` if any source says so, `0` if any says not, `NULL` unknown |
| `description`, `description_hash` | First available description (Canvas API, then iCal). The hash re-triggers estimates |
| `date_conflict` | JSON `[[source, iso], …]` when sources disagree by more than 5 minutes |
| `conflict_resolved_at` | Set when the student resolves the conflict; cleared if the disagreement changes |
| `status` | `upcoming` · `in_progress` · `submitted` · `graded` · `missed`; recomputed every cycle (`domain/status.py`) |
| `user_done_at`, `started_at` | Student facts that feed status (`DONE n`, a timer or time log) |
| `removed_at` | Every source record is gone; the item is hidden but kept for history |

## item_sources
Stores each source's view: `title`, `kind`, `due_at`, `release_at`, `late_due_at`, `all_day`,
`points`, `submitted`, `graded`, `description`, `question_count`, `url`, `raw_hash`
(change detection), `present`, `first_seen_at`, `last_seen_at`, and `changed_at`. `item_id` is set by
the merge step. Links are sticky (see `domain/merge.py`).

## posts
`via` records the channel (`piazza`, `piazza_email`, `canvas_api`). `author_role` is `instructor`, `ta`,
`student`, or `unknown`. `kind` is `note`, `question`, `instructor_answer`, `announcement`, or `syllabus`.
`content_hash` changes on edit. `scanned_hash` is the hash at the last AI extraction (Phase 4).
