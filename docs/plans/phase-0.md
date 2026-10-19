# Phase 0 — CLAUDE.md and repo skeleton

## Goal
A repo where `make check` is green on an almost-empty app, with every check layer
(lint, types, unit, integration, e2e, a11y, visual, coverage floors) wired so later
phases only add tests and do not have to set up tooling.

## Files
- `CLAUDE.md`: the operating manual, kept under 200 lines.
- `pyproject.toml`, `uv.lock`: Python 3.12, deps, ruff/mypy/pytest/coverage config.
- `Makefile`: setup, dev, css, test-unit, test-int, test-e2e, test-a11y, test-visual, evals, check, deploy.
- `.pre-commit-config.yaml`: ruff, mypy, gitleaks.
- `.github/workflows/ci.yml`: `make check` on x64, plus an arm64 job.
- `config.example.yaml`, `.env.example`.
- `argus/clock.py`, `argus/config.py`, `argus/logging.py`, `argus/cli.py`, and a minimal `argus/web/app.py`.
- `scripts/fetch_tailwind.sh` (a pinned, checksum-verified standalone CLI) and `scripts/coverage_floors.py`.
- `docs/DECISIONS.md`, `docs/schema.md` (stub), `docs/runbook.md` (stub).

## Tests first
- `tests/unit/test_clock.py`: the system clock is UTC-aware; the fake clock advances; local conversion across DST.
- `tests/unit/test_config.py`: example config loads; secrets come from env and from `$CREDENTIALS_DIRECTORY`; validation errors.
- `tests/unit/test_logging.py` (security): secrets, phone numbers, and iCal feed tokens are redacted.
- `tests/integration/test_app.py`: `/healthz` returns ok.
- `tests/e2e/test_smoke.py`, `tests/e2e/test_a11y.py`, `tests/visual/test_snapshots.py`: the placeholder home page, to prove the harness works.

## Risks
- Visual baselines differ between macOS and Linux font rendering. Baselines are stored
  per platform, and Linux baselines are generated in the Playwright Docker image (see DECISIONS).
- The `datetime.now()` ban has to be enforced mechanically: ruff `TID251` banned-api, allowed only in `clock.py`.
