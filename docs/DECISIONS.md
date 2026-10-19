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
**Context.** The simulated week showed that with progress = time logged only, a student who never logs time got the
same work re-planned (and re-nudged) every hour, forever.
**Decision.** If an item has any time logs, they are the truth. Otherwise planned sessions that have already passed
count as done. The student corrects with `LOG n h` or `EST n h`. Calibration still uses logged actuals only.

## D031 — Plan quality rules (2026-10-18)
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
