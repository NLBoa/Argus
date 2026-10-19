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
