# Decisions

One entry per non-obvious choice: context, decision, alternatives rejected. Newest last.

## D001 — The product is named Argus (2026-10-18)
**Context.** The build prompt calls the system "Course Hub". The owner named it Argus.
**Decision.** Use Argus everywhere: the package `argus/`, the CLI `argus` (so `hub sync` becomes
`argus sync`), the vault markers `%% argus:begin <id> %%` / `%% argus:end <id> %%`, the opt-in
frontmatter `argus-edit: allow`, the vault folder `Argus/` (with `Argus/Proposals.md` and
`Argus/Inbox/`), and the git identity `Argus <argus@pi.local>`.
**Rejected.** Keeping `hub` names internally. Two names for one thing would confuse future sessions.

## D002 — One toolchain: Python Playwright, axe-playwright-python, Pillow diffs (2026-10-18)
**Context.** The prompt asks for Playwright e2e, axe-core accessibility checks, and visual
regression, and says Node must not be needed at runtime.
**Decision.** Use pytest-playwright (Python), `axe-playwright-python` (it bundles axe-core),
and a small Pillow comparator (`tests/visual/snapshot.py`) instead of `@playwright/test`'s
`toHaveScreenshot`. All test layers then run under one `pytest`, with one coverage and fixture system.
**Rejected.** The Node Playwright test runner. It would add a second package manager and lockfile.

## D003 — Per-OS visual baselines; Linux baselines come from a pinned container (2026-10-18)
**Context.** macOS and Linux rasterize fonts differently, so one baseline set cannot pass on both.
**Decision.** Baselines live in `tests/visual/baselines/{darwin,linux}/`. Linux baselines are
recorded in `mcr.microsoft.com/playwright/python:v1.63.0-noble` (`make visual-linux`), and CI
runs inside the same image on both x64 and arm64. Playwright is pinned to `==1.63.0` to match
the newest published image. Comparison allows 0.2% of pixels to differ by more than 24/255
in a channel, which absorbs antialiasing noise.
**Rejected.** Linux-only baselines, which would mean no local visual check on the dev Mac.
A loose tolerance, which would hide real regressions.

## D004 — Secrets: `ARGUS_*` env vars, else systemd credential files (2026-10-18)
**Context.** Secrets must never be in git, logs, or the vault.
**Decision.** `argus/config.py:load_secrets` reads `ARGUS_<NAME>` env vars, plus files named
`argus_<name>` in `$CREDENTIALS_DIRECTORY` (systemd `LoadCredential=`). Env vars win.
`config.yaml` holds no secrets. Every loaded secret is registered with the log redactor at startup.
**Rejected.** pydantic-settings, whose implicit `os.environ` reads made tests non-hermetic.
A secrets file inside the repo directory.

## D005 — The wall clock is banned outside `clock.py`, mechanically (2026-10-18)
**Decision.** Ruff `TID251` bans `datetime.now/utcnow/today`, `date.today`, and `time.time`
everywhere except `argus/clock.py`. Components take a `Clock`.

## D006 — Pre-commit hooks run the project's own tools (2026-10-18)
**Decision.** The ruff and mypy hooks are `language: system` and run through `uv run`, so they
use the locked versions. gitleaks runs as the system binary (pinned to 8.30.1 in CI).
`make check` also runs `gitleaks git` over the full history.
**Rejected.** Upstream mirror hooks, which drift from the locked versions.

## D007 — Tailwind v4 standalone CLI, pinned and checksum-verified (2026-10-18)
**Decision.** `scripts/fetch_tailwind.sh` downloads Tailwind v4.3.3 for the host platform
(including linux-arm64 for the Pi) and verifies it against the release's `sha256sums.txt`.
The built CSS is not committed; `make css` runs before every browser test and on deploy.

## D008 — Merge links are sticky; auto-merge never fuses existing items (2026-10-18)
**Context.** The spec's rule is to merge a Gradescope item and a Canvas item when the course and normalized
title match and the due times are within 60 minutes. Dates move after items are created.
**Decision.** Once a source record is linked to an item, it stays linked even if its date moves away.
A disagreement becomes a flagged conflict instead of a split. Auto-merge only attaches *unlinked*
records. It never fuses two existing items, which could duplicate reminders or orphan time logs.
Canvas iCal and Canvas API records with the same assignment id always join. Fuzzy matching is
greedy by smallest gap with total-order tie-breaks, so it is order-independent. That property is tested.
**Rejected.** Re-clustering from scratch every cycle, which would let item ids churn.

## D009 — Earliest official due date wins; disagreements over 5 minutes are flagged (2026-10-18)
**Decision.** Among Canvas and Gradescope, the earliest due date drives the item, so a reminder is
never late. Manual dates rank below official ones, and dates found in posts rank lowest
(the non-negotiable). Any other date that differs by more than 5 minutes sets `date_conflict` until the student resolves it.

## D010 — Status semantics (2026-10-18)
**Decision.** Exams and events take no submission, so they become `submitted` ("done") once they start.
An assignment or quiz is `missed` from its effective deadline on: the late-due time if later,
otherwise the due time, and the due instant itself counts as too late. Unknown submission state counts as not
submitted, so deadline alerts err toward firing. Between the due time and the late-due time, the item stays
open (the UI shows it as overdue).

## D011 — Unofficial libraries sit behind adapters and run in threads (2026-10-18)
**Decision.** `gradescopeapi` and `piazza-api` are pinned exactly. Each is wrapped in a small adapter
(`LibGradescopeClient`, `LibPiazzaClient`) that converts library errors into `AuthError` or `FetchError`
("page format changed") and runs in a worker thread. Contract tests drive the real adapters through
`responses` against synthetic HTML and JSON. Pipeline tests inject fake clients.

## D012 — Canvas iCal lookback window protects old items (2026-10-18)
**Context.** The feed keeps only about 30 days of history. A complete fetch would otherwise mark older items absent.
**Decision.** `complete_scopes` carries a window start (now − 29 days). Records due before it are never
marked absent by that source.

## D013 — Piazza email fallback via the Gmail REST API with httpx (2026-10-18)
**Decision.** Gmail is called directly with httpx, refreshing an OAuth token from a JSON file, instead of using
`google-api-python-client`. That keeps it respx-testable and light on the Pi. It runs only when the
Piazza API is unconfigured or its latest run failed. Posts dedupe with the API by `(platform, nid:nr)`.

## D014 — Live smoke test skipped in Phase 1 (2026-10-18)
No credentials were available when Phase 1 finished, so `tests/integration/test_live_smoke.py`
(opt-in via `ARGUS_LIVE=1`) has not run against real ELMS, Gradescope, Piazza, or umd.io yet.
Run it once credentials exist. Its first run is also the check on the Piazza email format.

## D015 — Interim risk until the planner exists (2026-10-18)
**Decision.** `planner/risk.py:assess` implements the spec rule (at risk when the free hours before the due time are fewer than
the remaining p80) once the planner supplies those numbers (Phase 4). Until then, an open item past due is
overdue, a missed item is overdue, and an item due within 24 hours that hasn't been started is "watch". The signature is already final.

## D016 — Same-origin guard on every state-changing request (2026-10-18)
**Context.** The dashboard is reachable only over Tailscale, but a malicious page open in the same browser
could still POST to it (CSRF).
**Decision.** Middleware refuses any non-GET request whose `Origin` (or `Referer`) host differs from
`Host`. `/webhooks/*` is exempt, because Twilio signs its requests (Phase 6). This needs no tokens in forms
and works for HTMX and no-JS posts alike.

## D017 — Alpine's standard build; no CSP header yet (2026-10-18)
**Decision.** Alpine's standard build needs `unsafe-eval`, so no Content-Security-Policy is sent in Phase 2.
Revisit in Phase 6 hardening: either switch to `@alpinejs/csp`, or set a CSP allowing `'unsafe-eval'` for the
same origin only.

## D018 — Loading state = progress bar plus dimmed content during HTMX navigation (2026-10-18)
**Context.** Pages are server-rendered, and on the LAN a full render takes milliseconds. Skeleton-first loading
would add a round trip to every view.
**Decision.** First paint is complete HTML. While an HTMX navigation or refresh is in flight, `body.is-loading`
shows a top progress bar and dims `main`. The skeleton component exists for lazily loaded regions in later
views and appears on `/styleguide`.

## D019 — One hyperlegible typeface (2026-10-18)
**Decision.** Atkinson Hyperlegible Next instead of Inter. It serves both AA readability and the room-distance kiosk, and
gives Argus a voice that isn't the default dashboard look. See `docs/design.md`.

## D020 — Batched messages claim one key per item (2026-10-18)
**Context.** "New item" reminders are batched, but idempotency must be per item, rule, and due time.
**Decision.** A `notification_keys` table maps every covered key to its notification. A claim inserts the
notification and all of its keys atomically, or nothing. Rules skip keys already recorded, so later
batches never repeat an item. The first import is never reported as "new" (items created within 10 minutes of the first
successful sync are excluded).

## D021 — Today's "Past due" group (2026-10-18)
**Decision.** Today lists assignments and quizzes from the last 3 days that are past due and either still open
or `missed`, so the student can still mark them done. The due-today list excludes them.

## D022 — Browser tests use a fresh server per mutating test (2026-10-18)
**Decision.** Read-only suites (a11y, visual, navigation) share one seeded server on a frozen clock.
Tests that change data get `fresh_server()`, with its own database, so no test can disturb
another or the snapshots. All of them use the same synthetic seed as `argus serve --demo`.

## D023 — Search: one FTS5 table, rebuilt after each sync (2026-10-18)
**Decision.** `search_index` is rebuilt from items, modules, and posts at the end of every sync cycle,
inside the same transaction as the merge. It holds hundreds of rows, so a rebuild takes milliseconds and can't drift the way triggers can.
User text never reaches FTS syntax: each word is quoted, and the last becomes a prefix (`repo.fts_query`).
Snippets use control-character sentinels, are HTML-escaped, and only then get `<mark>`.

## D024 — `/` focuses search; ⌘K opens the palette (2026-10-18)
**Decision.** The spec gives `/` to search, so it focuses the header search box (on phones, it opens `/search`).
The command palette, which also lists items and courses, stays on ⌘K/Ctrl+K.

## D025 — Tier 2 is hidden, not empty, without a token (2026-10-18)
**Decision.** Without `ARGUS_CANVAS_TOKEN`, the Course view omits the grade and modules sections entirely, and nothing
mentions them. Health lists "ELMS (API)" and "ELMS modules" as not set up, with the one-line fix. Search still
covers items (iCal descriptions) and posts. Both configurations are tested (`test_web_course.py`).
