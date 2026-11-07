# Runna runs as assignments — design

Date: 2026-11-06 · Status: approved, revised 2026-11-06 · Decision number when built: D078

## Intent

The student trains with Runna, a running-plan app. Every morning a scheduled Claude task reads Argus through
the connector (D073) and plans the day. Right now it sees only coursework, so runs are planned somewhere else
or not at all. The student wants each Runna run to be **an assignment in Argus**, with **how long it takes**
taken from Runna. That way the morning Claude, and Argus's own queue, fit the run into the day next to the
homework. The connector is the surface that matters. The student has moved away from the web UI.

Runna has no public API. It does publish a private iCal subscription feed (Profile → Connected Apps &
Devices → Calendars → Other Calendar):
- It lists upcoming planned workouts.
- Depending on an in-app setting, it also lists completed activities: None, Completed plan workouts only, or
  All completed activities.
- Runna updates the feed whenever the plan changes.
- Workouts given a time in Runna are timed events. Workouts without a time are all-day events.

## What the student said

- "Treat the running as an assignment."
- "Runna should provide an estimate to how long the run is" — the run's length comes from Runna.
- "The run should just be done by EOD."
- "I prefer it around 4pm, but it doesn't have to be at that time — whatever works."
- Later: the web UI is out of scope beyond the Health source line; no vault notes for runs; rescheduling
  needs no extra support; runs keep the normal deadline alerts.

## Goals

1. Each planned Runna run becomes an Argus item: kind `assignment`, in a Running pseudo-course, due
   **23:59 local on the workout's day**. It is due at the end of the day even if a time was set in Runna.
2. The run's estimate is Runna's duration. It is not an AI estimate, and no AI call is made for runs.
3. Runs take part in Argus as assignments do: queue, workload, reminders (`due_24h`, `due_3h`), status, and
   `get_upcoming_work` / `get_changes`. On the web they appear only through the existing assignment rows,
   with no run-specific UI.
4. The morning Claude gets each run with its length and a **preferred start** (around 16:00, or the time
   set in Runna), so it can schedule the run without guessing.
5. Moving or deleting a workout in Runna moves or removes the item. Finishing it marks it done, never removed.

## Non-goals

- Writing anything back to Runna. The feed is read-only and there is no API.
- Strava, Garmin, or any other fitness source.
- Pace, distance, or heart-rate analytics.
- A general "add any iCal feed" feature. A second feed would be a new decision.
- Run-specific web UI: no Running course page, no item-page lines, no palette entries.
- Vault notes for runs.
- Non-run Runna events (rest days, strength and mobility sessions, notes), unless Task 0 of the plan gives
  a reason to keep one.

## Anti-goals (would be a failure even if it works)

- A run marked **MISSED**, or a "missed" text. Like Canvas iCal without the API (D056), runs are never MISSED.
- A finished run recorded as **removed**. That happens if Runna replaces the planned event with its
  completed activity and Argus doesn't recognize the replacement.
- **Ended runs left in the connector.** A run whose day is over and that isn't marked done must not sit in
  `get_upcoming_work` as overdue work for Claude to reschedule.
- A wrong length from the workout text: a warm-up, rep, or pace taken as the run's total.
- The Runna feed URL in the chat, git, logs, the vault, or the connector output. **The URL path is the
  token**, and the repo is public. It is a secret, it goes through the redactor, and a gitleaks rule
  catches it.
- Runs crowding out deadlines. Runs use the normal tiers and slack, with no special boost.
- Anything about runs showing when Runna isn't set up (hidden, not empty — D055).
- Double-booking: the same timed run appearing both as a busy calendar event (if the student also
  subscribes FlowSavvy's calendar to the feed) and as a task Claude schedules. The runbook warns against
  that subscription.

## How it works (decided)

### Configuration
- New secret `runna_ical_url` (env `ARGUS_RUNNA_ICAL_URL` or a credential file). It is registered with the
  redactor like `canvas_ical_url`. The student sets it; it is never pasted into the chat.
- New `runna-ical-feed` rule in `.gitleaks.toml`, modeled on `canvas-ical-feed`, for the URL shape.
- New optional `runna:` block in `config.yaml`:
  - `preferred_time: "16:00"` (local) — used when a workout has no time in Runna;
  - `default_minutes: 45` — used only when Runna gives no usable duration.

  There is no accent: nothing would use it.
- The Running course is built from this block, not listed under `courses:`. It has code `RUN` and
  display name "Running", and is synced only while Runna is set up. `config.course("RUN")` stays `None`,
  so there is no course page.

### Collector (`collectors/runna_ical.py`, `Source.RUNNA_ICAL`)
- Fetch with ETag/Last-Modified and skip unchanged payloads, like `canvas_ical.py`.
- Only **runs** count. Rest days, strength and mobility sessions, and notes are skipped. Race day is a run.
- **Planned runs** become `SourceItem`s:
  - `external_id` from the event UID;
  - `title` from SUMMARY and `description` from DESCRIPTION;
  - `kind=ASSIGNMENT`, `course_code=RUN`, `all_day=True`, `due_at` = 23:59 local on the event's local date.
- **Duration**, in this order:
  1. DTEND − DTSTART on a timed event.
  2. A duration in the event's text, parsed strictly:
     - a labeled total (Runna's label, e.g. "Estimated duration") wins;
     - paces (`5:30/km`, `8:45 min/mi`, `6:30-7:00/km`) are never durations;
     - a bare `45m` (ambiguous with metres) counts only inside a labeled total ("Duration: 45m");
     - `mm:ss` doesn't count;
     - `h:mm:ss`, `X min`, and `Xh Ym` do count;
     - with no labeled total, more than one distinct duration in the text gives no duration.
  3. `default_minutes`, with low confidence.
- **Preferred start**: the event's own start time if it is timed, else `preferred_time` on that day.
- **Completed activities**: the student sets Runna to "Completed plan workouts only". A completed
  activity never becomes an item of its own.
  - If its planned twin is in the same fetch, the twin is marked SUBMITTED.
  - If it has no twin, because Runna replaced the planned event, the runner matches it against stored runs.
    It tries these in order, and the first that finds exactly one run wins:
    1. the same UID;
    2. the same local day and normalized title;
    3. the only not-yet-done run that day. This is safe because only plan workouts are synced, and it covers
       a renamed activity such as "Morning Run".

    The matched run is kept present with SUBMITTED, so it is never marked removed.
  - A completed activity that matches nothing is dropped.
- The feed's recent history defines `complete_scopes` for `RUN`, so a workout deleted in Runna is removed
  from Argus, as with Canvas iCal.

### Estimates
- Runna's duration is written as an estimate with a new source `runna` (p50 = p80 = the duration). This
  needs the `estimates.source` CHECK widened, a migration that rebuilds the table as 0009 rebuilt `time_logs`.
- The student's own estimate (`user`) still wins. Runs are never sent to the AI and never calibrated.
  "Re-run AI estimate" refuses a run (404) and is hidden on a run's item page.
- When Runna changes a workout's length, a new estimate version is appended. Rows are never overwritten.

### Planner and reminders
- Runs go through the queue as ordinary work items: tier from risk, then slack, then due. The 16:00
  preference lives in the data and the connector, not in queue order. Runs are not busy time.
- Reminders are unchanged. A run is due 23:59 and takes a submission, so:
  - the 24-hour text lands around 23:59 the night before;
  - the 3-hour text lands around 20:59 the same day.

  Quiet hours apply. The deadline golden is unchanged.

### Connector (`feed.py`, `mcp_server.py`)
- `type: "run"` and `source: "runna"`.
- `estimate_minutes` on **every** item: the effective estimate, yours > Runna / AI.
- `preferred_start` on runs.
- **A run leaves `get_upcoming_work` once its day ends** (`due_at < now`), done or not. Coursework keeps its
  7-day overdue tail. A skipped run is not overdue work to reschedule.
- `INSTRUCTIONS` says runs are flexible within their day: aim for `preferred_start` if it is free, and don't
  reschedule a run after its day.
- The feed URL and the workout description are never sent.

### Web
- Health lists "Runna" as a source (label, setup hint, staleness).
- Runs otherwise render through the existing assignment rows, with the `"slate"` course-color fallback.
- The RUN course chip on the item page and the course code in search aren't links, because `/courses/RUN` is a 404.
- Nothing about runs renders when Runna isn't set up.

### Vault
- No course or item notes for runs: the vault writes those only for `config.courses`. Runs still appear
  in the Today and weekly notes and in Today's work-window split, because those use every item. They
  appear as plain text with no `[[link]]`, so Obsidian never creates an empty note from a click.

## Left to the builder

- Plan Task 0 reads the student's real feed, fetched from `$ARGUS_RUNNA_ICAL_URL` without echoing it, to learn:
  - the URL shape (for the gitleaks rule);
  - timed versus all-day runs, and the duration label and format;
  - the event types and how to tell them apart;
  - what happens to a planned event when the workout is completed: (a) a twin, (b) the same UID with changed
    fields, or (c) replaced under a new UID.

  Fixtures in `tests/fixtures/runna/` are **synthetic** and match that shape.
- Whether `RUNNA_ICAL` counts as `is_official`. It never merges with course items, so this only affects
  link and source reporting.
- Docs: D078 in `DECISIONS.md`, Runna quirks in `CLAUDE.md`, the migration in `docs/schema.md`, and the
  runbook entries (the secret, the completed-activities setting, the double-booking warning).
