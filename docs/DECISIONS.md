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
*Superseded by D059.*
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
**Resolved by D042.**

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
*Extended app-wide by D054 and D055.*
**Decision.** Without `ARGUS_CANVAS_TOKEN`, the Course view omits the grade and modules sections entirely, and nothing
mentions them. Health lists "ELMS (API)" and "ELMS modules" as not set up, with the one-line fix. Search still
covers items (iCal descriptions) and posts. Both configurations are tested (`test_web_course.py`).

## D026 — AI calls: our own JSON-schema validation, one retry, then the fallback (2026-10-18)
**Decision.** `ai/client.py` sends `output_config.format` with a strict JSON schema generated from the
Pydantic model, with constraints stripped and enforced by Pydantic afterwards. It validates, retries once, and then returns
`None`. Every caller has a non-AI path: extraction leaves posts "Not analyzed yet" (still shown),
estimates fall back to the median of actuals or a per-kind default, and the plan note falls back to the plain first fact.
Refusals and API errors go straight to the fallback. The SDK already retries transient errors.
For `claude-sonnet-5-5` and `claude-opus-5-5`, requests opt into server-side refusal fallbacks
(`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`). Haiku has none.
**Rejected.** `messages.parse`, because its error behavior on validation failure is less explicit for our retry rule.

## D027 — Models and effort (2026-10-18)
Extraction uses `claude-haiku-5-5` at effort `medium` (date resolution needs some care). Estimates use
`claude-sonnet-5-5` at `medium`. The plan note uses Haiku at `low`. The verifier (Phase 5) uses `claude-opus-5-5`. All are
configurable in `config.yaml`. Prices for cost accounting live in `ai/budget.py`. Unknown models are charged
at a conservative rate so spend is never under-counted.

## D028 — Confirming a date change sets a student override (2026-10-18)
**Context.** The non-negotiable is "official sources beat dates extracted from posts", and dates that change an official date are held.
**Decision.** The code never applies a post's date over an official one. When the student explicitly confirms
("Use announced date", or `OK n` in Phase 6), that is the student's decision, stored in `item_overrides`, and it wins.
A date-only confirmation keeps the official time of day. Dismissing keeps the official date. If Canvas later lists the
new date, the override and Canvas agree. If the sources' disagreement changes, the conflict reappears.

## D029 — Guards on AI findings (2026-10-18)
A finding is dropped unless its quote appears in the post (normalized). A date is kept only if the post contains
a date expression and the date falls in [posted − 7 days, posted + 200 days]. A date-only finding is stored as 23:59 local and
compared by local day. These guards make "never invent a date" a code property, not just a prompt request.

## D030 — Passed sessions count as progress unless the student logs time (2026-10-18)
*Superseded by D060.*
**Context.** The simulated week showed that with progress = time logged only, a student who never logs time got the
same work re-planned (and re-nudged) every hour, forever.
**Decision.** If an item has any time logs, they are the truth. Otherwise planned sessions that have already passed
count as done. The student corrects with `LOG n h` or `EST n h`. Calibration still uses logged actuals only.

## D031 — Plan quality rules (2026-10-18)
*Superseded by D058 and D059: there are no sessions.*
Sessions are 30 to 120 minutes on a 15-minute grid, followed by a 15-minute break. They are trimmed so no remainder under 30 minutes is left over.
The plan is earliest deadline first, with no daily cap yet. It replans when inputs change or hourly, and blocks that have started are never moved. Items that fit
alone but are squeezed out by earlier deadlines are shown as Watch. "At risk" stays exactly the spec rule.

## D032 — Reminder titles use local calendar days (2026-10-18)
The simulated week caught "Due tomorrow" on a reminder deferred past quiet hours for something due that evening.
Titles now compare local dates, not hours remaining.

## D033 — Eval gates are checked twice (2026-10-18)
`tests/evals/scoring.py` is pure and unit-tested in CI. Perfect answers pass every gate, and invented dates,
misses, auto-applied date changes, and wrong days each fail the right gate. So `make evals` (real API, a key needed)
measures the model, not a buggy scorer. The dataset holds 44 synthetic posts across announcements, relative dates (including ambiguous ones),
reschedules, cancellations, scope, logistics, multi-finding posts, noise, and student hearsay.

## D034 — Phase 4 eval gate pending an API key (2026-10-18)
The planner property tests and the simulated-week pipeline test pass in `make check`. The extraction eval gates
(`make evals`) need `ARGUS_ANTHROPIC_API_KEY`, which has not been provided yet. The harness, datasets, and scorer are
in place, and CI checks the scorer. Run `make evals` once a key exists and record the numbers in `docs/handoff.md`.
Prompt tuning (a new prompt version) follows from whatever the first run shows.

## D035 — Vault layout and the course folder note (2026-10-19)
Every course lives in `Courses/<code>/`. The course page is the folder note `Courses/<code>/<code>.md`, so
`argus-edit: allow` there opens the whole course folder to free edits. Under it: `Concepts/`, `Modules/`,
`Assignments/`, `Exams/`, `Flashcards.md`, and `Concept map.canvas`. Vault-wide notes: `Review/Today.md`,
`Weekly/YYYY-Www.md`, `Argus/Proposals.md`, and `Argus/Inbox/`. Wikilinks use full paths, so two courses can
have a concept with the same name. Assignment notes carry logistics only (due date, status, estimate, links,
scope); they never contain anything about how to solve the work.

## D036 — Files outside the region model are written once (2026-10-19)
The JSON Canvas concept map and the inbox README cannot hold `%% argus %%` markers. Argus writes them only when
they are missing or still byte-identical to what it last wrote (hash in `vault_regions`, region `file`). If the
student edits one, Argus leaves it alone for good, with no proposal: a diff of canvas JSON isn't useful to review.

## D037 — Exam review: horizon, order, and nudges (2026-10-19)
*Placement and nudges superseded by D061 and D062; the readiness order stays for Exam prep.*
Review sessions (45 minutes, 3 concepts) sit 7, 5, 3, 2, and 1 days before an exam, in free time left after work
sessions. They are planned for exams inside the 14-day planning horizon; days already past are skipped. Concepts
go weakest first (shaky, meaning missed or unsure, then untested, then solid; inbox confusion raises priority) and interleave across
modules. Self-grading triggers a replan, so the order follows readiness. Review blocks get their own start
nudge, "Time to review", keyed apart from work nudges.

## D038 — Confusion inbox is matched by code, not AI (2026-10-19)
A line in `Argus/Inbox/*.md` matches the concept whose name words mostly appear in it (at least half), narrowed
by a course code if the line names one. A match raises the concept's priority once (the entry is keyed by
path and text hash, so a line never counts twice). Targeted practice (AI, then verified) runs in the worker's
study step and is retried until `practice_at` is set. Unmatched lines are stored and do nothing else. Argus
never edits or deletes inbox files.

## D039 — One vault cycle at a time (2026-10-19)
The worker (every 15 minutes) and the web app (approving a proposal) both run vault cycles. An `flock` on
`<data_dir>/vault.lock` serializes them. The worker runs git synchronously at the end of its tick, after
reminders, so reminders never wait on git. Each git call has a 120-second timeout. Approval runs inside a
cycle: pull first, so the exact-base check sees the student's latest version, then apply, write, commit, and push.

## D040 — Where proposals and exam prep show up (2026-10-19)
*Superseded by D067.*
A banner on Today (not on every page) says when proposals are waiting, and Health shows the vault's sync status and
proposal count. Exam prep is in the desktop nav (`g e`). On mobile it is reached from exam items, the course page,
and the palette, so the tab bar keeps its five tabs.

## D041 — Verifier eval (2026-10-19)
`tests/evals/datasets/verification.py` holds 22 synthetic notes with 32 seeded errors (unsupported claims, miscited
claims, wrong practice answers, wrong diagrams) and 73 clean elements. A miscited claim counts as caught if it is
ruled unsupported or its citation is flagged. A missing verdict counts as a rejection, as in `apply_verification`.
Gates: catch at least 90% of seeded errors, and false rejects at most 10%. The scorer is unit-tested in CI with
three verifiers: a perfect one, a rubber stamp, and a dead one. The run itself (`make evals`) needs the API key (see D034).

## D042 — Content-Security-Policy (resolves D017) (2026-10-19)
Every page is sent with `default-src 'self'`; `script-src 'self' 'unsafe-eval'`; `style-src 'self' 'unsafe-inline'`;
`img-src 'self' data:`; `object-src 'none'`; `base-uri 'none'`; `form-action 'self'`; `frame-ancestors 'none'`.
There are no inline scripts or event handlers (a test-time grep found none). `'unsafe-eval'` remains because Alpine's
standard build evaluates `x-*` expressions with `new Function`; the CSP build would mean rewriting every expression,
for little gain on a tailnet-only app with no third-party content. Inline styles stay for computed widths (charts).
An e2e test walks the main pages and interactions and asserts zero CSP violations.

## D043 — The webhook is a separate app on its own port (2026-10-19)
Tailscale Funnel exposes a whole port, so a webhook route inside the dashboard would make the dashboard public. The
webhook is its own FastAPI app (`argus serve-webhook`, 127.0.0.1:8001, its own unit) with exactly one route. Only it
goes behind Funnel (port 8443) or Cloudflare Tunnel (with a path rule). The dashboard (127.0.0.1:8000) is reached only
through `tailscale serve`. It has no webhook route, and it refuses every cross-origin POST. Tests assert both.

## D044 — Webhook checks (2026-10-19)
The signature is HMAC-SHA1 over the configured `notifications.webhook_url` plus the sorted form parameters. It is checked
against Twilio's documented example, and compared in constant time. The URL comes from config, not from request headers,
so a proxy can't change what is signed. A missing or bad signature gets 403, and nothing is stored. A valid
request from any number but `user_phone_number` gets an empty TwiML answer (so Twilio doesn't retry) and does nothing.
Requests are handled one at a time. `inbound_messages.message_sid` is UNIQUE, so a replay gets the stored reply without acting again.

## D045 — SMS commands: rules, then AI, then ask once (2026-10-19)
The rule parser handles the documented forms (`LOG 1 90m`, `snooze 3`, `DONE #2.`) and refuses anything ambiguous.
The AI fallback (fast tier) returns one command as JSON, and only confident readings (≥ 0.8) are used. That reading is
turned back into a command string and run through the same parser, so the AI can never do anything a typed command
couldn't (for example, a 30-hour LOG is refused either way). Replies say "Read as DONE 1." when the AI was used.
An unclear text gets the command list once. A second unclear text within 30 minutes gets "nothing changed" instead
of another prompt. Twilio's reserved keywords (STOP, START, HELP, ...) get no reply from Argus.
`[n]` resolves against the item ids of the latest numbered text: a sent notification, or a TODAY reply, which wins ties.

## D046 — OK/NO and Y/N answer the open question about an item (2026-10-19)
Today the only questions Argus asks by text are about announced changes: a date conflict ("Reply OK 1 to use the
post's date, NO 1 to keep the official one") and a possible cancellation. OK/Y confirms and NO/N dismisses, through the
same functions as the Announcements page, so the official date changes only on the student's explicit yes.
With one item in the latest text, the number may be left out.

## D047 — Snooze never silences the 3-hour warning (2026-10-19)
`SNOOZE n d` defers every reminder whose items are all snoozed (start nudges, 24-hour, at-risk, countdowns), up to
3 days. The 3-hour warning still goes out: deadline alerts are deterministic and the last one is not optional.
The reply says so.

## D048 — Twilio: one attempt per send (2026-10-19)
A POST that timed out may still have been accepted, so retrying inside the channel could double-text. The channel tries
once. On failure the router falls back to ntfy at once, and the engine's retry for failed notifications still applies.
The SMS simulated week includes an hour-long Twilio outage: those reminders arrive by ntfy, and none are lost or doubled.

## D049 — "[1]" in texts was being sent as "?1?" (2026-10-19)
`[` and `]` are in GSM-7's extension table, not the basic set, so `to_gsm7` replaced them with `?`. Extension
characters are now kept and counted as two septets by `gsm_length`, and `fit` cuts by septets. Rule tests assert
`gsm_length(sms) <= 160`.

## D050 — Morning brief default 08:00 (2026-10-19)
The brief used to default to 07:30, inside the default quiet hours (23:00 to 08:00), so it was always deferred to 08:00.
The default is now 08:00. The brief lists today's open deadlines (numbered for replies) and the next session.
Countdowns go out 7, 3, and 1 days before an exam, naming the two weakest concepts in `review_order`'s order
(shaky, then untested). Announced changes are texted for two days after the post; older ones stay on the Announcements page.

## D051 — Secrets on the Pi are credential files (2026-10-19)
Each secret is a root-only file named for the secret in `/etc/argus/credentials/`. `install.sh` writes a
`credentials.conf` drop-in per unit with one `LoadCredential=` per file (this works on any systemd ≥ 247, without
depending on directory credentials). `argus-admin` runs any CLI command as a transient unit with the same user, folder,
and credentials, so manual commands never need secrets in the shell environment. `load_secrets` accepts both
`argus_<name>` (systemd) and `<name>` (the folder itself).

## D052 — Backups and the dead-man's switch (2026-10-19)
`argus backup` (a systemd timer, 03:30) takes an online SQLite snapshot, gzips it, checks its integrity, and prunes
to 14 days. It then mirrors the folder to `paths.backup_remote` with `rsync -a --delete`, so the remote also holds
exactly 14 days. A remote that starts with `-` is refused. `argus restore` verifies the archive before replacing the
database and keeps the old file. The worker pings `healthchecks_url` every 5 minutes after a tick, or `/fail` when
the reminder step crashed. A failed ping is logged, never raised.

## D053 — Fixes from the independent review (2026-10-19)
All 16 findings in [review.md](review.md) are fixed and tested. Where the fix is a judgment call:
- **R1.** An override (confirmed date or cancellation) records the official date it was made against (`basis`). Any
  later official change drops it, and official wins again. Reminders re-arm, because their keys include the due date.
- **R2, R7, R16 (SMS).**
  - OK/NO resolves to the finding named in the latest "announced" text about that item. With several waiting and none
    texted, Argus refuses and points to the Announcements page.
  - `[n]` counts SMS texts only. When two unsolicited texts within 10 minutes give the same `[n]` different meanings,
    Argus refuses and asks for TODAY.
  - AI readings never run DONE, OK, NO, or SNOOZE. They answer "Did you mean …? Reply … to do it." LOG, EST, and TODAY still run, with "Read as …".
- **R3.** Argus-owned frontmatter keys are edited line by line; everything else stays byte for byte. If that can't be
  done and verified by re-parsing (for example, a one-line flow mapping), the change becomes a proposal.
- **R4.** Failed sends retry with backoff (1, 2, 4, 8, then every 15 minutes) until they expire, through the guards.
  Deadline reminders still failing after 3 tries make the heartbeat report `/fail`. That alert travels through
  healthchecks.io, a different path than the one that is failing.
- **R5.** The reminder engine runs first and last in every tick. Sync has a 240-second budget, and each AI step has
  90 seconds (a step that runs out is cancelled and retried next tick). A separate reminders task was considered,
  but steps share one SQLite connection, and interleaving at awaits inside transactions isn't worth the risk.
- **R6.** A complete listing that drops all, or at least 3 and more than half, of a course's upcoming records is not
  believed. Nothing is marked absent, the run is an error (three in a row alert), and the payload is fetched in
  full again. If the listing is still the same 24 hours later, it is believed. The cost is a deleted assignment
  that keeps reminding for a day; the alternative was silently losing deadlines.
- **R8.** The cap stays at 6, but its last 2 slots are kept for 24-hour warnings. The spec defines the cap as
  excluding only at-risk alerts and 3-hour warnings, so 24-hour warnings stay counted, with room reserved for them.
- **R10.** Code enforces the graded-work rule, not just the prompts. Materials whose titles look like graded work
  (homework, problem sets, projects, assignments, solutions, answer keys) or name a tracked item never reach the
  generator. Practice and review material is allowed. Inbox lines naming a tracked item get no targeted practice.
- **R11.** Only trusted posts (staff, confident) create a texted conflict; others are held for the Announcements page.
- **R12–R15.**
  - R12: `TimeWindow` refuses start == end.
  - R13: uvicorn logs go through the redactor (`log_config=None`).
  - R14: the webhook reads only URL-encoded bodies of 16 KB or less, before parsing.
  - R15: browsers without HTMX get an error page with the same status code.
- **Also.** An unexpected error inside an SMS command is answered and recorded, so Twilio's retry never reruns it.

## D054 — One capabilities object (2026-10-19)
**Context.** "Is this source set up?" was answered in scattered places (`data.tier2`, `ai_on`, per-template copy),
and only the course page hid anything.
**Decision.** `argus/capabilities.py` derives a frozen `Capabilities` from `config.yaml` and the secrets, using each
collector's own `enabled()`, so it cannot drift from what the sync runner does. Features are named by what the
student sees. The matrix (pinned by `tests/unit/test_capabilities.py`):

| Feature | Needs any one of |
|---|---|
| calendar | canvas_ical |
| modules, grade, syllabus | canvas_modules (and, per course, a `canvas_id`) |
| canvas_announcements | canvas_api |
| gradescope | gradescope |
| piazza | piazza, piazza_email |
| staff_posts (the Announcements page) | canvas_api, piazza, piazza_email |
| submission_tracking | canvas_api, gradescope |
| ai | an Anthropic key (or an injected transport in tests and the demo) |
| study_generation | canvas_modules and ai |

Per-course flags add the course's own id, so a token does nothing for a course without a `canvas_id`. The AI budget
is applied at request time (`with_budget`) and hides only buttons that make new calls (Re-run AI estimate), never
content already produced. One object is built per process (web app, webhook, worker) and passed to the web views,
templates (`caps`), the vault builder, the reminder engine, and the SMS inbox. The demo has two profiles
(`argus serve --demo [--no-canvas-token]`); every page is tested in both.

## D055 — Hidden, not empty, app-wide (2026-10-19)
**Decision.** A feature whose source is not set up is left out everywhere: no section, no empty box, no nav entry, no
palette command, no copy that names the source. Capability-dependent markup carries `data-requires="<feature>"`;
`tests/integration/test_capabilities_web.py` checks every page in four setups (full, no token, token removed after a
sync, no staff-post source) for markers of features that setup lacks. Stored data from a source that is no longer set
up is not shown either (a token removed after a sync leaves modules, a grade, and ELMS announcements in the database);
the vault stops updating module notes but never deletes them. The only mention is on Health ("not set up" with the
one-line fix), including a note on reminder rules that cannot fire (the announcement rule needs staff posts and AI).
Without staff posts, `/announcements` redirects to Today and SMS HELP leaves out OK/NO. Without study notes, the vault
writes no flashcards note, concept map, or inbox README, and the exam note's study regions stay empty (the markers
remain, so they fill in if study notes arrive later).
Stored posts from a removed source also stop *acting*: the AI scan skips them (they are kept unscanned for when the
source returns, so nothing they say can change a date), and their findings send no "Announced" texts. Study generation
runs only with `study_generation`. Deliberately kept: items and the scope or logistics already applied to them from
such findings. An announced quiz is a deadline, and hiding a deadline is the one failure Argus must not have.

## D056 — No MISSED without a submission source (2026-10-19)
**Context.** The calendar feed never says whether work was submitted. Without the ELMS API or Gradescope, every
iCal-only assignment became MISSED at its deadline, was shown as Overdue, and its 3-hour warning said "Not submitted
yet" even when it had been.
**Decision.** An item is *tracked* if a present source record comes from an enabled source that reports submissions
(`SUBMISSION_SOURCES`: canvas_api, gradescope). `refresh_statuses` takes that set as a parameter, so the store stays
config-free; the sync runner computes it from the collectors it actually runs. An untracked item past its (late-aware)
deadline stays open: `RiskLevel.UNCONFIRMED`, "Did you submit?" on Today with Mark done (no Start) for 7 days, then
"Not marked done" where it is still listed (the course page). It is never OVERDUE. The 24-hour and 3-hour warnings keep
their timing, keys, and guards; only the 3-hour body changes for untracked items ("Reply DONE 1 once submitted"). The
deadline golden (`tests/integration/test_deadline_golden.py`) proves the timing did not move.

## D057 — Work windows (2026-10-19)
**Context.** "Free time" was waking hours minus classes, so 14 days held 167 hours, and every gap was booked with
sessions the student could not move.
**Decision.** The student says when they usually work (`schedule.work_windows`, for example Mon-Thu 19:00-23:00 and
Sun 13:00-18:00). Argus plans into those hours only. `days` are start days, and an end at or before the start runs
past midnight. At most 3 windows a day; overlaps merge, with a `check-config` warning. Classes (with the walking buffer)
and commitments inside a window are not work time; quiet hours do not cut windows. Hours are real elapsed hours, so a
window across a DST change is 3 or 5 hours long. Each occurrence has a stable id, its local start date and time
(`2026-10-19T19:00`), so keys survive class and commitment changes. Config seeds the `work_windows` table on the first
run only, and only when it lists at least one window (so adding windows to config later still seeds); after that
Settings owns them: the Settings page edits them (days with an "Every day" option, start, end; checked per row, at most
3 a day; saving replaces them and replans at once). `argus windows --reset` (on the Pi: `sudo argus-admin windows
--reset`) copies config.yaml's windows again. `/etc/argus/config.yaml` is root-owned and the units run with `ProtectSystem=strict`, so the app
could not write it anyway. Without any windows, risk falls back to the time-only rule and no window messages are sent.
`schedule.max_session_minutes` is retired but still accepted, so existing configs load.

## D058 — The queue (2026-10-19)
**Decision.** Instead of a calendar, open work is a ranked list. The key is total, so the order never depends on input
order: skipped for this window ("Not tonight") last, then pinned (in the student's order), then tier (overdue but
still workable, at risk, creeping, exam study due now, the rest), then slack (window hours to spare, rounded to
minutes), then the due date (undated last), then work before study, then the id. An explicit pin lasts until the
item is done. Moving an entry up or down pins the displayed prefix as "move" pins that expire at the end of the
window, so the automatic order comes back. A move names the neighbour the student saw; if the list has changed, the
move is refused. Each window's time is forecast in queue order: exam study first, then work, each in the earliest
windows before its due time, never overfilling a window and never giving an item more than it needs. The window's
split is stored just before it starts and frozen when it starts (a worker that was down at the start stores what is
left).

## D059 — Creeping and at risk on window time (2026-10-19)
**Decision.** For each open item with a due date: C is the work-window hours before it is due (after now), minus exam
study reserved in those windows, minus a window it is skipped for. D is its own remaining hours, plus those of counted
items due at the same time, plus the earlier work that can still be done before its due time. Walking the due dates
in order, earlier work counts only up to the window time before its own deadline: work that cannot fit by then is
missed and does not take time from later items (the deadline-feasibility test). Past-deadline and unconfirmed items do
not count; an overdue item still inside its late window is planned up to its late deadline and does count. At risk: C < D. Creeping: C < 1.5 x D. An item with nothing left (E = 0) is OK. Comparisons are strict.
Remaining hours (E) are the calibrated p80 (or the student's estimate) minus logged time, or the student's latest
"about N h left" minus what they logged since. Without windows or an estimate, the time-only rule applies (due within
24 hours and not started: creeping). With nothing logged, C only shrinks and D never grows, so an item's level only
gets worse, except when a competing item's deadline passes (or its unfinishable part stops counting): then the item
may get room back. The simulated week checks this, and that the deadline golden is unchanged with windows on.

## D060 — Progress: ask, don't assume (2026-10-19)
**Context.** D030 counted passed sessions as done to stop a nag loop. With no sessions there is nothing to assume, and
the nag loop cannot recur: window messages are keyed per window and at-risk alerts per item and due date.
**Decision.** Progress is only what the student logged: the timer (counted live while it runs), `LOG`, the form, or
an answer to "Last night: did you work on Homework 3? 2h, 1h, None", asked once on Today after a window whose planned
items have no time logged in it (or a LOG within a day after). An answer is a `checkin` time log, unique per item and
window; "None" records 0 hours. No answer means no credit, and the question goes away when the next window ends.
Planned sessions that had passed under D030 are converted once to `legacy` time (items with no real logs only), so
remaining hours do not jump at the switch; calibration ignores `legacy` time.

## D061 — The work-window message replaces start nudges (2026-10-19)
**Decision.** Once per window, the lead time set in Settings (default `notifications.window_notice_minutes`, 20) before it starts, one message says
what to do and why, for example "Tonight 7-11pm: [1] Homework 3, 4h of 10h left, due Thu. [2] Problem Set 6, 3h
left, due tomorrow (at risk). Study 45m: Midterm 1." (the first number is this window's share; the second appears when
more is left after it). The key is `window:<window id>`. It respects quiet hours, class time, mute,
and snooze (it waits only when every named item is snoozed), and expires 30 minutes after the start. It does not count
toward the daily cap (at most 3 windows a day), so it can never delay a 24-hour warning. It is built entry by entry so
it fits one GSM-7 segment and its numbers match `item_ids` exactly; exam study is never numbered, so `DONE n` cannot
hit an exam. At-risk alerts keep their key but are batched into one message ("+N more on Today" when they don't all
fit), and an item planned for the window in
progress waits until the window ends (the student may be working on it). The morning brief and `TODAY` name the next
window and what it starts with. `Rule.START_NUDGE` stays as a value so old rows still parse; Health hides it. A planner
failure in a tick is logged and skipped: the 24-hour and 3-hour warnings never depend on it.

## D062 — Exam study time (2026-10-19)
**Decision.** Each exam gets one "Study for <exam>" card. Its hours are the exam's calibrated p80, clamped to 1-6 h;
progress is time logged on the exam item only. What is left is spread evenly (recomputed window by window, so time a capped window can't take moves to the later ones)
over the windows that start in the week
before the exam and still have at least 30 minutes before it: at least 20 minutes per window, capped at half a window
(three quarters in the last 48 hours), and three quarters of a window across all exams. That time is reserved first in
each window; homework fills the rest, and its capacity C excludes it. What cannot fit is the shortfall, shown on the
exam page and named in the plan line. Readiness and the review order stay for the Exam prep page and the countdown's
"Weakest:" names; they do not feed the queue.

## D063 — Today leads with the next task; a day strip on Today, a laned rail on the kiosk (2026-10-20)
**Decision.** Today answers "what do I do now": the hero is the first queue entry (title, course, due, "~4 h left",
one reason line such as "6 h of window time before it's due", and Start), or the running timer when one runs. Then
the confirm line (D060), the ranked queue as an `<ol>` (with study cards), a "Not tonight" list, then "Did you
submit?", past due, and today's and tomorrow's exams and events. The vertical day rail moved off Today: a compact,
read-only horizontal strip shows classes and commitments in lanes, work windows shaded, deadlines in their own lane,
and the now needle; its range is the busy part of the day ±30 min, and its labels sit in a list under it. The kiosk
keeps the vertical rail, now with overlapping blocks in columns, a range stretched to take in late deadlines, and hour
labels at real local hours (25 on the fall-back day). Both use `planner/lanes.py` (greedy interval colouring: lanes =
the most blocks overlapping at once). Desktop puts the strip and "Due soon" beside the queue; the phone stacks them
last.

## D064 — Queue controls and reorder semantics (2026-10-20)
**Decision.** Each card has Move up / Move down, Pin to top (Unpin), Not tonight (Back to tonight), Mark done, and a
time-left field; study cards have only the moves and Pin. With a mouse they show on hover or focus-within (always in
the DOM, so nothing shifts and the keyboard reaches them); on touch screens and narrow windows the same controls sit
in a "..." `<details>` menu, which needs no JS. Pin and SMS `FIRST` both pin to the top until done or unpinned, and
clear a "Not tonight". A move pins everything from the top down to the moved pair, as "move" pins that expire when
tonight's window ends (at midnight without windows); explicit pins stay pins. A move names the neighbor it swaps
with (`before`); if that is no longer the neighbor the move is refused and the list is shown again with "The list
changed". Moves act on tonight's list only (skipped entries are listed apart). Every control is a form: with HTMX it
swaps `#queue` and the server names the control that keeps focus (the opposite arrow at an edge); without JS it
redirects to `/today#q-<entry>`. Mark done from the queue removes the card and puts "Marked X done. Undo" at the
top of the queue, with focus on Undo. While a timer runs on a card further down, that card is the hero and also keeps
its row, so every move swaps with a visible neighbor. An AI reading of `LEFT`, like `SKIP`, asks first.
SMS adds `SKIP n`, `FIRST n`, and `LEFT n h`; each needs a number.
The window message's lead time is set in Settings (a `settings` row, 0011); config.yaml's value is only the default.

## D065 — Color roles, markers, and the workload chart (2026-10-20)
**Decision.** Teal (`--brand`) is reserved for four jobs: now and next (the hero's "Next up", the running timer), the
primary action, the active nav, and focus. Bronze (`--needle`) is only the "now" needle. Links are ink with an
underline. Course color appears once, as a 3 px stripe on rows (and the hero); the code is muted text, and the chip is
kept for item, exam and course headers. Rows show the title, then a muted line (course, the kind unless it is an
assignment, "~4 h" from the remaining calibrated p80), the due date and relative time, and a marker only for
creeping, at risk, overdue or "Did you submit?". Sources moved to the item page. Row controls follow D064: hover or
focus on a mouse, a "..." menu on touch. Markers are always an icon plus a label, and the fill gets heavier with
severity: creeping has none, at risk a soft fill, overdue a solid one, "Did you submit?" a neutral outline. The
three status hues were re-picked with the dataviz skill's `validate_palette.js` (light `#756b0a` / `#8a2f00` /
`#b21a77`; dark `#e8d65a` / `#f78f3c` / `#f871c8`): neighbours are at least 15 OKLab ΔE apart under normal vision
and 8 under protan and deutan simulation, and every text pair meets AA. `test_tokens_contrast.py` carries a Python
port of those two checks. The dark set is lighter than the validator's band for chart marks, because these colors
are text on a dark surface and need 4.5:1. Dark tokens are written once (`[data-theme="dark"]`);
`scripts/gen_tokens.py` writes the media-query copy and the font fallbacks, and a unit test fails if they are stale.
/workload replaces the per-day bars ("planned of free") with two cumulative step lines on one hours axis over 14
days: window time (neutral) and work due (teal; queue work by when it must be done, plus each exam's remaining study
time by the exam, `planner/workload.py`). Where work due outruns window time the gap is shaded and the worst day is
labelled ("7 h short"); forced colors get a dashed line and a hatch. A sentence leads ("Next 7 days: 14 h work due,
16 h window time."); the plot stretches to fit and its text is HTML, so labels never shrink on a phone. A per-day
readout follows the pointer, or the arrow keys once the plot has focus, and a table has every value.

## D066 — The display face (2026-10-20)
**Decision.** Bricolage Grotesque (OFL), chosen over Fraunces and Space Grotesk after a side-by-side on real content
(the hero, a page title, the timer, the kiosk clock): its figures are even and lining, which matters for "42:25" and
the kiosk clock; Fraunces set "4" old-style and reads as a generated look; Space Grotesk's hooked "1" made "11:08"
odd. It is vendored from fontsource (pinned by sha512) with fonttools: opsz 48 and wdth 100 pinned, weight cut to
500–800, latin only, 39 KB. It is used for page titles, section titles, the hero task, and big numbers (timer, stats,
kiosk clock and headings), and nowhere else; Atkinson Hyperlegible Next stays for body and data. The hero task is the
one bold thing (37 px on desktop), so Today's date title stays a step smaller. The scale is a perfect fourth: 12.5 /
14 / 16 / 21 / 28 / 37 / 50, and 96 on the kiosk. Both faces are preloaded (73 KB, under the 90 KB budget) with
`font-display: swap` and a local Arial or Liberation Sans fallback whose size-adjust and ascent/descent overrides
match the web font's metrics, so the swap moves nothing.

## D067 — Navigation and the status line (supersedes D040) (2026-10-20)
**Decision.** The header nav is text only: Today, Upcoming, Exams, and "More" (Workload, Announcements when a staff
post source is set up, Health, Settings, Proposals with a count). "More" is a `<details>` menu: Enter or a click
opens it, Esc closes it and returns focus, a click outside or a navigation closes it. Search and Ctrl K stay; the
theme toggle left the header (Settings and the palette set the theme). Phones get four tabs: Today, Upcoming, Exams,
More (a menu that opens upward). The viewport is `viewport-fit=cover`; the body's bottom padding is the tab bar plus
the safe area plus 1rem, and an e2e test checks that the last control on a page is never under the tab bar. Banners
gave way to a status line under the header: a dot and a note, "Gradescope 3h behind", "Piazza hasn't synced yet",
"1 note edit to review" (on every page but Proposals), and a compact offline line. A full banner is only for a real
error: a source failing 3 times in a row, the vault failing 3 times in a row, a vault conflict, or a page that
couldn't load from the database.

## D068 — The mark (2026-10-20)
**Decision.** The wordmark: "argus" in the display face (Bricolage Grotesque at weight 750, outlined with fonttools,
so it renders without the font), with the g's ear replaced by a bronze needle, the same needle that marks "now" on
the day strip. The square mark (favicon, PWA icons) is the "a" with the needle growing from its shoulder, drawn light
on the night-blue tile. One-color variants draw both parts in `currentColor`. The header shows the wordmark alone.
Picked by the user over two other directions shown at 16, 28, 64 and 192 px in both themes: **A**, an "A" whose
crossbar is an eye with a slit pupil (the strongest app icon, but the nearest to the Eye of Providence: an eye in a
triangle), and **B**, seven eyes on an arc, one bronze for now (a peacock's train; busy at 28 px, and it reads as a
loading spinner). **Checked against existing marks:** no eye and no triangle, so nothing of the Eye of Providence;
nothing of the Google Lens or Apple Watch app glyphs (a camera ring, a watch outline). The nearest familiar mark is
Amazon's "a" with an orange arrow, which is curved and sits under the letter; Argus's needle is straight and leaves
the shoulder. A steep needle on the lone "a" read as an accent ("á"), so the needle is flatter (about 25°) and touches
the letter. Source: `components/brand.html` (`mark`, `wordmark`, `mark_shapes`); `scripts/make_icons.py` renders
`argus.svg` and the 192 and 512 px maskable PNGs with Playwright from `mark_shapes` in the dark tokens, and a unit
test runs its `--check`. `theme-color` now matches the header surface (white, and `#121b2c` dark) instead of teal,
which D065 reserves.

## D069 — Estimates: read the whole task or ask (2026-10-22)
**Decision.** The AI estimates an item only when it can read the full description of the work. Two gates: the
description must be at least 200 characters (whitespace collapsed, `estimates.MIN_DESCRIPTION`), and the AI must say
it describes the whole task (`estimate.v2` adds `whole_task` and `missing`: a description that points to an attached
PDF, covers only logistics, or is too thin to size the work gets `whole_task: false`). Otherwise there is no estimate:
no per-kind default, no median of past actuals, no title-only guess. The student sets the time instead, from a
"Set time" button that is always visible (never hover-only) on a queue card, the hero, or a row of open work with no
estimate and no "about N h left": a popover with 30 min, 1, 2, 3, 5 and 8 h and any other number. The item page says
why there is no estimate and offers the same choices inline; `EST n h` still works by text. **Why.** Estimates made
from a title (Canvas iCal often carries little or no description) were far off, and a confident wrong number planned
the week worse than an honest blank. **Details.** A miss is stored in `estimate_misses` per item and input hash:
`incomplete` is never asked again for the same description, `failed` (invalid output, budget, an API error) is
retried after 24 hours. No AI call is made once the student has set a time. Old estimates with `source = 'default'`
or from `estimate.v1` no longer count anywhere (planning, item rows, calibration history), and items with a full
description are estimated again under v2. The non-AI fallback for an estimate is now the student's own answer.
Exam study time still reserves the per-kind exam default when an exam has no estimate (D062): it budgets study, it
does not claim how long an assignment takes.

## D070 — Tasks you add yourself (2026-10-22)
**Decision.** "Add a task" on Today opens `/tasks/new`: what, course, due date and time (default tonight, 11:59 pm),
optional hours and notes. A task is stored as a `manual` source record (`argus/tasks.py`), so it merges into an
assignment item and ranks in the queue, gets the 24-hour and 3-hour deadline texts, and gets a vault note like any
other item. Hours, when given, become the student's estimate; without them the row offers "Set time" (D069). The
item page of a task has "Delete task", which marks the record absent so the next merge removes the item. **Why.**
Studying and reading compete for the same work windows as graded work, and the queue can only plan around what it
knows about. Reusing the source-record path kept merge, planning, reminders, and the vault unchanged, with no
schema change. **Details.** The external id hashes course, title (case-folded), and due time, so a double submit
adds one task. Collectors never mark `manual` records absent, so syncs keep tasks. Synced items can't be deleted
here: their source would bring them back. A task needs a course, since every item has one. Its kind is
`assignment`; there is no separate "study" kind.

## D071 — A desktop widget and countdown window on the Mac (2026-10-22)
**Decision.** `GET /widget.json` returns Today's summary line, the stale-source notes, and up to 8 open,
actionable items due in the next 7 days, sorted by due time. Each item has its id, title, course, accent, kind,
due time (UTC, `Z`), and path. `mac/` is an XcodeGen project with two parts, built with SweetPad or `xcodebuild`.
ArgusWidget is a medium WidgetKit widget. ArgusBar is a background menu bar app: it polls the feed every 5
minutes and pops a glass countdown window. **Why.** The student wants the next deadlines on the Mac desktop at a
glance, without opening the site. The feed is a GET on the tailnet like every page, so it needs no token (D016
guards writes only); the widget stays read-only. **Details.** Countdown rule: an item due on today's local date
with 12 hours or less left shows a live countdown. In the widget it's `Text(timerInterval:)`, which ticks with no
reloads. The window pops at 12 h, 3 h, and 1 h left and stays until dismissed. Each pop has a key
`id:due_at:milestone` (UserDefaults), so a moved due date re-arms it. The rule lives in the Mac app
(`mac/Shared/Countdown.swift`), not in the feed: it has to switch on at an exact time, and WidgetKit limits refreshes
to a budget (roughly every 15 minutes), so the feed can't do it. These windows are extra. Argus's own 24-hour and
3-hour texts stay the deterministic deadline alerts. The tailnet host and signing team go in the git-ignored
`mac/Config/Local.xcconfig`. `make mac` generates the Xcode project (XcodeGen), `make mac-test` runs the Swift
tests, and SweetPad runs the `ArgusBar` scheme. **Quirks.** Anything time-based in the widget uses timeline entries or
`Text(timerInterval:)`, never a fetch. A SwiftUI view built inside a SwiftUI update (for example from the `App`'s
`@State` init) aborts in AttributeGraph, so AppKit panels are opened from a `Task`.
