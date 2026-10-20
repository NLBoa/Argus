# Argus overhaul: plan (amended 2026-10-19)

## Status (updated 2026-10-19)

| Phase | State |
|---|---|
| Step 0: deadline golden | Done |
| 1: Capabilities, hiding what isn't set up | Done; deployed to the Pi 2026-10-19 |
| 2: Work windows, queue, risk, window message, exam study time | Done (`make check` green, reviewed, darwin and linux baselines); **waiting for the Pi run** |
| Settings page for work windows (planned for P3) | Done early, ships with P2. Notice lead time and the theme move are still P3 |
| 3: Today redesign, queue actions, SMS verbs, day strip, 0010 | Not started |
| 4: Visual system pass, workload chart | Not started |
| 5: Logo | Not started |

**Before Phase 3 starts:**
1. The user deploys P2: `git push origin overhaul`, then on the Pi (via Pi Connect, not SSH)
   `sudo /opt/argus-src/deploy/update-on-pi.sh overhaul`. Work windows are then set at `/settings`;
   `/etc/argus/config.yaml` no longer needs `schedule.work_windows`.
2. A day or two of real use: the window message arrives 20 min before a window, the confirm line shows the
   next morning, and Workload shows real window hours. 0010 must not ship until the Pi's `legacy` conversion has run.
3. The user runs `make evals` (plan_note.v2 changed; needs `ARGUS_ANTHROPIC_API_KEY`).
4. Decided 2026-10-20: keep the demand cap (D059) as is.

**Phase 3 kickoff: do this first, before any Phase 3 code (user's decision, 2026-10-19).**
The testing workflow gets lighter. Commit it as its own `chore:` commit:
1. In CLAUDE.md "Definition of done", replace item 2 with: "During a phase, run the tests the change touches
   (`make test-unit`, `make test-int`, targeted e2e). `make check` is green at the end of each phase." Replace
   item 3 with: "Any new page is in `tests/e2e/pages.py`. Visual baselines are darwin only, recorded once a
   phase's UI settles; linux baselines are recorded once, at the end of the overhaul."
2. Pause visual snapshots during P3 and P4: `make check` skips `test-visual` until P4 ends (a Makefile change,
   with a comment saying why and when it comes back). Resume them at the end of P4 with one darwin re-record.
3. One independent review per phase, at its end, not per step.
4. Keep unchanged: tests first for domain logic, the deadline golden, unit, integration, axe a11y, and coverage floors.
5. Update the "Workflow" section below to match.

## Context

Argus works, but it plans like a calendar robot and looks generated.
- `planner/scheduler.py` fills every free gap with sessions that can't be moved. The demo Monday is booked from 3:30 to 11 pm.
- "Free time" means waking hours minus classes, so /workload claims 167 h free over 14 days. Risk and workload are far too optimistic.
- The UI is badge soup, the same buttons repeat on every row, and banners eat the phone's first screen.
- Without the Canvas token, several surfaces are empty or misleading. One is serious: every iCal-only assignment turns **Missed** at its deadline.

This overhaul:
- replaces auto-scheduling with **work windows + a ranked queue** that the student controls;
- hides whatever a missing source can't support, app-wide;
- gives Argus a deliberate visual identity and a new mark.

There are **no new learning features this iteration**. Existing study features (Exam prep, practice, vault study notes, Anki export) keep working as they are and get only the P4 visual pass.

All CLAUDE.md non-negotiables hold. Deadline alerts (24 h, 3 h) keep their timing, keys and guards. A golden test recorded *before any change* proves this.

**Decided with you (2026-10-19)**
1. Risk counts competing work (cumulative).
2. **Progress: ask, don't assume.** No assumed credit. The confirm line plus SMS LOG; unanswered means no credit.
3. Flashcards stay in Anki (moot this iteration).
4. Config seeds work windows once; after that, Settings is the only source.
5. **Stop after each phase** so you can run it on the Pi. Order: Step 0 (golden), then Phase 1, then stop.

## 0. Baseline, tools, workflow

### "Before" screenshots
`/private/tmp/claude-501/-Users-noahleibovitz-Argus/0c7a85d7-e712-4ba6-a096-ec162b6066f1/scratchpad/before/` holds `{today,upcoming,workload,exams,course,item}-{desktop,phone}-{light,dark}.png` (1440 and 390 wide) plus phone first-screen crops. Step 0 copies them to `docs/plans/overhaul/before/` (git-ignored).

What they show:
- Today (phone): banners and the date fill the first screen.
- Today (desktop): the rail starts at 8 am with empty hours and four dashed sessions. Homework 3 (11:30 pm) is missing from the rail.
- Rows: chip+dot, kind, "3.9 h estimated 3.5 h", source links, "Watch" on everything, stripe, Start and Mark done.
- Workload: "20.5 h planned of 167.4 h free".
- MATH240 course page: "No modules yet" even with a token.
- Item page: Re-run AI shows even without AI.

### Rail root cause (confirmed)
- `.rail-block` (`app.css:291`) is absolute and full width with no lanes.
- `min-height: 1.25rem` is taller than short blocks.
- `.rail-due` and `.rail-now__label` both sit at `right: .25rem` above blocks.
- `viewmodels.timeline` (`viewmodels.py:227`) spans only the waking window, so 11:30 and 11:59 pm deadlines are dropped.
- Hour labels assume uniform hours, so DST days are off by one.

### Other bugs fixed along the way
| Bug | Phase |
|---|---|
| Missing `viewport-fit=cover`, so safe-area padding is 0 on iOS | P4 |
| `max_session_minutes` is unused | P2: deprecated |
| Starting a timer drops D030 credit | P2: model replaced |
| Finding buttons are HTMX-only | P1 |
| Practice buttons are HTMX-only | P3 |
| Estimate and log forms have no HTMX branch | P3 |
| Duplicated dark tokens and a stray `--overdue-ink` | P4 |
| `modules.digest` is read but never written | P1 |
| Search promises "module files" that aren't indexed | P1: copy |
| No layout-shift test | P3 |

### Tools in use
- Explore agents (audits, done) and a Plan agent (its 14 findings are folded in).
- The `frontend-design` and `dataviz` skills in P3–P5 (`validate_palette.js` for status and window colors).
- The repo's Playwright: screenshots, e2e, axe, visual, and the new no-JS and layout-shift tests.
- `superpowers:test-driven-development`, plus the `audit-suite` a11y and design lenses on the P3 and P4 designs.
- For each phase's independent review: a fresh `general-purpose` subagent plus the `code-review` skill at `high`.

**Not installed. Install if you want them:**
- `fonttools` (`uv add --dev fonttools`), to subset the display face. **Needed in P4.**
- A Playwright MCP server: optional, since the repo's Playwright covers everything.
- There's no code-reviewer agent type; a general-purpose agent stands in.

### Workflow
- **Branch.** One branch, `overhaul`, with one conventional commit per green step. The first commit adds this plan as `docs/plans/overhaul.md`.
- **During a phase (from P3):** run only the tests a change touches (`make test-unit`, `make test-int`, targeted e2e).
  Tests first for domain logic, the deadline golden, axe and coverage floors are unchanged.
- **End of each phase:**
  1. `make check` green, plus the deadline golden. Visual snapshots are out of `make check` during P3 and P4;
     they come back at the end of P4 with one darwin re-record.
  2. Darwin baselines only, once the phase's UI settles. Linux baselines once, at the end of the overhaul.
  3. One independent review of the whole phase, fixes, then `make check` again.
  4. A summary with before/after screenshots and the **Pi deploy checklist**.
  5. **Stop for your Pi run.**
- **Pi deploy checklist.** It goes in `docs/runbook.md` and is repeated in each phase summary:
  1. On the Pi: `argus backup`.
  2. Then `argus check-config -c /etc/argus/config.yaml`.
  3. Then deploy, and let the services migrate.
  4. `make logs`, and the phase's hand checks.
- **Docs.** Each phase updates CLAUDE.md (summary, architecture map, data model, idempotency key formats, UI rules), `docs/schema.md`, and the superseded DECISIONS entries in place ("Superseded by D0xx").

### Step 0: golden deadline record (first commit after the plan)
- **File:** `tests/integration/test_deadline_golden.py`.
- **What it records:** it runs the current simulated week and writes the DUE_24H and DUE_3H `(key, sent_at)` list to `tests/fixtures/sim/deadline_alerts.json`.
- **Commit:** `test: record deadline alerts golden`.
- **Rule:** every later phase must reproduce it exactly.

---

## Phase 1: Capabilities and hiding what doesn't work

### Design
- **New pure `argus/capabilities.py`.**
  - `derive(config, secrets) -> Capabilities` reuses each collector's `enabled()` (moved from `web/data.py:50`).
  - `Capabilities` (frozen) holds:
    - `sources`;
    - per-course `CourseCaps(canvas_api, modules, gradescope, piazza)`, each = source enabled ∧ the course has its id;
    - `features: frozenset[Feature]`;
    - `tracking_sources`.
  - `FEATURES` is the single feature → requirement matrix, pinned by a test to the table below.
  - `with_budget(spent, cap)` adds `ai_calls_left`. The budget hides only new-call affordances.
- **Threading.**
  - Computed in `create_app` and held on `AppState`.
  - `render()` injects `caps`, and a new `common.fragment()` does the same for HTMX fragments.
  - The kiosk, the vault `Builder`, the reminder `Snapshot`, SMS `Inbox` and the worker receive it.
  - `data.tier2` and `configured(state)` are deleted.
- **Templates.**
  - Capability-dependent markup sits in `{% if caps.has(...) %}` and carries `data-requires="…"`. This becomes a CLAUDE.md UI rule.
  - The nav is a feature-filtered list.
  - Copy that names sources is built from the configured ones.
- **Stored data from a disabled source is hidden** in loaders, search and the vault builder. Existing vault module notes are left untouched.
- **Submission tracking** (domain change, tests first).
  - An item is tracked if it has a present `item_sources` row from `tracking_sources` (the enabled subset of {canvas_api, gradescope}). This is derived in SQL.
  - `refresh_statuses(conn, now, tracking_sources=…)` takes the set as a parameter, so the store stays config-free. All six callers pass it.
  - No new status value. That would need an `items` rebuild, which has many foreign keys.
  - An untracked item past its late-aware deadline stays open:
    - `awaiting_confirmation(item, now, tracked)` flags it, and `risk.py` returns `RiskLevel.UNCONFIRMED`, not OVERDUE.
    - Today lists it under "Did you submit?" for 7 days. After that it appears only on the course page ("Not marked done"), and the vault Status line says the same.
    - The queue, risk sums and reminders exclude it.
  - The 24 h and 3 h warnings keep identical timing, keys and guards. The only change is the body for untracked items: "Reply DONE 1 once submitted."
  - The first refresh reopens old iCal-only MISSED items. The 7-day cutoff keeps Today clean.
- **Demo profiles.**
  - `DemoProfile` (`full`, `no_canvas_token`) via `argus serve --demo [--no-canvas-token]`.
  - The no-token seed omits modules, grade, Canvas announcements, materials and canvas_api submitted flags.
  - Demo fixes:
    - Add Piazza credentials and network ids.
    - Add a `DemoTransport`: it reports AI as available but always falls back offline.
    - Record canvas_api and modules sync runs.

### Capability matrix (audited; ✱ = correction to the brief)
| Feature | Data | Source(s) | Without it |
|---|---|---|---|
| Modules (course page, vault course note, module notes) | `modules` | canvas_modules, per course | Hidden; module notes not updated |
| Grade | `courses.grade_*` | canvas_modules, per course | Panel and vault line hidden |
| Syllabus ✱ | `posts.kind=syllabus` | canvas_modules | Not on the course page today; removed from search and Announcements |
| Canvas announcements | `posts.platform=canvas` | canvas_api | Not listed; "From the instructors" shows Piazza only, or is hidden |
| Announcements page and nav | `posts`, `findings` | canvas_api ∨ piazza ∨ piazza_email | Nav hidden; route redirects to Today |
| Submission state / auto-done | `item_sources.submitted` | canvas_api ∨ gradescope, per item | No MISSED; "Did you submit?"; 3 h body |
| Late due / release / points ✱ | item fields | canvas_api ∨ gradescope | Not shown |
| Study material generation | `materials` | canvas_modules ∧ AI | No generation; exam pages show the exam and study time |
| Exam scope ✱ | concepts + findings | (modules ∧ AI) for concepts; AI for scope text | Empty section hidden |
| Search: modules / materials ✱ | `search_index` | canvas_modules | Module hits filtered; copy lists only configured sources |
| Health | all | – | The only "not set up" mention, with its fix (D025) |
| Kiosk ✱ | items, meetings | – | No token dependency; notices filtered |
| Command palette ✱ | `/palette.json` | – | Hidden pages drop out |
| SMS ✱ | – | – | No module or grade commands; HELP drops OK/NO without posts; countdown drops "Practice on Exam prep" without study |
| Vault | modules, grade, study | as above | Those sections and study artifacts omitted |
| Re-run AI estimate, "Not analyzed yet" | AI | key ∧ budget | Button hidden; posts listed plainly |
| "Open in" links | config ids | per course | Already per course |

### Files
- **New:** `argus/capabilities.py` (added to `scripts/coverage_floors.py` at 95%).
- **Planner and store:** `planner/risk.py`, `store/repo.py`.
- **Web:**
  - `web/{deps,app,data,study_data,viewmodels}.py`
  - `web/routes/{common,pages,actions,kiosk}.py`
- **Templates:** `base.html`, `pages/{course,item,search,announcements,exams,exam,health,upcoming,today}.html`, `components/findings.html` (real forms).
- **Vault:** `vault/{service,render}.py`.
- **Reminders and SMS:** `reminders/{rules,engine}.py`, `sms.py`.
- **Domain:** `domain/status.py`.
- **Demo and CLI:** `demo.py`, `cli.py`.

### Tests first
- **Unit:**
  - `test_capabilities.py`: every `enabled()` combination, per-course caps, matrix = table, budget.
  - `test_status.py`: untracked → open and awaiting confirmation; tracked → MISSED. A hypothesis property: never MISSED without a tracking source.
  - `test_risk.py`: UNCONFIRMED.
  - `test_rules.py`: 3 h and 24 h keys and timing are identical for tracked and untracked items; only the body differs.
- **Integration:**
  - `test_capabilities_web.py`, over `full` and `no_canvas_token` on every GET page. It asserts:
    - no `[data-requires~=modules|grade|canvas_announcements]` elements;
    - no `#module-` links;
    - no nav entries to hidden pages;
    - stored modules are not shown or searched.
  - Vault: no Modules or grade sections without a token.
- **e2e:**
  - `LiveServer(profile=…)`; `pages.py` entries gain `profiles=("full", "no_canvas_token")`.
  - a11y and visual `CASES` multiply by profile (desktop and mobile only).
  - Baselines named `…[-notoken].png`.
- **Golden:** the deadline record matches.

### Migrations
None.

### DECISIONS
- **D054** Capabilities.
- **D055** Hidden, not empty, app-wide (extends D025).
- **D056** No MISSED without a tracking source.

### Risks
- **Lingering open items.** Mitigated by the 7-day cutoff and Mark done.
- **The baseline count roughly doubles.** Mitigated: the kiosk is excluded from the profile dimension.

### Verify by hand (local, then on the Pi)
- Run `--demo` and `--demo --no-canvas-token` side by side. Nothing token-only should appear in pages, nav or the palette.
- Health shows "ELMS (API) · not set up · add ARGUS_CANVAS_TOKEN".
- Lab report 2 shows "Did you submit?".
- On the Pi: after the checklist, the real setup hides what its sources can't support.

---

## Phase 2: Work windows, queue, risk, window notice, exam study time (domain + planner)

### Model
- **Config.**
  - Shape: `schedule.work_windows: [{days: [Mon,Tue,Wed,Thu], start: "19:00", end: "23:00"}, {days: [Sun], start: "13:00", end: "18:00"}]`.
  - `days` are *start* days. `end <= start` wraps midnight (reuses `TimeWindow`).
  - At most 3 windows per day. Overlaps are merged with a `check-config` warning.
  - `notifications.window_notice_minutes: 20`.
  - `max_session_minutes` stays **accepted but deprecated**, because `_Strict` forbids extra keys and deployed configs set it. `check-config` warns.
  - `waking_window` now only sets the rail range.
- **Seeding (amendment 6).**
  - On the first run, config seeds the `work_windows` table.
  - `plan_state.windows_seeded_at` is set **only if config gave at least one window**. Seeding zero windows leaves it NULL, so a later config edit still seeds. Both paths are tested.
  - After that, Settings (P3) is the only source. Deleting every window there does not re-seed.
  - `check-config` notes that config windows only seed the first run.
  - No "config changed" flagging.
- **Window identity.** `window_id = "<local date>T<HH:MM>"` of the configured window. It's stable when classes or commitments change, and unique across DST.
- **Window intervals.** `planner/windows.py` (pure):
  - `window_intervals(start, end, windows, busy) -> list[WindowSpan(window_id, intervals)]`.
  - Built per local day with `local_datetime`. Classes (± walking buffer) and commitments are cut out; quiet hours don't cut windows.
  - `W(a, b)` = real elapsed hours in `[a, b)`.
  - Reuses `Interval`, `merge`, `subtract`, `busy_on`. `free_intervals` and `scheduler.py` are deleted.
- **Windows capability.** On when the table has rows: `Capabilities.with_windows(n)`, set at runtime like the budget.

### Formula (supersedes D015 and D031)
For an open item *i* that takes a submission, with due *dᵢ*:

| Term | Definition |
|---|---|
| **pᵢ** (progress) | `time_logs` hours, all real sources. A running timer counts live (`repo.logged_hours` already does this). **No assumed or provisional credit.** |
| **Eᵢ** (remaining) | `max(0, p80ᵢ − pᵢ)` (calibrated p80 or the override). After a time-left mark (h at t): `max(0, h − progress since t)`. The 0.25 h floor is display/queue only; risk uses the unfloored Eᵢ, and **Eᵢ = 0 → OK**. |
| **Dᵢ** (competing demand) | `Eᵢ + Σ Eⱼ` over open *j ≠ i* with a due date, `dⱼ ≤ dᵢ`, not past their late-aware deadline, not awaiting confirmation. Overdue items still in a late window count; undated items never count. |
| **Cᵢ** (capacity) | `W(now, dᵢ) − RS(now, dᵢ) − SKIPᵢ` |

- **RS** is exam study time reserved in windows before dᵢ, prorated in the current window to `min(q_w, remaining part)`.
- **SKIPᵢ** is the unreserved remaining part of a "not tonight" window for *i*, if it falls before dᵢ.

**Levels**, in precedence order:
1. **Unconfirmed** (D056).
2. **Overdue:** `now ≥ dᵢ`, or MISSED.
3. **At risk:** `Eᵢ > 0 ∧ Cᵢ < Dᵢ`.
4. **Creeping:** `Eᵢ > 0 ∧ Cᵢ < 1.5·Dᵢ`.
5. OK.

Comparisons are strict.

**Edge cases**
- An undated item is OK and not in D. Exams and events get no marker.
- No window before the due date with Eᵢ > 0 is at risk ("No work window before it's due").
- Inside a window, only the rest counts. A due time inside a window counts up to the due time.
- DST uses real hours: a Sat 22:00–02:00 window across 2026-11-01 is 5 h, a spring-forward one is 3 h, and nonexistent local times move forward.
- Snoozed items count. Pins and reorder don't change risk.
- Late due: markers use `due_at`; MISSED and D inclusion use the late deadline.
- **No windows:** time-only fallback (overdue; due ≤ 24 h and not started → creeping), no notices, and an invitation on Today.
- **Untimed work inside a window** can raise the level mid-window. The guard: the **AT_RISK text for an item is deferred while now is inside a window allocated to it**. It may still go after the window ends (once per key), if nothing was logged or confirmed.

**Monotonicity.** With no new logs, *now* advancing:
- W only shrinks.
- A passing window removes `h` from W and at most `min(q_w, h)` from RS.
- Unmet quota spread over fewer windows raises RS.
- D is constant.

So Cᵢ never rises and the level never decreases.

### Progress (supersedes D030; ask, don't assume)
- **Why D030's nag loop can't recur.** Window notices are keyed per window, and at-risk alerts are keyed per (item, due). Both are once-per-key.
- **When the confirm line shows.** A window ended, its allocation named items, and no real log for those items overlaps the window or was created in the 24 h after it.
- **The confirm line.** Today shows one quiet line: "Last night: did you work on Homework 3? 2h · 1h · None".
  - An answer writes a `checkin` row (0 h for None), unique on `(item_id, window_id)`, so it's idempotent and never asks twice.
  - SMS `LOG n h` also clears the prompt.
  - **No answer means no credit.** The line disappears when the next window ends.
- **Migration from D030.** Before this change, passed planned sessions already counted as progress. A one-time worker step converts the passed *work* blocks of items with no real logs into `time_logs` with source `legacy`, so remaining hours don't jump at the switch.
  - Review blocks are not converted.
  - `legacy` is excluded from calibration via a new `repo.actual_hours` reader (`timer`, `sms`, `manual`, `checkin`).

### Queue (pure `planner/queue.py`)
- **Entries:** work items, plus one **"Study for <exam>"** card per exam with study time in the current or next window.
- **Total order key:** `(skipped_this_window, not pinned, pin_rank, tier, slack_min, due is None, due, kind_order, entry_id)`.
  - `slack_min = round((Cᵢ − Dᵢ) × 60)`, or +∞ for study cards.
  - Tiers: 0 overdue (late window open) · 1 at risk · 2 creeping · 3 exam study due this or the next window · 4 OK.
  - Skip sorts first, so "Not tonight" on a pinned item still moves it out of tonight.
- **User state** is stored in `queue_prefs(entry, item_id, pin_rank, pin_kind 'pin'|'move', pin_expires_window, skip_window_id)`.
  - **Pin** lasts until done or unpinned.
  - **Move up / Move down** pins the displayed prefix up to the new position as `move` pins. They expire when the current or next window ends.
  - A move sends `before=<neighbor>`. If that no longer matches, the move is refused and the list re-renders with "The list changed; try again".
  - An at-risk item below pins keeps its marker, and the notice still names it.
  - Study time is reserved wherever the card sits. The card says "Set aside each window".
- **"Not tonight"** skips the current window, or the next one starting within 24 h (otherwise the control is hidden). It expires at that window's end.
- **Time left** appends `remaining_marks(item_id, hours, at, source)`. EST keeps its meaning (the total).

### Window notice (replaces START_NUDGE)
- **The rule.** `Rule.WINDOW_PLAN` ("Work window plan", mutable).
  - `Rule.START_NUDGE` stays as a **legacy member** that nothing produces, so old `notifications` and `rule_mutes` rows still parse (`engine.py:263`, `actions.py:127`).
  - 0009 expires pending start_nudge rows, and Health hides legacy rules.
- **Timing and key.** It fires for `now ∈ [ws − lead, ws + 30 min)`, where `ws` is the nominal start. The key is `window:<window_id>`.
- **Guards.**
  - It obeys quiet hours, class time, mute and snooze (it waits only when every named item is snoozed).
  - **It doesn't count toward the daily cap** (≤ 3 windows a day, so it's bounded like AT_RISK). It therefore can't delay a DUE_24H.
  - Priority 4. Expires at `ws + 30 min`.
- **Snapshot.** `window_plans(window_id PK, starts_at, ends_at, allocation JSON)`.
  - The worker's plan step creates it **every tick inside the lead time**, not only on replans. Queue actions update it until the window ends.
  - The reminders step computes it on the fly if it's missing.
- **Text.** The message is built entry by entry and stops before it would exceed one GSM-7 segment (`[`, `]` and `~` count as 2 septets). It is never truncated mid-entry, so `item_ids` match the numbers in the text.
  - Example: `Tonight 7-11pm: [1] Homework 3, 4h left, due Thu. [2] PS6, 3h left, due tomorrow (at risk). Study 45m: Midterm 1.`
  - The day word follows the start time ("Tonight", "This afternoon", "This morning", plus the weekday when it isn't today).
  - Study entries are never numbered, so `DONE n` can't hit an exam.
  - An empty allocation sends nothing.
- **Brief, TODAY and AT_RISK.**
  - The morning brief and `TODAY` say "Tonight 7-11pm: Homework 3 first."
  - AT_RISK keeps its rule and key, with a reason in the body ("only 3h of window time before Thu").
  - At-risk alerts that fire together are **batched into one message**, with per-item keys (like D020).

### Exam study time (supersedes D037 placement; amendment 2)
- **Card.** One "Study for <exam>" queue card per exam.
- **Hours.** `Qₑ = clamp(calibrated p80 of the exam, 1 h, 6 h)`.
- **Progress.** Only time logged on the exam item. The timer and LOG are allowed on exam items for this.
- **Spread.** Over windows starting in `[Tₑ − 7 d, Tₑ)` with capacity ≥ 30 min:
  - `q_w = ceil₁₅((Qₑ − progress) / n_remaining)`, at least 20 min;
  - **then** capped at 50% of the window's capacity (75% in the last 48 h);
  - so `q_w ≤ capacity` and `RS ≤ W`.
  - It is reserved first in each window's allocation.
- **Shortfall.** "Study time short by 1.5h before Wed" on the exam card and in the countdown.
- **What stays as is.** `readiness` and `review_order` stay for the Exam prep page and the countdown's "Weakest:" names. They do not feed the queue.

### What happens to the old pieces
| Piece | Fate |
|---|---|
| `plan_blocks` | 0009 renames it to `plan_blocks_legacy`. The one-time worker step (with `Clock`) runs the `legacy` conversion and sets `plan_state.legacy_converted_at`. `0010_drop_plan_blocks_legacy.sql` (only the DROP) ships in P3, after the conversion has run on the Pi |
| `plan_state` | Kept: explanation, `model_since`, `legacy_converted_at`, `windows_seeded_at` |
| `scheduler.py`, `free_intervals`, `place_reviews`, `review_sessions` | Deleted |
| `readiness`, `review_order` | Kept (Exam prep only) |
| START_NUDGE | Legacy enum only |
| `SessionRef`, `Snapshot.sessions` | Replaced by `Snapshot.window` |

### Minimal UI glue to stay green (the redesign is P3)
- **Rail:** sessions dropped, windows shaded.
- **Item "Plan" panel:** C, D and the level.
- **Workload:** the existing chart, fed by window hours.
- **Exam page and vault exam note:** "Review plan" becomes the per-window study time.
- **`Review/Today.md`:** shows the window allocation.
- **Baselines:** re-recorded.

### Files
- **New:** `planner/{windows,queue,exam_time}.py`.
- **Rewritten:** `planner/risk.py`, `planning.py`.
- **Changed:**
  - `reminders/{rules,engine,guards}.py`
  - `config.py`, `config.example.yaml`, `tests/fixtures/config.yaml`
  - `worker.py`, `sms.py`, `store/repo.py`
  - `web/{data,study_data}.py`, `web/routes/actions.py` (exam timer)
  - `vault/{service,render}.py`, `planner/review.py` (trimmed)
  - `demo.py` (windows Mon–Thu 19–23, Sun 13–18)
- **Deleted:** `planner/scheduler.py`.
- **Prompt:** `ai/prompts/plan_note.v2.md`. Run `make evals`.

### Tests first
- **`unit/test_windows.py`:** parsing, start days, wrap, merge, cuts, `window_id` stability, DST (Oct 31 → Nov 1 2026, Mar 14 2027), `W` additivity.
  - Also **the demo's 14-day window hours = 38.0** from `DEMO_CONFIG` at `DEFAULT_NOW`: 8 weekday windows × 4 h + 2 Sundays × 5 h − 4 h of Tue/Thu dining-shift overlap.
- **`unit/test_risk.py`** (rewrite), table-driven:
  - the C = D and C = 1.5·D boundaries;
  - Eᵢ = 0 → OK with no window left (Lab E);
  - an open timer counted live;
  - untracked items and late-window inclusion in D;
  - the no-windows fallback;
  - AT_RISK deferral inside an allocated window.
- **`unit/test_queue.py`:** tiers, slack rounding, skip over pin, move pins and their expiry, refused stale moves, None due dates, ties by id.
- **`unit/test_exam_time.py`:**
  - the clamp, then spreading with the minimum first and the cap second;
  - fragments under 30 min skipped;
  - progress from exam logs only;
  - shortfall.
- **`unit/test_window_notice.py`:**
  - the firing window, one key per window;
  - quiet/class deferral and the expiry drop;
  - snooze, and not capped;
  - fit-by-entry ≤ 160 septets with `item_ids` matching;
  - study entries not numbered;
  - the day word;
  - an empty allocation sends nothing.
- **`unit/test_progress.py`:**
  - the confirm line appears only without overlapping or subsequent logs;
  - a checkin is idempotent, and "None" writes 0 h;
  - an SMS LOG clears the prompt;
  - no answer means no credit;
  - calibration excludes `legacy`.
- **Hypothesis properties:**
  - the queue order is deterministic and total;
  - the level never decreases with no new logs;
  - at risk ⇒ creeping-or-worse, and `C ∈ [D, 1.5·D)` ⇒ exactly creeping;
  - `0 ≤ Cᵢ ≤ W(now, dᵢ)`, Σ allocation ≤ window capacity, `RS ≤ W`;
  - **with logs matching the allocation, no item is named in more windows than its estimate needs.** In the queue model, that's `⌈Eᵢ / allocation per window⌉` windows.
- **FakeClock:**
  - DST 2026-11-01: the 12:40 notice before and after, with distinct ids;
  - a midnight-crossing window notifies once;
  - a 07:30 window with quiet hours until 08:00: the notice defers, then is dropped;
  - the same window sends one message, and a moved due date re-arms only the due rules.
- **Integration:**
  - **The golden deadline record matches exactly** with windows off *and* on (the second run seeds windows and asserts no DUE_24H was ever deferred for the daily cap).
  - **`test_simulated_week.py`**, two student variants:
    - *Logs as allocated:* the windows-needed property holds.
    - *Never logs:* at most one notice per window; at-risk alerts ≤ 1 per (item, due), batched; the cap and quiet hours hold; no AT_RISK text inside an allocated window.
  - **Migration test:** the 0009 rebuild recreates `time_logs_one_running` and `time_logs_item`, and the legacy conversion runs once.

### Migration `0009_windows_queue.sql` (pure SQL inside `BEGIN IMMEDIATE`)
- **New tables:**
  - `work_windows(id, day, start, end, created_at)`
  - `queue_prefs(…)`
  - `remaining_marks(id, item_id FK, hours, at, source)`
  - `window_plans(…)`
- **`time_logs` rebuild** (copy, drop, rename; nothing references it):
  - the source CHECK adds `checkin` and `legacy`;
  - adds a `window_id` column, with a partial unique index `(item_id, window_id) WHERE source='checkin'`;
  - both existing indexes are recreated.
- **`plan_blocks` → `plan_blocks_legacy`**, by rename.
- **`plan_state` gains** `model_since`, `legacy_converted_at` and `windows_seeded_at`.
- **Pending `start_nudge` notifications expire.**

### DECISIONS
- **D057** Work windows: seeded once, Settings owns them, stable ids.
- **D058** Queue ranking and user control (supersedes D031).
- **D059** Creeping and at risk on window hours (supersedes D015).
- **D060** Progress: ask, don't assume; D030 conversion to `legacy` (supersedes D030).
- **D061** The window notice replaces start nudges; uncapped and bounded; at-risk batching and deferral.
- **D062** Exam study time (supersedes D037 placement).

### Risks
- **Never-logging inflates risk.** Mitigated: the confirm line is one tap, at-risk texts are once per key and deferred while you're in the window, and creeping is a quiet marker only.
- **Cumulative demand flags more items.** Mitigated: at-risk texts are batched.
- **About 15 tests depend on the scheduler.** The golden record guards deadline alerts.

### Verify by hand
- **Demo:** Workload shows **38 h** of window time (asserted by the test). Homework 3's item panel shows C, D and the level. `argus notify-test` shows the notice wording.
- **Pi checklist addition for this phase:** add `schedule.work_windows` to `/etc/argus/config.yaml` **before deploying**.
- **Pi:** after the checklist, the first window notice arrives 20 min before your window, and the next morning shows the confirm line.

---

## Phase 3: Today redesign, queue actions, SMS, rail

### Today ("what do I do now")
**Phone order:**
1. Status line (if any).
2. **Hero next task:** title, course, due and time left, "~4 h left", one reason line ("6 h of window time before Thu"), and **Start** in brand. While a timer runs, the hero is the timer (elapsed time and Stop).
3. The confirm line, if any.
4. **The queue**, an `<ol>` (order is the content), including study cards.
5. "Did you submit?" items, plus today's and tomorrow's exams and events that aren't queued.
6. The day strip.

**Desktop:** hero and queue in the main column; the day strip and "due soon" in the aside.

**Empty mornings:** the strip's range is `[min(first class, first window, now) − 30 min, max(last window end, last due) + 30 min]`, clipped to the day.

### Queue actions
- **Controls on each card:** **Move up / Move down** (aria-labels name the item), **Pin**, **Not tonight**, **Done**, and an inline **time left** edit.
- **Visibility:**
  - Shown on the hero and on row hover or focus-within. Always in the DOM, so nothing shifts and the keyboard can reach them.
  - With `(hover: none)`, a 24 px+ overflow button opens a `<details>` menu that works without JS.
- **Routes:** `POST /queue/{entry}/move` (`dir`, `before`), `/queue/{entry}/pin`, `/queue/{entry}/skip`, `/items/{id}/left`, and `/windows/{window_id}/confirm` (hours).
  - **HTMX:** returns the `#queue` fragment plus an `HX-Trigger` toast ("Moved Homework 3 to 2 of 5", which is also the live-region text). Focus stays on the same control; at an edge it goes to the opposite one.
  - **No JS:** 303 to `/today#q-{entry}`.
- **Bug fixes in the same pass:**
  - The estimate and log forms gain their HTMX branch.
  - **Existing practice buttons get real forms** (a no-JS path).

### SMS
New verbs in `reminders/commands.py`. Each needs a number, so chatter falls through to the AI.

| Command | Meaning |
|---|---|
| `SKIP n` | Not tonight |
| `FIRST n` | Pin to the top |
| `LEFT n h` | Time left (same amount grammar as EST, ≤ 200) |

`[n]` keeps resolving against the latest numbered text. HELP stays in one segment. `ai/prompts/sms.v2.md` adds the verbs; run `make evals`.

### Rail decision (recommended)
- **Today:** a compact horizontal **day strip**. It's read-only and shows classes, shaded work windows, the now needle, and due ticks in their own lane. Labels sit outside the bars.
- **Kiosk:** keeps the vertical rail.
- **Shared layout:** both use the pure `planner/lanes.py: assign_lanes` (greedy interval-graph coloring).
  - Overlaps become columns (kiosk) or rows (strip).
  - Short blocks get a single-line style.
  - The range includes late deadlines.
  - Hour labels use real local hours, so DST days are right.

### Settings page (new; added to `pages.py`)
- Work windows: no-JS add and remove forms. This is the only source after seeding.
- Notice lead time.
- Theme, moved here and into the palette.

### Files
- **Templates:** `pages/today.html` (rewritten), new `pages/settings.html`, `components/{queue,strip,practice,timeline}.html`.
- **Routes:** `routes/{actions,pages}.py`.
- **Web:** `web/data.py` (`load_today`, `load_settings`), `viewmodels.py` (`QueueCardView`, `StripView`), `static/src/app.js`.
- **SMS:** `reminders/commands.py`, `sms.py`.
- **Planner:** `planner/lanes.py`.
- **Migration:** `0010_drop_plan_blocks_legacy.sql`.
- **e2e:** `tests/e2e/pages.py` adds settings and the Today states.

### Tests first
- **Unit:**
  - `test_lanes.py`, with properties: overlapping intervals never share a lane, and lanes = max overlap.
  - `test_commands.py`: SKIP, FIRST and LEFT, and chatter refused.
- **Integration:**
  - Every new route with and without `hx-request` (fragment + `HX-Trigger`, or 303), plus CSRF refusal.
  - The practice forms' no-JS path.
  - Webhook TestClient for SKIP, FIRST and LEFT: signature checked, a replay answered once.
- **E2E (`test_queue_flows.py`, each with JS and with `java_script_enabled=False`):**
  - Reorder, keyboard only.
  - Pin.
  - Not tonight.
  - Time left.
  - Mark done.
  - The confirm line.
  - The timer.
  - Existing practice self-grading without JS.
- **Layout shift (`test_layout_shift.py`):**
  - Records `layout-shift` entries (including `hadRecentInput`) around each swap.
  - Same-size swaps (done, timer, pin) must total < 0.001.
  - A move may change only the `#queue` rect.
- **Axe and visual:** Settings and the Today states (empty queue, running timer, confirm line, no windows, no token).

### Migration
`0010_drop_plan_blocks_legacy.sql`: only `DROP TABLE plan_blocks_legacy`.
- It ships after the P2 conversion has run on the Pi.
- **The guard lives inside `store/db.migrate()`.** Every process runs `migrate()` at startup (`web/app.py:84` and others), so the guard can't sit in the CLI alone.
  - If `plan_state.legacy_converted_at` is NULL, 0010 stays pending, is logged once, and the remaining migrations continue.
  - It never raises, so the services can't crash-loop.
  - Both paths are tested: pending, then applied after conversion.

### DECISIONS
- **D063** Day strip on Today; laned rail on the kiosk.
- **D064** Queue reorder semantics: move pins expire, stale moves are refused.

### Risks
- **Reorder can surprise.** Mitigated by visible "Pinned" and "For tonight" labels.
- **New Today states add axe and visual cases.**

### Verify by hand
- At phone width, in both profiles, reorder using only Tab and Enter.
- Mark "not tonight", then reload.
- Answer the confirm line.
- Send `SKIP 1` through `argus serve-webhook` as a signed test request (see the runbook).
- On the Pi: the checklist, then a real evening with the queue.

---

## Phase 4: Visual system pass + workload chart

### Direction
The `frontend-design` pass and a design review come before any CSS. Argus is the watchman: calm, nocturnal, precise. **Spend boldness in one place: the hero next task, set in the display face.** Everything else stays quiet.

**Color**
- Teal is reserved for:
  - now/next;
  - the primary action;
  - the active nav;
  - focus.
- Bronze stays the "now" needle. Links become ink with an underline.
- Course color is used **once**: a 3 px stripe on rows. The code is muted text. Chips appear only in item and course headers.
- Status markers are an icon plus a label. Colors are validated by `validate_palette.js` in both themes, plus the existing contrast test.
- Dark tokens are generated once by `scripts/gen_tokens.py`, checked in CI.

**Type**
- Atkinson Hyperlegible Next stays for body and data.
- One OFL display face (latin subset, woff2, wght axis) for titles, the hero, and big numbers.
- `/styleguide` shows three candidates on real content (the hero, a page title, the timer "42:25", the kiosk clock):
  - **Bricolage Grotesque** (recommended);
  - **Fraunces** at opsz 72 / SOFT 50 (a common "generated" look);
  - **Space Grotesk**.
- Instrument Serif is excluded: one weight and a low x-height, which reads poorly as kiosk numbers.
- Tabular lining figures are a selection criterion. The unchosen faces are removed.
- Scale is a perfect fourth: 12.5 / 14 / 16 / 21 / 28 / 37 / 50, and 96 on the kiosk.

**Rows**
- The title, then a muted line: course code, kind (only if not an assignment), "~4 h".
- The right side: the due date and relative time, plus a marker only for creeping, at risk, overdue, or "Did you submit?".
- Source links move to the item page.

**Header**
- Text-only nav. **Today · Upcoming · Exams** are primary.
- A "More" `<details>` holds Workload, Announcements (if available), Health, Settings, and Proposals (with a count badge).
- Search and Ctrl-K stay. The theme toggle leaves the header.
- Mobile tabs: Today, Upcoming, Exams, More. This supersedes D040.
- `viewport-fit=cover`. The bottom padding is tab bar + safe area + 1rem. An e2e test checks the last focusable element isn't covered at 390×844.

**Banners**
- A status line under the header: a dot plus "Gradescope 3h behind", or "1 note edit to review".
- Full banners only for real errors: a collector failing ≥ 3 times, a vault conflict or error, or a DB error.
- Offline gets a compact line.

**Microcopy:** a pass against the `docs/design.md` writing rules.

**Workload chart (dataviz)**
- The chart:
  - Cumulative step lines on one hours axis over 14 days: "Window time" (neutral) and "Work due" (brand), with exam study time included in "Work due".
  - The shortfall gap shaded at-risk, with a direct label and a texture for forced colors.
- Around it:
  - A one-sentence summary ("Next 7 days: 14h of work, 16h of window time").
  - The item list below.
- Size and interaction:
  - About 160 px tall.
  - A per-day hover tooltip (≈ 1 KB of vanilla JS).
  - A `<table>` toggle.

**Docs:** `docs/design.md` is rewritten.

### Files
- **CSS and fonts:** `static/src/{tokens,app}.css`, `static/fonts/`.
- **Scripts:** `scripts/vendor_assets.py` (fontsource pinned by sha512), new `scripts/gen_tokens.py`.
- **Templates:** `base.html`, `components/{ui,charts}.html`, every page template (Exam prep and practice get only this pass).

### Tests
- **Budgets:**
  - JS+CSS < 150 KB gzipped, including the new JS.
  - Display face ≤ 45 KB; preloaded fonts ≤ 90 KB.
  - A metric-matched `size-adjust` fallback, so the font swap causes no CLS.
- **Server time:** the median-time test is extended to `/workload`, `/exams`, `/courses/*` and `/settings`.
- **e2e:** tab-bar overlap, and the More keyboard path (Enter opens; Esc closes and returns focus).
- **Axe and visual:** both themes, both profiles.

### DECISIONS
- **D065** Color roles.
- **D066** The display face.
- **D067** Navigation and the status line (supersedes D040).

### Risks
- **Font weight on the Pi.** Subset, preloaded, `swap`.
- **Baseline churn.** One commit per area.

### Verify by hand
- Compare the before and after shots at 1440 and 390 in both themes. The first phone screen shows the hero task.
- On the Pi: the kiosk at room distance.

---

## Phase 5: Logo

### Directions
Three SVG macros in `components/brand.html` (`mono`, `size`). On `/styleguide`, each appears at 16, 28, 64 and 192 px, in light and dark, one-color and full-color. No gradients.
- **A. Monogram.** An "A" whose crossbar is a narrow eye slit, with a needle-thin bronze pupil. At 16 px the slit alone carries it.
- **B. Watch arc.** Seven small dots on an arc, one filled and larger for "now". At 16 px: three dots, one filled.
- **C. Wordmark.** "argus" in the chosen display face, with the "g" ear as a bronze needle tick. The favicon is "a" plus the tick.

Each is checked against existing marks (Eye of Providence, eye-in-triangle, the Lens and Watch app glyphs). The check is recorded in DECISIONS.

### After you pick
- `scripts/make_icons.py` renders the 192 and 512 icons (maskable) and `argus.svg` from the chosen macro via Playwright.
- The manifest and `<meta theme-color>` are updated, and the service-worker version is bumped.

### Tests
- The styleguide variants.
- `make_icons.py --check` in CI.
- Visual baselines.

### DECISIONS
- **D068** The mark.

### Verify by hand
- The favicon at 16 px in both themes.
- The installed PWA icon on the phone.

---

## Open questions (recommended default in bold)
1. **The estimate that drives risk and "~N h":** **remaining calibrated p80**. p50 appears only on the item page.
2. **Notice timing:** **a 20 min lead; expires 30 min after the start; outside the daily cap.**
3. **Labels:** **"Creeping", "At risk", "Overdue", "Did you submit?"**
4. **How long "Did you submit?" shows:** **7 days after the deadline.**
5. **Mobile tabs:** **Today · Upcoming · Exams · More.**
6. **Move pins:** **expire at the end of tonight's window.** An explicit Pin lasts until the item is done.

## End-to-end verification
1. **Every phase:** `make check` (lint, mypy --strict, gitleaks, coverage floors with the new `planner/*` and `argus/capabilities.py` at 95%, unit, integration, e2e, a11y, visual, budgets) plus the deadline golden.
2. **Linux baselines:** `scripts/visual_linux.sh --update`, then a clean re-run.
3. **Screenshots:** demo shots in both profiles go to `docs/plans/overhaul/after-pN/` and are compared with `before/`.
4. **Evals:** after a prompt version changes (plan_note.v2 in P2, sms.v2 in P3), run `make evals` (you run it; it needs `ARGUS_ANTHROPIC_API_KEY`).
5. **Independent review:** a fresh agent reviews the diff against the brief, CLAUDE.md and this plan. Then fixes, then `make check` again.
6. **Before continuing:** the Pi deploy checklist and **a stop for your Pi run**.
