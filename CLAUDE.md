# Argus — operating manual

## What this is
Argus is a personal, always-on course hub for one UMD student, running on a Raspberry Pi 4B.
It pulls deadlines, announcements, and course resources from ELMS (Canvas), Gradescope, Piazza, and
umd.io. It estimates effort, ranks the work into a queue for the student's work windows, and builds an
Obsidian study vault.
It reminds the student over SMS (Twilio) or ntfy, and accepts commands by reply.
Product behavior: `docs/SPEC.md` if present, otherwise the build prompt. Choices: `docs/DECISIONS.md`.

## Non-negotiables
- Deadline alerts are deterministic and never depend on the AI.
- The hub never edits user-written vault text without prior permission or an approved proposal.
- Official sources (Canvas, Gradescope) beat dates extracted from posts.
- No secrets in git, logs, or the vault.
- The AI never drafts solutions to graded work.
- Every AI output is schema-validated and has a non-AI fallback.

## Commands
| Task | Command |
|---|---|
| First-time setup (deps, Chromium, Tailwind CLI, hooks) | `make setup` (needs `uv`, `gitleaks`) |
| Run dashboard locally with reload | `make dev` → http://127.0.0.1:8000 |
| Preview with synthetic data (no credentials) | `uv run argus serve --demo` (add `--no-canvas-token` for the setup without a token) |
| Always-on loop (sync + reminders) / test a push | `uv run argus worker` / `uv run argus notify-test` |
| Build CSS (minified) / watch | `make css` / `make css-watch` |
| Lint + format check / auto-fix | `make lint` / `make fmt` |
| Types | `make types` (mypy --strict on `argus/`) |
| Unit / integration | `make test-unit` / `make test-int` |
| Coverage with floors | `make cov` |
| E2E / accessibility / visual | `make test-e2e` / `make test-a11y` / `make test-visual` |
| Re-record visual baselines | `make visual-update` (macOS) · `scripts/visual_linux.sh --update` (Linux) |
| AI evals (real API, not in CI) | `make evals` |
| Everything CI runs | `make check` |
| Deploy to the Pi / tail logs | `make deploy PI_HOST=argus-pi` / `make logs` · on the Pi: `sudo argus-admin <cmd>` |
| Webhook / backup / restore | `argus serve-webhook` · `argus backup` · `argus restore FILE` ([runbook](docs/runbook.md)) |
| CLI | `uv run argus --help` (`sync`, `list`, `serve`, `check-config`, `discover`, …) |

## Architecture map
- `argus/clock.py`: the only wall-clock reader. `Clock`, `SystemClock`, `FakeClock`, local-time helpers.
- `argus/config.py`: `AppConfig` (YAML, no secrets) and `Secrets` (env / `$CREDENTIALS_DIRECTORY`).
- `argus/capabilities.py`: which features exist for the configured sources (D054), from each collector's `enabled()`.
  One object per process, passed to views (`caps` in templates), the vault, reminders, and SMS.
- `argus/logging.py`: JSON logs and the redactor. Call `configure_logging(secrets=…)` at every entry point.
- `argus/collectors/`: one module per source. Fetch → typed records + raw payload hash. Never raises past its boundary.
- `argus/domain/`: pure models, merge rules, status transitions, finding application. No I/O.
- `argus/planner/`: work windows, load and risk (C and D), the queue and its forecast, exam study time,
  calibration, busy time, and `lanes` (layout for the day strip and the kiosk rail). Pure and deterministic.
- `argus/reminders/`: rules (what), guards (when and whether), engine (idempotent dispatch), SMS command parser.
- `argus/ai/`: Anthropic client wrapper (validation, retry, fallback, cache, budget), prompts, task modules.
- `argus/vault/`: `layout` (paths, links), `regions`, `ownership` (pure table), `proposals`, `writer` (the only
  vault writer), `render` (pure notes), `gitsync`, and `service` (the 15-minute cycle, inbox, health).
- `argus/notify/`: Twilio SMS (one attempt), ntfy, and the router (SMS first, ntfy fallback).
- `argus/sms.py`: replies (parse → AI fallback → ask once → act). `argus/web/webhook.py`: the separate public
  webhook app (D043). `argus/backup.py`: snapshots, prune, off-device copy, restore.
- `argus/worker.py`: the every-minute loop: sync → announcements → estimates → study → plan → reminders → vault,
  each isolated.
- `argus/announcements.py` (posts → findings → apply / hold, plus confirm and dismiss), `argus/estimates.py` (AI or fallback,
  overrides), `argus/planning.py` (`compute`: remaining, load, risk, queue, study time, window forecast; `replan`:
  seed windows, legacy conversion, the window's split; the confirm question; queue actions shared by Today and SMS:
  pin, not tonight, move; the notice lead time), `argus/study.py` (materials →
  generate → verify → store), `argus/exams.py` (scope, readiness, attempts), and `argus/demo.py` (the synthetic seed).
- `argus/web/`: the app factory (`app.py`), state and dependencies (`deps.py`), loaders (`data.py`), view models,
  routes per view, Jinja templates, component macros (`components/`), and static assets (vendored by `scripts/vendor_assets.py`).
- `argus/store/`: SQLite connection (WAL, FTS5), numbered SQL migrations, and repository functions (including search).
- `argus/cli.py`: the Typer CLI. `deploy/`: systemd units, installer, kiosk. `scripts/`: dev tooling.

## Data model
SQLite with WAL and FTS5. Full schema: [docs/schema.md](docs/schema.md).
`courses` classes · `meetings` class times · `items` merged assignments/quizzes/exams + status ·
`item_sources` per-source links · `posts` Piazza posts / Canvas announcements · `findings` extracted
facts + applied state · `modules` Canvas modules · `concepts` concept notes · `practice_items`
questions/flashcards · `practice_attempts` results + confidence · `exams` scope/logistics ·
`estimates` versioned, never overwritten · `time_logs` actuals and check-ins · `work_windows` when the student works ·
`queue_prefs` pins and skips · `remaining_marks` time left · `window_plans` each window's split ·
`notifications` every send, unique idempotency key · `inbound_messages` replies · `vault_regions`
path/region/hash/commit · `proposals` pending edits to user text · `sync_runs` collector health ·
`settings` choices made in Settings (the notice lead time).

## Conventions
- **Time.** Store UTC only (ISO 8601 with `Z`/offset). Convert to `America/New_York` only at
  display or when evaluating local-time rules (quiet hours, waking window). Never call
  `datetime.now()` (ruff bans it); take a `Clock`. Naive datetimes are bugs; `ensure_utc` rejects them.
- **Idempotency.** Every side effect has a key, stored with a UNIQUE constraint. Notifications:
  `item_id:rule:due_at_iso`, so a moved due date re-arms reminders; the work-window message is `window:<window id>`
  (once per window). Check-ins are unique per item and window. Proposals apply once by
  id + diff hash. Collectors upsert by `(source, external_id)`.
- **Errors.** Collectors and AI calls return `Result`-style outcomes and record `sync_runs`. They do
  not raise into the scheduler. Domain functions raise `ValueError` on invalid input. Web routes render the
  error state. Never swallow an exception without logging it.
- **Logging.** `argus.logging.get_logger(__name__)`. Put structured fields in `extra={...}`.
  Never log message bodies with phone numbers or raw credentials. The redactor is a backstop, not a license.
- **Naming.** Modules are nouns (`merge.py`); functions are verbs (`merge_items`). Records are
  `*Record` (collector output); persisted rows are domain models in `domain/models.py`.
  Course codes are uppercase, no space (`CMSC351`).
- **Fixtures** live in `tests/fixtures/<source>/`, are synthetic or scrubbed, and never hold real names,
  emails, ids, or course content.

## Source quirks
- Canvas iCal times are UTC.
- The Canvas iCal feed never says whether work was submitted. Without the ELMS API or Gradescope, Argus cannot tell
  submitted from missed, so such items are never MISSED (D056).
- Canvas API pagination uses `Link` headers (`rel="next"`). Follow them; do not compute pages.
- Canvas omits module `items` for large modules and gives `items_url`, which is also Link-paginated. Announcements
  need `context_codes[]` per course. Only staff can post them.
- umd.io meeting times are strings like `9:30am`; umd.io `seats` fields are strings.
- Gradescope may need a password even for SSO users (set one at gradescope.com).
- `gradescopeapi` and `piazza-api` are unofficial and break without warning. Their versions are pinned exactly.
- Canvas iCal SUMMARY ends with `[CMSC351-0101-Fall2026]` (stripped for display). The feed keeps about 30 days of history.
  The feed URL path *is* the token, so it is redacted everywhere.
- `gradescopeapi` posts login credentials as query params. Student-view rows without a link have no id
  (Argus derives `name-<hash>`), and its dates carry offsets.
- `piazza-api` login needs a `session_id` cookie. `content.get` is per post, so fetch only staff-relevant posts.
- The Piazza notification email format is unverified until real mail arrives (`collectors/piazza_email.py`).
- The newest Playwright Python Docker image is v1.63.0, so Playwright is pinned `==1.63.0` to match.
- Twilio signs the exact public URL it posts to: `notifications.webhook_url` must match it character for character.
  STOP/START/HELP are Twilio's (Argus stays silent). `[` and `]` are GSM-7 extension characters (2 septets each).

## AI rules
- The AI judges and code decides. Every call goes through `AIClient.structured()` (schema-validated, one retry, then `None` → the caller's
  fallback). Prompts are versioned files in `ai/prompts/`; change wording by adding `name.vN+1.md`.
- Every prompt states: "Argus never drafts answers or solutions to graded work." A test enforces this.
- Spend is recorded in `ai_calls`. At the monthly cap, no calls are made, there is one alert, and fallbacks run.

## Vault rules
- Argus writes only between `%% argus:begin <id> %%` and `%% argus:end <id> %%`. The `core`
  region holds generated note content. "Explain it yourself" and "My notes" sit after the
  markers and are never written.
- `vault_regions` stores each region's last-written hash. The ownership decision (`vault/ownership.py`):
  new Argus note → write · region hash unchanged → rewrite · region edited by the user → leave, create a proposal
  · user text elsewhere → leave, propose only · `argus-edit: allow` in frontmatter (note or folder) →
  edit freely and log it in the weekly note · approved proposal → apply that exact diff once.
- Proposals live in `Argus/Proposals.md` and the Proposals view, and expire after 14 days.
- Git sync every 15 min: `pull --rebase` → write → commit as `Argus <argus@pi.local>` → push.
  On conflict, abort the rebase, keep the user's version, and retry next cycle. Never force-push. Alert after 3 failures.
- Canvas files are linked, never copied, into the vault. Layout and the course folder note: D035.
- Nothing is published unverified: concepts and practice exist only after the verifier's verdict is applied
  (`domain/study.py`). Without AI the vault still gets course, module, assignment, exam, Today, and weekly notes.
- `Argus/Inbox/` is read-only for Argus (D038). Flashcards keep Obsidian_to_Anki ids ([docs/anki.md](docs/anki.md)).
- Vault cycles are serialized by a file lock (D039). Tests must point `paths` at a temp dir, never `var/`.

## UI rules
- Design tokens are CSS custom properties in `argus/web/static/src/tokens.css`, exposed to Tailwind via
  `@theme`. Use tokens, never raw hex. Spacing is a 4px scale. Light and dark themes follow the system,
  with a manual toggle.
- Components are Jinja macros in `argus/web/components/`. Each appears in every state on `/styleguide`.
- Every view handles **loading, empty, error, stale** ("Gradescope last synced 3h ago") and **offline**.
- **Hidden, not empty** (D055): a feature whose source isn't set up renders nothing (no section, nav entry, or copy
  naming the source); only Health says "not set up". Gate on `caps.has(...)` / `caps.course(code)` and mark the
  element `data-requires="<feature>"` so `test_capabilities_web.py` can check it. Stored data from a source that is no
  longer set up is not shown either.
- WCAG 2.2 AA: zero axe violations in both themes, visible focus rings, a full keyboard path, a reduced-motion
  variant for every animation, and targets of at least 24×24px. Nothing loads from a CDN (the Pi may be offline).
- Budgets: under 150 KB gzipped JS+CSS per page, median server time under 150 ms, no layout shift on HTMX swaps.
- Add every new page to `tests/e2e/pages.py`, so it gets axe and visual coverage automatically, in both demo profiles
  (`cases()`: full and `notoken`; no-token baselines end in `-notoken`).
- Every state-changing route works with and without JS: HTMX gets a fragment plus an `HX-Trigger` toast; a plain
  form gets a 303 back. Cross-origin POSTs are refused (D016). Design reference: `docs/design.md`.
- Row controls that only show on hover stay in the DOM (opacity, never `display: none`), so nothing shifts and the
  keyboard reaches them; touch and narrow screens get the same controls in a `<details>` menu (D064). After an HTMX
  swap the server says which control keeps focus. `tests/e2e/test_layout_shift.py` guards shifts around swaps.

## Testing rules
- **Unit** (`tests/unit`): pure logic, property tests (hypothesis), and DST/time tests with `FakeClock`.
- **Integration** (`tests/integration`): the store, collectors against fixtures (respx), vault temp repos,
  the webhook (TestClient), and the simulated-week pipeline.
- **E2E / a11y** (`tests/e2e`) and **visual** (`tests/visual`): a live server with a fake clock, driven by Playwright.
  These run in their own pytest process (the `make` targets do this). Plain `pytest` runs unit and integration only.
  Tests that mutate data use `fresh_server()`. Shared servers stay read-only (D022).
- **Evals** (`tests/evals`, marker `evals`): the real API. Run only with `make evals`.
- **LLM calls are mocked** at the `Transport` boundary (`tests/ai_fakes.py`: `FakeTransport`, `ok()`, `recorded()`),
  replaying recorded responses from `tests/fixtures/ai/`. No test outside evals touches the network.
- Evals: `tests/evals/` (datasets plus a pure scorer, which is unit-tested in CI). `make evals` needs `ARGUS_ANTHROPIC_API_KEY`
  and writes JSON reports to `tests/evals/reports/` (git-ignored).
- Write domain logic tests first: merge, planner, reminders, vault edit rules, finding application.
- **Deadline golden** (`tests/integration/test_deadline_golden.py`): the simulated week's 24-hour and 3-hour sends
  must match `tests/fixtures/sim/deadline_alerts.json` exactly. Re-record (`ARGUS_UPDATE_GOLDEN=1`) only for a
  deliberate, reviewed change to deadline rules.
- **Coverage floors** (`scripts/coverage_floors.py`): 95% line+branch per file in `domain/`,
  `planner/`, `reminders/`, `capabilities.py`, `vault/ownership.py`, `vault/regions.py`; 85% overall.

## Definition of done
1. Tests written first for domain logic. New behavior has tests at the right layer.
2. During a phase, run the tests the change touches (`make test-unit`, `make test-int`, targeted e2e).
   `make check` is green at the end of each phase.
3. Any new page is in `tests/e2e/pages.py`. Visual baselines are darwin only, recorded once a phase's UI settles;
   linux baselines are recorded once, at the end of the overhaul.
4. No secrets, real personal data, or real course content in the diff.
5. Non-obvious choices are in `docs/DECISIONS.md`, new quirks are in this file, and the schema change is in `docs/schema.md`.
6. A conventional commit (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), made at a green state.
