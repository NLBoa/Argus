# Runna Runs as Assignments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the student's Runna iCal feed and turn each planned run into an Argus assignment in a `RUN`
pseudo-course. It is due at the end of its day, its estimate is Runna's duration, and the Claude connector
receives it with a preferred start time until its day ends.

**Architecture:** A new collector (`collectors/runna_ical.py`, `Source.RUNNA_ICAL`) emits `SourceItem`s
that carry two new optional fields, `duration_minutes` and `preferred_start`. They are persisted on
`item_sources` (migration 0014). The collector also emits completed activities that have no planned twin in
the same fetch. The runner matches those against stored runs, so a run that Runna *replaces* with its
completed activity becomes SUBMITTED, not removed. A worker step, `runs.sync_run_estimates`, turns
durations into versioned estimates with source `runna`. The `RUN` course comes from a `runna:` config block
and is synced only while the collector is enabled. The connector is the main surface. The web UI only lists
the source on Health, and runs get no vault notes.

**Tech Stack:** Python 3.12, icalendar, SQLite, FastAPI/Jinja, FastMCP, pytest, respx, gitleaks.

**Spec:** `docs/superpowers/specs/2026-11-06-runna-runs-design.md`

## Global Constraints

- The course code is `RUN`, with the display name "Running". It is never accepted under `courses:` (the existing `^[A-Z]{4}\d{3}[A-Z]?$` rule already rejects it).
- A run is due at **23:59 America/New_York on the workout's local date**, with `all_day=True`. This holds even when Runna gave it a time.
- Defaults: `preferred_time` is `16:00`, `default_minutes` is `45` (allowed range 5–600).
- The repo is public. The feed URL is a secret: it never appears in the chat, git, logs, the vault, `sync_runs.error`, or connector output. Read it only from `$ARGUS_RUNNA_ICAL_URL` and never echo it. Workout descriptions are never sent to the connector.
- Runs are never MISSED. `RUNNA_ICAL` is not in `SUBMISSION_SOURCES`, so `tracked` stays False (D056).
- No AI call is ever made for a `RUN` item. Runna estimates are not calibrated (factor 1.0). The student's own estimate still wins.
- Hidden, not empty (D055): nothing about runs shows unless `caps.has(Feature.RUNS)`.
- Time rules: store UTC and never call `datetime.now()`. Fixtures are synthetic.
- Reminders are unchanged. Runs get the normal `due_24h` and `due_3h` alerts. The deadline golden (`tests/fixtures/sim/deadline_alerts.json`) must not change.
- Commits are conventional, made at a green state, and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A late-evening timed run near the UTC date line.** A run at 22:30 New York time on Fri Nov 13 is 03:30Z on Nov 14. It must be due 23:59 on **Nov 13** local (`2026-11-14T04:59Z`). Tested in Task 2.
2. **DST day.** A run on 2026-11-01 (fall back) is due at 23:59 EST (`2026-11-02T04:59Z`), not EDT. Tested in Task 2.
3. **Duration text that isn't one total.** A labeled total wins. Paces are never durations: "5:30/km", "8:45 min/mi", and "6:30-7:00/km" give `None`. Distances like "8 km" and "5 x 400m" give `None`. A workout that lists several different durations with no labeled total ("Warm up 10 min · 4 x 3 min @ 5:10/km · cool down 10 min") gives `None`, and the default then applies with low confidence. "1h 5m", "65 min", and "1:05:00" give 65. Tested in Task 2.
4. **Half setup.** If the `runna:` block is present with no secret, or the secret is set with no block, nothing about runs appears: no RUN course row, nothing on the web pages, and nothing from the connector. Tested in Tasks 3, 4, 5, and 6.
5. **A finished run recorded as removed.** If Runna replaces a planned workout with its completed activity, the run becomes SUBMITTED and its change status is never "removed". Tested in Task 3.

---

### Task 0: Learn the real feed shape (no code)

The student sets `ARGUS_RUNNA_ICAL_URL` in the environment. Don't ask for the URL, and never print, log, or paste its value.

- [ ] **Step 1:** Ask the student to set Runna's completed-activities option (Profile → Connected Apps & Devices → Calendars) to **"Completed plan workouts only"**.
- [ ] **Step 2:** Fetch once with `curl -s "$ARGUS_RUNNA_ICAL_URL" -o <scratchpad>/runna.ics`. Don't use `-v`, and don't echo the variable. Write the URL's shape in the task notes with the token redacted (for example `https://<host>/<path>/<TOKEN: 32 hex>`), covering the host, path pattern, and token alphabet and length. Task 1's gitleaks rule needs this.
- [ ] **Step 3:** Write down, in the task notes (not the repo):
  - whether planned runs are timed or all-day;
  - where a duration appears (DTEND, DESCRIPTION, or SUMMARY), the exact label of a total if there is one (e.g. "Estimated duration"), and whether Runna ever writes mm:ss durations or bare "45m";
  - every kind of event in the feed (runs, strength or mobility sessions, rest days, race day, notes) and the field that tells them apart (SUMMARY wording, CATEGORIES, or DESCRIPTION).
- [ ] **Step 4:** Have the student complete a workout, or use one already done this week. Fetch again into `<scratchpad>/runna-after.ics` and record which case happened to the planned event:
  - **(a)** it is still there, with a completed twin next to it;
  - **(b)** it kept its UID and some fields changed (record which ones);
  - **(c)** it is gone, replaced by an event with a new UID.

  Also record how a completed event is marked (UID prefix, SUMMARY marker, or STATUS) and whether its title matches the planned one after the marker is stripped.
- [ ] **Step 5:** If any finding contradicts Task 1's or Task 2's assumptions below, change the gitleaks regex, the fixtures, `TOTAL_LABELS`, `is_completed`, `is_run`, or the duration rules to match. Note each change for D078.

### Task 1: Config, secret, source, and gitleaks rule

**Files:**
- Modify: `argus/config.py` (add `RunnaConfig`, `AppConfig.runna`, `running_course`, `all_courses`, and `Secrets.runna_ical_url`)
- Modify: `argus/domain/models.py`:
  - add `Source.RUNNA_ICAL = "runna_ical"`;
  - add `duration_minutes: int | None = None` and `preferred_start: datetime | None = None` to `SourceItem`;
  - add `CompletedRun`.
- Modify: `config.example.yaml` (a commented `runna:` block)
- Modify: `.gitleaks.toml` (a `runna-ical-feed` rule)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - `RUN_CODE: Final = "RUN"` and `RUN_TITLE: Final = "Running"` in `argus/config.py`
  - `class RunnaConfig(_Strict)` with `preferred_time: time = time(16, 0)` (parsed from `"HH:MM"`) and `default_minutes: int = Field(45, ge=5, le=600)`. It has no accent: nothing would use it.
  - `AppConfig.runna: RunnaConfig | None = None`
  - `AppConfig.running_course() -> CourseConfig | None`: `CourseConfig.model_construct(code="RUN", section="0000")` when `runna` is set
  - `AppConfig.all_courses(*, running: bool) -> list[CourseConfig]`: `courses`, plus the running course last when `running` is true and it exists
  - `AppConfig.course()` is unchanged, so `course("RUN")` stays `None` and `/courses/RUN` stays a 404.
  - `Secrets.runna_ical_url: SecretStr | None = None`
  - `@dataclass(frozen=True, slots=True) class CompletedRun` with `external_id: str`, `local_day: date`, `title_key: str`, and `raw_hash: str`. This is a completed activity with no planned twin in its fetch.

- [ ] **Step 1: Write the failing tests**

```python
def test_runna_block_defaults():
    cfg = AppConfig.model_validate({"runna": {}})
    assert cfg.runna.preferred_time == time(16, 0) and cfg.runna.default_minutes == 45

def test_runna_preferred_time_rejects_bad_value():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"runna": {"preferred_time": "25:00"}})

def test_running_course_only_with_block():
    assert AppConfig().running_course() is None
    cfg = AppConfig.model_validate({"runna": {}, "courses": [CMSC351]})
    assert cfg.running_course().code == "RUN" and cfg.course("RUN") is None
    assert [c.code for c in cfg.all_courses(running=True)] == ["CMSC351", "RUN"]
    assert [c.code for c in cfg.all_courses(running=False)] == ["CMSC351"]

def test_run_is_not_a_course_code():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"courses": [{"code": "RUN", "section": "0101"}]})

def test_runna_secret_loads_and_is_redacted():
    s = load_secrets({"ARGUS_RUNNA_ICAL_URL": SYNTHETIC_RUNNA_URL})
    assert SYNTHETIC_RUNNA_URL in s.secret_values()
```

`SYNTHETIC_RUNNA_URL` has Task 0's host and path with the token `FAKEFEED`. Like the Canvas test URLs (`user_FAKEFEED.ics`), it is deliberately shorter than the rule's token pattern, so committed tests never trip gitleaks.

- [ ] **Step 2:** Run `uv run pytest tests/unit/test_config.py -k "runna or running or run_is" -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the interfaces above. The `SourceItem` fields both default to `None`, so existing collectors are unchanged.
- [ ] **Step 4:** Add the rule to `.gitleaks.toml`, modeled on `canvas-ical-feed`:

```toml
[[rules]]
id = "runna-ical-feed"
description = "Runna calendar feed URL (the URL itself is a credential)"
regex = '''<host and path pattern from Task 0, escaped>/<token pattern from Task 0>'''
```

- [ ] **Step 5:** Check the rule from the shell, without writing anything to a file:
  - it fires on a full-length synthetic token (made up, matching Task 0's token alphabet and length);
  - it stays quiet on the short test URL and on the bare host.

```bash
FULL="https://<host>/<path>/$(python3 -c 'print("0123456789abcdef"*2)')"   # adjust to Task 0's token shape
printf '%s\n' "$FULL" | gitleaks stdin --config .gitleaks.toml --no-banner --redact; echo "exit=$?"
printf '%s\n' "https://<host>/<path>/FAKEFEED" | gitleaks stdin --config .gitleaks.toml --no-banner; echo "exit=$?"
printf '%s\n' "https://<host>/" | gitleaks stdin --config .gitleaks.toml --no-banner; echo "exit=$?"
```

Expected: `exit=1` with one `runna-ical-feed` finding, then `exit=0`, then `exit=0`.
- [ ] **Step 6:** Run `uv run pytest tests/unit/test_config.py -v && make types`. Expected: PASS, and mypy is clean.
- [ ] **Step 7: Commit:** `feat(runs): runna config block, secret, source, and gitleaks rule (D078)`

### Task 2: Pure feed parser

**Files:**
- Create: `argus/collectors/runna_ical.py` (pure functions only in this task)
- Create: `tests/fixtures/runna/feed.ics`, `feed_replaced.ics`, and `feed_replaced_renamed.ics` (synthetic, shaped like Task 0's findings)
- Test: `tests/unit/test_runna_parse.py`

**Interfaces:**
- Consumes: `RunnaConfig`, `RUN_CODE`, `SourceItem`, `Source.RUNNA_ICAL`, `CompletedRun`
- Produces:
  - `TOTAL_LABELS: tuple[str, ...] = ("estimated duration", "total time", "duration")`. Task 0 replaces these with Runna's actual label(s).
  - `parse_duration_text(text: str) -> int | None`: minutes, using the rules below, in this order:
    1. **Labeled total.** If a `TOTAL_LABELS` label (case-insensitive) is followed, after an optional `:`, by a duration, return that duration. Inside a labeled total, a bare `Xm` means minutes (`"Duration: 45m"` → 45).
    2. **Remove paces.** Delete pace expressions before looking further:
       - `m:ss` or `m:ss-m:ss`, optionally followed by `min`, then `/km` or `/mi`;
       - any number followed by `/km` or `/mi`.
    3. **Find durations:**
       - `Xh Ym` / `X hr Y min` (one duration, X·60+Y);
       - `X h` / `X hr` / `X hours`;
       - `X min` / `X mins` / `X minutes`;
       - `h:mm:ss`.

       Outside a labeled total, a bare `Xm` is minutes **only** as the minutes part of `Xh Ym`. On its own it is ambiguous with metres, so `45m` and `400m` aren't durations. `mm:ss` isn't a duration unless Task 0 shows Runna uses it.
    4. Exactly one **distinct** value gives that value. None, or more than one distinct value, gives `None`.
  - `normalize_title(text: str) -> str`: strip the completion marker (Task 0), casefold, and collapse whitespace.
  - `is_completed(ev: Component) -> bool`. Default until Task 0: SUMMARY starts with `"✓"` or UID starts with `"activity-"`.
  - `is_run(ev: Component) -> bool`. Default until Task 0: False when SUMMARY (case-insensitive) matches `rest|strength|mobility|cross[- ]?train|yoga|stretch|note`. A race-day event **is** a run.
  - `run_due(local_day: date) -> datetime`: 23:59 that day in New York, as UTC.
  - `@dataclass(frozen=True) class RunnaFeed` with `runs: list[SourceItem]` and `completed: list[CompletedRun]`.
  - `parse_runna_feed(body: bytes, runna: RunnaConfig) -> RunnaFeed`: raises `ValueError` if the body is malformed. Only events where `is_run` is true count.
    - **Planned runs** go into `runs`. Each has:
      - `source=RUNNA_ICAL`, `external_id=f"uid-{sha256(uid)[:16]}"`, `course_code="RUN"`, `kind=ASSIGNMENT`, `all_day=True`;
      - `due_at=run_due(local date)`;
      - `title` = SUMMARY (stripped, or `"Run"` when empty), `description` = DESCRIPTION;
      - `duration_minutes`, `preferred_start`, and `raw_hash=sha256(ev.to_ical())`;
      - `submitted=True` when a completed event in the same fetch has the same local day and `normalize_title`, otherwise `None`.
    - **Completed events with no planned twin** in this fetch go into `completed`, as `CompletedRun(external_id=<its uid hash>, local_day, title_key=normalize_title(SUMMARY), raw_hash)`. Completed events never become items directly.
    - **Duration order:**
      1. A timed event's DTEND − DTSTART, rounded to minutes and kept only if 1–600.
      2. Otherwise `parse_duration_text(DESCRIPTION)`, then `parse_duration_text(SUMMARY)`.
      3. Otherwise `None`.

      `default_minutes` is applied later, in Task 4, so the source record stays honest.
    - **`preferred_start`:** a timed event's DTSTART (UTC); otherwise `runna.preferred_time` on the local day, converted to UTC.
  - `@dataclass(frozen=True) class StoredRun` with `external_id`, `title`, `due_at`, `description`, `duration_minutes`, `preferred_start` (the fields of a stored `runna_ical` record), and `open: bool` (its item isn't done yet).
  - `match_completed(completed: Sequence[CompletedRun], stored: Sequence[StoredRun]) -> list[SourceItem]`. For each completed event, try these in order and stop at the first that finds **exactly one** stored run:
    1. **Same id:** a stored run with the same `external_id` (case b in Task 0).
    2. **Same day and title:** the stored run's local due date equals `local_day` and `normalize_title(stored.title) == title_key`.
    3. **Only open run that day:** exactly one `open` stored run has its local due date on `local_day`. This is safe because Task 0 sets "Completed plan workouts only", so every completed event is a plan workout. It covers case (c) with a renamed activity, such as "Morning Run".

    If no step finds exactly one, the event is dropped. For a match, emit a `SourceItem` that copies the stored run's id, title, due date, description, duration, and preferred start, with `submitted=True` and `raw_hash=completed.raw_hash`.

The fixture `feed.ics` holds these synthetic events:
- (a) a timed "Tempo run" Tue 2026-11-10 16:00–16:45 EST (`21:00Z`–`21:45Z`);
- (b) an all-day "Easy run" Wed 2026-11-11 with DESCRIPTION `"Easy run · 50 min"`;
- (c) an all-day "Steady run" Thu 2026-11-12 with DESCRIPTION `"8 km steady"`;
- (d) a timed "Night run" Fri 2026-11-13 22:30–23:10 local (`03:30Z`–`04:10Z` on the 14th);
- (e) an all-day "Long run" Sun 2026-11-01 (the fall-back day);
- (f) a completed activity for (a);
- (g) a completed activity with no planned twin;
- (h) an all-day "Rest day" on Mon 2026-11-09;
- (i) a timed "Strength & mobility" session on Thu 2026-11-12.

`feed_replaced.ics` is `feed.ics` with (a) and (f) removed and (a') added: (a)'s completed activity with a **new UID**, the same day, and the title "✓ Tempo run". `feed_replaced_renamed.ics` is the same, except that (a') is titled "✓ Morning Run".

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text,minutes", [
    ("45 min", 45), ("Easy · 50 mins", 50), ("1h 5m", 65), ("1 hr 5 min", 65), ("1:05:00", 65),
    ("65 min", 65), ("45m", None), ("65:00", None), ("8 km", None), ("5 x 400m", None),
    ("Zone 2", None), ("", None),
    ("Warm up 10 min · 4 x 3 min @ 5:10/km · cool down 10 min", None),
    ("5:30/km", None), ("8:45 min/mi", None), ("6:30-7:00/km", None),
    ("Estimated duration: 42 min · Warm up 10 min · 4 x 3 min @ 5:10/km · cool down 10 min", 42),
    ("Estimated duration 1h 5m · easy 6:30/km", 65),
    ("Duration: 45m", 45),                 # bare m inside a labeled total means minutes
    ("Easy 45m · strides", None),          # bare m elsewhere is not a duration
])
def test_parse_duration_text(text, minutes):
    assert parse_duration_text(text) == minutes

def test_items_from_fixture():
    feed = parse_runna_feed(FIXTURE, RunnaConfig())
    items = {i.title: i for i in feed.runs}
    assert set(items) == {"Tempo run", "Easy run", "Steady run", "Night run", "Long run"}
    a = items["Tempo run"]
    assert a.due_at == datetime(2026, 11, 11, 4, 59, tzinfo=UTC)   # 23:59 EST Nov 10
    assert a.duration_minutes == 45 and a.preferred_start == datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
    assert a.submitted is True                                       # twin (f)
    b = items["Easy run"]
    assert b.duration_minutes == 50 and b.preferred_start == datetime(2026, 11, 11, 21, 0, tzinfo=UTC)
    assert items["Steady run"].duration_minutes is None and items["Steady run"].submitted is None
    assert [c.title_key for c in feed.completed] == [G_TITLE_KEY]    # (g) only; (f) had a twin

def test_rest_days_and_strength_sessions_are_skipped():
    titles = {i.title for i in parse_runna_feed(FIXTURE, RunnaConfig()).runs}
    assert "Rest day" not in titles and "Strength & mobility" not in titles

def test_late_timed_run_is_due_on_its_local_day():      # Review Focus 1
    d = by_title("Night run")
    assert d.due_at == datetime(2026, 11, 14, 4, 59, tzinfo=UTC)    # 23:59 EST Fri Nov 13
    assert d.duration_minutes == 40

def test_fall_back_day_due_is_est():                     # Review Focus 2
    assert by_title("Long run").due_at == datetime(2026, 11, 2, 4, 59, tzinfo=UTC)

def test_custom_preferred_time():
    b = by_title("Easy run", RunnaConfig(preferred_time=time(7, 30)))
    assert b.preferred_start == datetime(2026, 11, 11, 12, 30, tzinfo=UTC)

def test_replaced_run_is_a_completed_orphan():
    feed = parse_runna_feed(FIXTURE_REPLACED, RunnaConfig())
    assert "Tempo run" not in {i.title for i in feed.runs}
    assert any(c.title_key == "tempo run" and c.local_day == date(2026, 11, 10) for c in feed.completed)

def test_match_completed_by_day_and_title():
    stored = [StoredRun(TEMPO_ID, "Tempo run", TEMPO_DUE, None, 45, TEMPO_START, True)]
    [hit] = match_completed([CompletedRun("uid-new", date(2026, 11, 10), "tempo run", "h1")], stored)
    assert hit.external_id == TEMPO_ID and hit.submitted is True and hit.duration_minutes == 45

def test_match_completed_by_same_uid():
    stored = [StoredRun(TEMPO_ID, "Tempo run", TEMPO_DUE, None, 45, TEMPO_START, True)]
    [hit] = match_completed([CompletedRun(TEMPO_ID, date(2026, 11, 10), "renamed", "h1")], stored)
    assert hit.external_id == TEMPO_ID

def test_match_completed_only_open_run_that_day():             # case (c), renamed
    stored = [StoredRun(TEMPO_ID, "Tempo run", TEMPO_DUE, None, 45, TEMPO_START, True)]
    [hit] = match_completed([CompletedRun("uid-new", date(2026, 11, 10), "morning run", "h1")], stored)
    assert hit.external_id == TEMPO_ID and hit.submitted is True

def test_match_completed_two_runs_that_day_is_no_match():
    stored = [StoredRun(TEMPO_ID, "Tempo run", TEMPO_DUE, None, 45, TEMPO_START, True),
              StoredRun("uid-x", "Shakeout", TEMPO_DUE, None, 20, TEMPO_START, True)]
    assert match_completed([CompletedRun("uid-new", date(2026, 11, 10), "morning run", "h1")], stored) == []

def test_match_completed_ignores_done_runs_for_the_day_fallback():
    stored = [StoredRun(TEMPO_ID, "Tempo run", TEMPO_DUE, None, 45, TEMPO_START, False)]
    assert match_completed([CompletedRun("uid-new", date(2026, 11, 10), "morning run", "h1")], stored) == []

def test_unmatched_completed_is_dropped():                     # fixture (g)
    assert match_completed([CompletedRun("uid-g", date(2026, 11, 9), G_TITLE_KEY, "h")], STORED) == []

def test_malformed_feed_raises():
    with pytest.raises(ValueError):
        parse_runna_feed(b"not ical", RunnaConfig())

def test_every_item_is_a_run_assignment():
    assert all(i.course_code == "RUN" and i.kind is ItemKind.ASSIGNMENT and i.all_day
               for i in parse_runna_feed(FIXTURE, RunnaConfig()).runs)
```

- [ ] **Step 2:** Run `uv run pytest tests/unit/test_runna_parse.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement. Use `icalendar` as `canvas_ical.py` does, and `LOCAL_TZ` from `argus.clock` for the local date. A floating time counts as local.
- [ ] **Step 4:** Run `uv run pytest tests/unit/test_runna_parse.py -v`. Expected: PASS.
- [ ] **Step 5: Commit:** `feat(runs): parse Runna's calendar feed into run assignments`

### Task 3: Collect, store, and sync

**Files:**
- Modify: `argus/collectors/runna_ical.py` (add `RunnaIcalCollector`)
- Modify: `argus/collectors/base.py` (`CollectResult.completed_runs: tuple[CompletedRun, ...] = ()`)
- Modify: `argus/collectors/runner.py`:
  - register the collector in `default_collectors()`;
  - `run_cycle` calls `sync_courses(..., running=<the runna collector's enabled(ctx)>)`;
  - `_persist` matches completed runs.
- Create: `argus/store/migrations/0014_runna.sql`
- Modify: `argus/store/repo.py` (`sync_courses`, `upsert_source_items`, new `run_details`, new `stored_runs`)
- Modify: `argus/collectors/schedule.py` (a `POLICIES` entry for `runna_ical`, the same as `canvas_ical`)
- Modify: `docs/schema.md`
- Test: `tests/integration/collectors/test_runna_ical.py`, `tests/integration/test_sync.py`

**Interfaces:**
- Consumes: `parse_runna_feed`, `match_completed`, `StoredRun`, `AppConfig.all_courses`
- Produces:
  - `class RunnaIcalCollector`:
    - `source = Source.RUNNA_ICAL`;
    - `enabled(ctx)` is `ctx.config.runna is not None and ctx.secrets.runna_ical_url is not None`;
    - `collect` mirrors `CanvasIcalCollector.collect` (ETag/Last-Modified, the payload hash, state key `"feed"`);
    - it returns `items=feed.runs`, `completed_runs=feed.completed`, and `complete_scopes={"RUN": now - RUNNA_LOOKBACK}`, with `RUNNA_LOOKBACK = timedelta(days=7)`.
  - `repo.stored_runs(conn, *, since: datetime, exclude: Collection[str]) -> list[StoredRun]`: `runna_ical` records due at or after `since`, whose external id isn't in `exclude`. `open` is the item's `status.is_open`.
  - `_persist`, for a `RUNNA_ICAL` result and **before** `upsert_source_items` and the absent marking: `items = result.items + tuple(match_completed(result.completed_runs, repo.stored_runs(conn, since=now - RUNNA_LOOKBACK, exclude={i.external_id for i in result.items})))`. A matched run is therefore present in this fetch and never marked absent.
  - `repo.sync_courses(conn, config, now, *, running: bool = False) -> dict[str, int]`: upserts `config.all_courses(running=running)`, sets the RUN row's `title` to `RUN_TITLE`, and deactivates anything else as it does today.
  - Migration 0014 adds:
    - `ALTER TABLE item_sources ADD COLUMN duration_minutes INTEGER`;
    - `ALTER TABLE item_sources ADD COLUMN preferred_start TEXT`;
    - a rebuild of `estimates` with `CHECK (source IN ('ai','default','user','runna'))`, following 0009's `time_logs` rebuild: create `estimates_new` with the same columns and `UNIQUE (item_id, version)`, `INSERT … SELECT` keeping ids, `DROP TABLE estimates`, `ALTER TABLE estimates_new RENAME TO estimates`. No table references `estimates`, so this is safe with `foreign_keys=ON` inside `migrate()`'s `BEGIN IMMEDIATE`. There are no other indexes to recreate.
  - `upsert_source_items` writes both new columns.
  - `repo.run_details(conn, item_ids: Sequence[int]) -> dict[int, RunDetail]`, where `RunDetail(duration_minutes: int | None, preferred_start: datetime | None)` comes from the item's present `runna_ical` record.

- [ ] **Step 1: Write the failing tests** (respx serves the Task 2 fixtures; `FakeClock` is set to 2026-11-09T12:00Z)

```python
async def test_runs_land_in_the_running_course(runna_env):
    await runna_env.cycle()
    runs = [i for i in repo.list_items(runna_env.conn) if i.course_code == "RUN"]
    assert len(runs) == 5  # (e) Nov 1 is stored; the lookback only protects it from removal
    assert repo.active_course_titles(runna_env.conn)["RUN"] == "Running"

async def test_second_identical_fetch_is_unchanged(runna_env): ...   # result status UNCHANGED

async def test_deleted_workout_is_removed(runna_env):
    await runna_env.cycle(); runna_env.serve(feed_without("Easy run")); await runna_env.cycle()
    assert "Easy run" not in titles(runna_env.conn)
    assert change_status(runna_env.conn, "Easy run") == "removed"

async def test_moved_workout_rearms_reminders(runna_env):
    # move "Easy run" from Nov 11 to Nov 12 -> due changes, item_changes says "changed",
    # and the notifications key for due_24h now carries the new due ISO
    ...

async def test_completed_run_is_submitted_and_never_missed(runna_env):
    runna_env.clock.set(datetime(2026, 11, 13, 12, 0, tzinfo=UTC)); await runna_env.cycle()
    assert status_of(runna_env.conn, "Tempo run") is ItemStatus.SUBMITTED
    assert status_of(runna_env.conn, "Steady run") is not ItemStatus.MISSED

async def test_completed_replacing_planned_is_submitted_not_removed(runna_env):   # Review Focus 5
    await runna_env.cycle()                                    # fetch 1: planned "Tempo run"
    runna_env.serve(FIXTURE_REPLACED); runna_env.clock.set(datetime(2026, 11, 10, 23, 0, tzinfo=UTC))
    await runna_env.cycle()                                    # fetch 2: replaced by its completed event
    assert status_of(runna_env.conn, "Tempo run") is ItemStatus.SUBMITTED
    assert change_status(runna_env.conn, "Tempo run") != "removed"

async def test_renamed_completed_replacing_planned_is_submitted(runna_env):      # case (c), renamed
    await runna_env.cycle()
    runna_env.serve(FIXTURE_REPLACED_RENAMED); runna_env.clock.set(datetime(2026, 11, 10, 23, 0, tzinfo=UTC))
    await runna_env.cycle()                                    # "✓ Morning Run" is the only run that day
    assert status_of(runna_env.conn, "Tempo run") is ItemStatus.SUBMITTED
    assert change_status(runna_env.conn, "Tempo run") != "removed"

async def test_half_setup_has_no_running_course(tmp_db):           # Review Focus 4
    for cfg, secrets in [(with_block, no_secret), (no_block, with_secret)]:
        await cycle(cfg, secrets)
        assert "RUN" not in repo.active_course_titles(tmp_db)

async def test_fetch_error_is_redacted(runna_env):
    runna_env.fail_with(httpx.ConnectError(f"cannot reach {SYNTHETIC_RUNNA_URL}"))
    await runna_env.cycle()
    assert SYNTHETIC_RUNNA_URL not in last_sync_run(runna_env.conn, "runna_ical").error
```

- [ ] **Step 2:** Run `uv run pytest tests/integration/collectors/test_runna_ical.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the collector, `completed_runs`, the registration, the policy, the migration, the repo changes, and `docs/schema.md` (the two columns and the CHECK).
- [ ] **Step 4:** Run `uv run pytest tests/integration/collectors/test_runna_ical.py tests/integration/test_sync.py tests/integration/test_store.py tests/integration/test_deadline_golden.py -v`. Expected: PASS, with the golden unchanged.
- [ ] **Step 5: Commit:** `feat(runs): collect Runna workouts into the Running course`

### Task 4: Capabilities and Runna estimates

**Files:**
- Modify: `argus/capabilities.py`:
  - add `Feature.RUNS`;
  - add `FEATURES[RUNS] = _any({"runna_ical"})`;
  - build the `courses` map from `config.all_courses(running=sources["runna_ical"])`, with RUN's `CourseCaps` all False.
- Create: `argus/runs.py`
- Modify: `argus/estimates.py` (`counts`, `needs_estimate`, `estimate_state`, `effective_estimates`)
- Modify: `argus/worker.py` (a `("runs", …)` step right after `"sync"`, with `SYNC_BUDGET`)
- Modify: `argus/web/routes/actions.py` (`rerun_estimate` refuses RUN items)
- Test: `tests/unit/test_capabilities.py`, `tests/unit/test_estimates.py` (create it if absent), `tests/integration/test_estimates.py`, `tests/integration/test_web_estimates.py`

**Interfaces:**
- Consumes: `repo.run_details`, `repo.insert_estimate`, `RunnaConfig.default_minutes`
- Produces:
  - `Feature.RUNS = "runs"`
  - `runs.sync_run_estimates(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> int`. For each open `RUN` item:
    - let `minutes` be the Runna duration, or `default_minutes` with `fallback=True`;
    - when the latest `runna` row's `input_hash` differs, append a row with `source="runna"`, `p50=p80=minutes/60`, `input_hash=f"runna:{minutes}"` (or `"runna-default:{minutes}"`), and `confidence="high"` (`"low"` for a fallback);
    - on a fallback, also set `assumptions=("Runna gave no length; using the default",)`.

    Returns how many rows were appended.
  - `counts(row)` is also True for `source == "runna"`.
  - `needs_estimate(...)` returns False when `item.course_code == RUN_CODE`.
  - `estimate_state(...)` returns `"runna"` when there is no user row and `base.source == "runna"`.
  - `effective_estimates` uses factor `1.0` when the base row's source is `runna`.
  - `rerun_estimate` (`POST /items/{id}/estimate/rerun`, which calls `estimate_item(..., force_ai=True)`) returns 404 for an item whose `course_code == RUN_CODE`, before any estimator call. This follows D076's rule that a route which doesn't apply to an item is a 404. It enforces the global "no AI call for a RUN item" rule.

- [ ] **Step 1: Write the failing tests**

```python
def test_rerun_estimate_refuses_runs(client_with_run_and_fake_transport):
    resp = client.post(f"/items/{RUN_ID}/estimate/rerun", headers=SAME_ORIGIN)
    assert resp.status_code == 404
    assert fake_transport.calls == []                        # no AI call

def test_runs_feature_needs_block_and_secret():
    assert derive(cfg_with_block, secrets_with_url).has(Feature.RUNS)
    assert not derive(cfg_with_block, Secrets()).has(Feature.RUNS)
    assert not derive(AppConfig(), secrets_with_url).has(Feature.RUNS)
    assert "RUN" in derive(cfg_with_block, secrets_with_url).courses

def test_run_items_never_go_to_the_ai():
    assert not needs_estimate(run_item, None, None, overridden=False, ai_available=True, now=NOW)

# integration
def test_runna_duration_becomes_the_estimate(db_with_runs):
    sync_run_estimates(db, cfg, NOW)
    eff = effective_estimates(db, NOW)[tempo_id]
    assert eff.p50 == pytest.approx(0.75) and eff.factor == 1.0

def test_changed_duration_appends_a_version(db_with_runs):
    sync_run_estimates(db, cfg, NOW); set_duration(tempo, 60); sync_run_estimates(db, cfg, NOW)
    assert [r.p50 for r in repo.estimates_for(db, tempo_id)] == [0.75, 1.0]
    assert sync_run_estimates(db, cfg, NOW) == 0   # idempotent

def test_missing_duration_uses_default(db_with_runs):
    sync_run_estimates(db, cfg, NOW)
    row = repo.estimates_for(db, steady_id)[-1]
    assert row.p50 == pytest.approx(0.75) and row.confidence == "low"

def test_student_override_beats_runna(db_with_runs):
    sync_run_estimates(db, cfg, NOW); set_override(db, tempo, 1.5, NOW); sync_run_estimates(db, cfg, NOW)
    assert effective_estimates(db, NOW)[tempo_id].p50 == pytest.approx(1.5)
```

- [ ] **Step 2:** Run `uv run pytest tests/unit/test_capabilities.py tests/unit/test_estimates.py tests/integration/test_estimates.py tests/integration/test_web_estimates.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement:
  - Update the pinned `FEATURES` table test and D054's feature list.
  - The worker step is `("runs", self._runs, SYNC_BUDGET)`. `async def _runs(self) -> None` returns at once when `not self.caps.has(Feature.RUNS)`; otherwise it calls `sync_run_estimates(self.conn, self.config, self.clock.now())` inside `transaction(self.conn)`.
- [ ] **Step 4:** Run the same command, then `make test-unit` and `make cov`. Expected: PASS, with coverage floors held for `capabilities.py`.
- [ ] **Step 5: Commit:** `feat(runs): Runna's length is the run's estimate`

### Task 5: Claude connector

**Files:**
- Modify: `argus/feed.py`, `argus/web/mcp_server.py`
- Test: `tests/unit/test_feed.py`, `tests/integration/test_mcp_server.py`

**Interfaces:**
- Consumes: `effective_estimates(conn, now)`, `repo.run_details`, `RUN_CODE`
- Produces:
  - `ItemType` gains `"run"`, and `SourceName` gains `"runna"`.
  - `item_type(kind, title, course: str) -> ItemType` returns `"run"` for course `RUN`. Every caller passes the course.
  - `_source_name(Source.RUNNA_ICAL, …)` returns `"runna"`.
  - `listed(item: Item, now: datetime) -> bool` (pure). An item is listed if it has a due date, and either it is due at or after `now`, or it is not a run and isn't done. A run whose day has ended (`due_at < now`) leaves the connector even if it isn't done. Coursework keeps its 7-day overdue tail. `upcoming()` filters with it, replacing the inline `keep` expression.
  - `WorkItem.estimate_minutes: int | None`: `round(effective.p50 * 60)` for any item that has an estimate. Field description: `"How long it should take, in minutes (the student's own time, Runna's, or Argus's estimate)."`
  - `WorkItem.preferred_start: str | None`: runs only, `local_iso(detail.preferred_start)`. Field description: `"Runs only: a good time to start (local). Flexible: any free time that day works."`
  - `to_work_item(..., estimate_minutes: int | None = None, preferred_start: datetime | None = None)`, filled by `_items_out`.
  - `INSTRUCTIONS`:
    - change "from Canvas (ELMS) and Gradescope" to "from Canvas (ELMS), Gradescope, and the student's Runna running plan";
    - append: `"Runs (type run) are due by the end of their day and are flexible: schedule one near preferred_start if that time is free, otherwise any free time that day, for estimate_minutes. A run drops out once its day ends; don't reschedule it."`
  - `get_upcoming_work`'s docstring says that runs aren't listed after their day ends.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_work_item():
    w = to_work_item(run_item, NOW, updated_at=NOW, estimate_minutes=45,
                     preferred_start=datetime(2026, 11, 10, 21, 0, tzinfo=UTC))
    assert w.type == "run" and w.source == "runna" and w.course == "RUN"
    assert w.estimate_minutes == 45 and w.preferred_start == "2026-11-10T16:00:00-05:00"
    assert "description" not in w.model_dump()

def test_coursework_gets_estimate_minutes_and_no_preferred_start():
    w = to_work_item(hw_item, NOW, updated_at=NOW, estimate_minutes=90)
    assert w.estimate_minutes == 90 and w.preferred_start is None

def test_ended_run_is_not_listed():
    assert not listed(run_due(NOW - timedelta(minutes=1), done=False), NOW)
    assert listed(run_due(NOW + timedelta(hours=3), done=False), NOW)

def test_overdue_coursework_is_still_listed():
    assert listed(hw_due(NOW - timedelta(days=2), done=False), NOW)
    assert not listed(hw_due(NOW - timedelta(days=2), done=True), NOW)

# integration, through the MCP client
async def test_upcoming_work_includes_runs(mcp_with_runs):
    out = await call(mcp_with_runs, "get_upcoming_work", days=7)
    run = next(i for i in out["items"] if i["type"] == "run")
    assert run["estimate_minutes"] == 45 and run["preferred_start"].endswith("-05:00")
    assert SYNTHETIC_RUNNA_URL not in json.dumps(out)

async def test_runs_hidden_when_runna_not_set_up(mcp_without_runna_secret): ...  # Review Focus 4: no type "run" (RUN course inactive)
```

- [ ] **Step 2:** Run `uv run pytest tests/unit/test_feed.py tests/integration/test_mcp_server.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. `effective_estimates` already only reads, so it works on the read-only connection.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit:** `feat(mcp): runs and estimate minutes in the Claude connector`

### Task 6: Health label, no dead course links, and plain vault lines

Runs already render on the web through the existing assignment rows. Their course color comes from `course_view()`'s `"slate"` fallback, so no view work is needed. There is no `/courses/RUN` page: `config.course("RUN")` stays `None`, so it is a 404. That is why the RUN course chip and course code must not be links.

**Files:**
- Modify: `argus/web/viewmodels.py` (`runna_ical` entries in the three Health maps at `:411` / `:420` / `:429`)
- Modify: `argus/web/templates/pages/item.html`:
  - `:7`: `course_chip(v.course, None if v.course.code == "RUN" else "/courses/" ~ v.course.code)`;
  - `:57`: the "Re-run AI estimate" form also needs `v.course.code != "RUN"`.
- Modify: `argus/web/templates/pages/search.html:35` (`course_code(course, None if course.code == "RUN" else "/courses/" ~ course.code)`)
- Modify: `argus/vault/service.py` (`_line` passes `note=None` for a `RUN` item, so `render._item_line` writes the title as plain text with no `[[link]]`)
- Modify: `argus/demo.py` (only what the tests need)
- Test: `tests/integration/test_capabilities_web.py`, `tests/integration/test_web.py`, `tests/integration/test_vault_capabilities.py`

**Interfaces:**
- Produces, in the three Health maps in `viewmodels.py`:
  - label `"Runna"`;
  - setup hint `"Set ARGUS_RUNNA_ICAL_URL and add a runna: block (Runna > Profile > Connected Apps & Devices > Calendars > Other Calendar)."`;
  - stale after `timedelta(minutes=90)`, the same as `canvas_ical`.
- Demo, full profile only:
  - add `runna: {}` to its config, a synthetic `runna_ical_url` secret, and one `runna_ical` health row so Health renders the source;
  - add **one** run, `DEMO_RUN_TITLE = "Easy run"`, due tonight, with a 45-minute `runna` estimate, for the item-page and search tests.

  `DemoProfile.NO_CANVAS_TOKEN` drops the secret.
- Vault: the Today note's work-window split is already plain text (`"{title}, {hours}"` strings in `_today`), so only the due lines in the Today and weekly notes change.

- [ ] **Step 1: Write the failing tests**

```python
def test_health_lists_runna(client_full):
    assert "Runna" in client_full.get("/health").text

def test_run_item_page_has_no_course_link_or_rerun(client_full):
    html = client_full.get(f"/items/{DEMO_RUN_ID}").text
    assert 'href="/courses/RUN"' not in html
    assert "/estimate/rerun" not in html

def test_search_hit_for_a_run_has_no_course_link(client_full):
    html = client_full.get("/search", params={"q": DEMO_RUN_TITLE}).text
    assert DEMO_RUN_TITLE in html and 'href="/courses/RUN"' not in html

def test_vault_run_lines_are_plain_text(vault_with_runs):            # Today and weekly notes
    today, weekly = vault_with_runs.read_today(), vault_with_runs.read_weekly()
    for note in (today, weekly):
        assert "Tempo run" in note and "[[" not in line_with(note, "Tempo run")

def test_no_runs_anywhere_without_runna(client_notoken):            # Review Focus 4
    for path in ("/", "/upcoming", "/workload"):
        assert "Running" not in client_notoken.get(path).text
    assert client_notoken.get("/courses/RUN").status_code == 404
```

- [ ] **Step 2:** Run `uv run pytest tests/integration/test_web.py tests/integration/test_capabilities_web.py tests/integration/test_vault_capabilities.py -k "runna or run" -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command, then `make test-e2e` and `make test-a11y` (the Health page is already covered). Expected: PASS.
- [ ] **Step 5: Commit:** `feat(runs): Runna on Health; no course links or notes for runs`

**Vault.** `VaultService.build` writes course and item notes only for `config.courses`, so runs get no notes. Runs still appear in the Today and weekly notes and in Today's work-window split, because those use every item. They appear as plain text, with no `[[link]]`, so clicking never makes Obsidian create an empty note.

### Task 7: Docs and the full check

**Files:**
- `docs/DECISIONS.md` (D078)
- `CLAUDE.md`:
  - source quirks: the Runna feed URL is the token; timed vs all-day; how completed activities look (Task 0 case a/b/c); the event types that are skipped;
  - the architecture map line for `runs.py` and `collectors/runna_ical.py`;
  - the data model note.
- `docs/runbook.md`
- `README.md` (connector fields)
- `docs/schema.md` (already done in Task 3; check it)

- [ ] **Step 1:** Write D078. Cover:
  - why: the morning Claude plans runs next to coursework;
  - due at the end of the day, the 16:00 preference, never MISSED, no AI, no calibration, estimate minutes for every item, hidden when not set up;
  - **completed activities:** the completed-activities setting, the Task 0 case observed, the in-fetch twin match, and the stored-run match for the replace case;
  - **why ended runs leave the connector:** a skipped run isn't overdue work to reschedule;
  - **the duration rules:** labeled total, paces removed, one distinct value or none, bare `m` and `mm:ss` not counted;
  - **which event types are skipped**, and why;
  - the web scope (Health only) and the vault scope (no notes);
  - any deviations recorded in Task 0.
- [ ] **Step 2:** In `docs/runbook.md`, add:
  - setting the `runna_ical_url` credential on the Pi, and the `runna:` block;
  - setting Runna's completed activities to "Completed plan workouts only";
  - **Don't double-book:** don't also subscribe the Google or Apple calendar that FlowSavvy reads to the Runna feed. If you do, a timed run shows up both as a busy event and as a task Claude schedules.
- [ ] **Step 3:** Run `make check`. Expected: green, with the deadline golden unchanged.
- [ ] **Step 4:** Run `make secrets` (`gitleaks git` over the full history). Expected: no findings. The `runna-ical-feed` rule from Task 1 is what makes "no Runna URL in history" something gitleaks can actually check. Without it, gitleaks has no pattern for the URL.
- [ ] **Step 5: Commit:** `docs: Runna runs as assignments (D078)`
