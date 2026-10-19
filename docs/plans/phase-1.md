# Phase 1: Store and collectors

## Scope
The schema and migrations; the collectors (Canvas iCal, Canvas API assignments and submissions,
Gradescope, Piazza, Piazza email via Gmail, umd.io) against fixtures; merging; status; the
poll schedule; the sync runner; and the CLI commands `argus sync` and `argus list`.
Canvas modules, announcements, and files are Phase 3. The worker daemon loop is Phase 2, when there are rules to fire.

## Files
- `argus/store/db.py`: connect (WAL, foreign keys, busy timeout), explicit transactions, migration runner.
- `argus/store/migrations/0001_core.sql`: courses, meetings, items, item_sources, posts, sync_runs, source_state.
  Later tables arrive with their phase's migration.
- `argus/store/repo.py`: typed repository functions. All SQL lives here.
- `argus/domain/models.py`: enums and frozen dataclasses (`SourceItem`, `MeetingRecord`, `PostRecord`, `Item`).
- `argus/domain/titles.py`: title normalization and kind inference.
- `argus/domain/merge.py`: `merge_sources` (pure grouping, with sticky links) and `resolve_item` (field precedence).
- `argus/domain/status.py`: `derive_status` (pure).
- `argus/collectors/base.py`, `http.py` (retry, backoff with jitter, conditional GET, Link pagination), one module per source,
  `schedule.py` (pure staggered polling policy), `runner.py` (isolation, persistence, merge, status).
- `argus/cli.py`: `sync`, `list`, `db migrate`.

## Tests first
- `tests/unit/test_titles.py`: normalization examples and properties (idempotent, case and punctuation insensitive).
- `tests/unit/test_merge.py`: examples, plus hypothesis properties. The partition must be order-independent, idempotent
  under its own links, and never pair two records of the same family. Every fuzzy pair must match on course and title and be within 60 minutes. Links must be sticky.
- `tests/unit/test_resolve.py`: due-date precedence, conflicts, and submitted/graded aggregation.
- `tests/unit/test_status.py`: the transition table, including the edge at the due instant and late-due windows.
- `tests/unit/test_schedule.py`: stagger, intervals, and weekly umd.io, across a DST change.
- `tests/unit/test_http.py`: backoff with jitter (seeded), Retry-After, no retry on 401/403, 304 handling, cross-host Link rejection.
- `tests/integration/collectors/test_*.py`: each collector against fixtures. Covers a normal payload, an empty payload, malformed data,
  auth failure, pagination, and an unchanged payload (hash).
- `tests/integration/test_store.py`: migrations are idempotent, WAL is on, foreign keys are enforced.
- `tests/integration/test_sync.py`: the runner isolates a failing collector; merge over two cycles keeps item ids stable;
  a source item disappearing marks it absent; the Piazza auth failure switches to the email fallback.

## Decisions to record
- Merge links are sticky: auto-merge only attaches unlinked records and never fuses two existing items.
- Among official sources, the earliest due date wins, and any disagreement over 5 minutes is flagged.
- Recurring local times (meetings, commitments) are stored as local `HH:MM`; instants are stored in UTC.
- Unknown submission state counts as not submitted for reminders, so alerts stay safe.

## Risks
- Unofficial libraries: they use `requests`, so they are tested with `responses` against synthetic HTML and JSON, and their versions are pinned exactly.
- The Piazza email format is unverified until real mail arrives. The parser is tolerant and the live check is recorded.
- No credentials yet: the live smoke test is skipped and recorded.
