# Argus — operating manual

## What this is
Argus is a personal, always-on course hub for one UMD student, running on a Raspberry Pi 4B.
It pulls deadlines, announcements, and course resources from ELMS (Canvas), Gradescope, Piazza, and
umd.io. It estimates effort, plans work into free time, and builds an Obsidian study vault.
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
| Build CSS (minified) / watch | `make css` / `make css-watch` |
| Lint + format check / auto-fix | `make lint` / `make fmt` |
| Types | `make types` (mypy --strict on `argus/`) |
| Unit / integration | `make test-unit` / `make test-int` |
| Coverage with floors | `make cov` |
| E2E / accessibility / visual | `make test-e2e` / `make test-a11y` / `make test-visual` |
| Re-record visual baselines | `make visual-update` (macOS) · `scripts/visual_linux.sh --update` (Linux) |
| AI evals (real API, not in CI) | `make evals` |
| Everything CI runs | `make check` |
| Deploy to the Pi / tail logs | `make deploy PI_HOST=argus-pi` / `make logs` |
| CLI | `uv run argus --help` (`sync`, `list`, `serve`, `check-config`, …) |

## Architecture map
- `argus/clock.py`: the only wall-clock reader. `Clock`, `SystemClock`, `FakeClock`, local-time helpers.
- `argus/config.py`: `AppConfig` (YAML, no secrets) and `Secrets` (env / `$CREDENTIALS_DIRECTORY`).
- `argus/logging.py`: JSON logs and the redactor. Call `configure_logging(secrets=…)` at every entry point.
- `argus/collectors/`: one module per source. Fetch → typed records + raw payload hash. Never raises past its boundary.
- `argus/domain/`: pure models, merge rules, status transitions, finding application. No I/O.
- `argus/planner/`: free time, EDF scheduler, risk, calibration. Pure and deterministic.
- `argus/reminders/`: rules (what), guards (when and whether), engine (idempotent dispatch), SMS command parser.
- `argus/ai/`: Anthropic client wrapper (validation, retry, fallback, cache, budget), prompts, task modules.
- `argus/vault/`: Obsidian layout, argus regions, ownership decisions, proposals, git sync.
- `argus/notify/`: Twilio SMS, ntfy, and the router that picks a channel and records the result.
- `argus/web/`: FastAPI app factory, routes per view, Jinja templates, component macros, static assets.
- `argus/store/`: SQLite connection (WAL, FTS5), numbered SQL migrations, repository functions.
- `argus/cli.py`: the Typer CLI. `deploy/`: systemd units, installer, kiosk. `scripts/`: dev tooling.

## Data model
SQLite with WAL and FTS5. Full schema: [docs/schema.md](docs/schema.md).
`courses` classes · `meetings` class times · `items` merged assignments/quizzes/exams + status ·
`item_sources` per-source links · `posts` Piazza posts / Canvas announcements · `findings` extracted
facts + applied state · `modules` Canvas modules · `concepts` concept notes · `practice_items`
questions/flashcards · `practice_attempts` results + confidence · `exams` scope/logistics ·
`estimates` versioned, never overwritten · `time_logs` actuals · `plan_blocks` work/review sessions ·
`notifications` every send, unique idempotency key · `inbound_messages` replies · `vault_regions`
path/region/hash/commit · `proposals` pending edits to user text · `sync_runs` collector health.

## Conventions
- **Time.** Store UTC only (ISO 8601 with `Z`/offset). Convert to `America/New_York` only at
  display or when evaluating local-time rules (quiet hours, waking window). Never call
  `datetime.now()` (ruff bans it); take a `Clock`. Naive datetimes are bugs; `ensure_utc` rejects them.
- **Idempotency.** Every side effect has a key, stored with a UNIQUE constraint. Notifications:
  `item_id:rule:due_at_iso`, so a moved due date re-arms reminders. Proposals apply once by
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
- Canvas API pagination uses `Link` headers (`rel="next"`). Follow them; do not compute pages.
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
- Canvas files are linked, never copied, into the vault.

## UI rules
- Design tokens are CSS custom properties in `argus/web/static/src/tokens.css`, exposed to Tailwind via
  `@theme`. Use tokens, never raw hex. Spacing is a 4px scale. Light and dark themes follow the system,
  with a manual toggle.
- Components are Jinja macros in `argus/web/components/`. Each appears in every state on `/styleguide`.
- Every view handles **loading, empty, error, stale** ("Gradescope last synced 3h ago") and **offline**.
- WCAG 2.2 AA: zero axe violations in both themes, visible focus rings, a full keyboard path, a reduced-motion
  variant for every animation, and targets of at least 24×24px. Nothing loads from a CDN (the Pi may be offline).
- Budgets: under 150 KB gzipped JS+CSS per page, median server time under 150 ms, no layout shift on HTMX swaps.
- Add every new page to `tests/e2e/pages.py`, so it gets axe and visual coverage automatically.

## Testing rules
- **Unit** (`tests/unit`): pure logic, property tests (hypothesis), and DST/time tests with `FakeClock`.
- **Integration** (`tests/integration`): the store, collectors against fixtures (respx), vault temp repos,
  the webhook (TestClient), and the simulated-week pipeline.
- **E2E / a11y** (`tests/e2e`) and **visual** (`tests/visual`): a live server with a fake clock, driven by Playwright.
  These run in their own pytest process (the `make` targets do this). Plain `pytest` runs unit and integration only.
- **Evals** (`tests/evals`, marker `evals`): the real API. Run only with `make evals`.
- **LLM calls are mocked** at the `argus.ai.client` boundary with recorded responses from
  `tests/fixtures/ai/`. No test outside evals touches the network.
- Write domain logic tests first: merge, planner, reminders, vault edit rules, finding application.
- **Coverage floors** (`scripts/coverage_floors.py`): 95% line+branch per file in `domain/`,
  `planner/`, `reminders/`, `vault/ownership.py`, `vault/regions.py`; 85% overall.

## Definition of done
1. Tests written first for domain logic. New behavior has tests at the right layer.
2. `make check` is green locally (lint, types, gitleaks, coverage floors, e2e, a11y, visual).
3. Any new page is in `tests/e2e/pages.py`, and baselines are recorded for darwin and linux.
4. No secrets, real personal data, or real course content in the diff.
5. Non-obvious choices are in `docs/DECISIONS.md`, new quirks are in this file, and the schema change is in `docs/schema.md`.
6. A conventional commit (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), made at a green state.
