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
| `ai_calls` | 0004 | Every AI attempt: task, model, prompt version, input hash, status (`ok`, `invalid`, `refused`, `error`, `budget`, `cache_hit`), tokens, cost in USD. Month-to-date spend and the budget cap read from it |
| `ai_cache` | 0004 | Validated AI answers keyed by sha256(model, prompt version, rendered input, schema) |
| `findings` | 0004 | Facts extracted from posts: kind, title, `date` (UTC; `date_only` when the post gave a day only), scope, quote, confidence, matched `item_id`, `status` (`applied`, `pending`, `conflict`, `linked`, `stored`, `confirmed`, `dismissed`, `superseded`), and the decision's `reason` |
| `item_overrides` | 0004 | Student decisions: a confirmed due date that wins over sources, or a confirmed cancellation (item hidden) |
| `items.scope`, `items.logistics` | 0004 | Text applied from staff findings |
| `estimates` | 0004 | Versioned, append-only: `source` (`ai`, `default`, `user`), p50/p80 hours, confidence, breakdown and assumptions (JSON), reasoning, input hash, model, prompt version. User overrides can be retracted (`retracted_at`), never rewritten |
| `plan_blocks` | 0004 | Planned work (and, in Phase 5, review) sessions. Future blocks are replaced on replan; started ones are kept |
| `plan_state` | 0004 | The inputs hash of the current plan, its one-line explanation, and when it was made |
| `search_index` | 0003 | FTS5 (porter, unicode61, diacritics removed) over items, modules, and posts. Columns `title`, `body`; unindexed `kind`, `ref_id`, `course`, `url`. Rebuilt after every sync cycle |

| `materials` | 0005 | Text extracted from course files and pages, per page or slide (JSON `pages` `[{ref, text}]`), with the Canvas `url` the student opens. Files themselves are never stored or copied into the vault. `UNIQUE(course_id, url)` |
| `concepts` | 0005 | Verified concept notes: `slug`, `name`, `body` (JSON sections of `{text, cites, verified}` claims plus `diagram`), `sources` (JSON), `related` (JSON slugs), `priority` (raised by the confusion inbox), `input_hash` (generate+verify prompt ids + material hashes), `verified_at` |
| `practice_items` | 0005 | Verified questions and flashcards per concept, with `source_ref`; `origin` `generated` or `inbox`. Items no longer generated are deactivated, never deleted (attempt history stays) |
| `practice_attempts` | 0005 | Self-graded results (`correct`, `shaky`, `incorrect`). Readiness per concept derives from the latest attempt per item |
| `exams` | 0005 | Per exam item: scope and logistics (from findings) and the concepts in scope (JSON ids) |
| `vault_regions` | 0005 | Per (path, region): the hash Argus last wrote. `frontmatter` tracks Argus-owned keys; `file` tracks files outside the region model (D036) |
| `proposals` | 0005 | Changes waiting for approval: exact `before_text`/`after_text`, `base_hash` of the whole note, unified `diff`, `status` (`open`, `applied`, `rejected`, `expired`, `stale`, `superseded`) |
| `inbox_entries` | 0005 | Lines from `Argus/Inbox/`, keyed by `text_hash` (path + text), with the matched `concept_id`. `practice_at` (0006) is set once targeted practice was added |
| `vault_runs` | 0005 | One row per vault cycle: `ok`, `conflict` (kept the student's version), `error`, or `skipped`, plus commit and files written. Three `error` rows in a row raise a system alert |
| `vault_edit_log` | 0006 | Edits Argus made in notes opened to it with `argus-edit: allow`; the weekly note lists them |

| `inbound_messages` | 0007 | One row per Twilio `MessageSid` (UNIQUE, so a replay is answered once): the text, what Argus understood (`command`, `via` `rules`/`ai`/`none`), `status` (`done`, `unclear`, `failed`), the reply, and `item_ids` (what `[n]` meant in the reply, for TODAY) |
| `snoozes` | 0007 | Per item: reminders deferred until `until` (`SNOOZE n d`), except the 3-hour warning |
| `item_overrides.basis` | 0008 | The official due date an override was made against; when an official source changes it, the override is dropped (review R1) |
| `notifications.last_attempt_at` | 0008 | When delivery was last tried; failed sends retry with backoff until they expire (review R4) |

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
