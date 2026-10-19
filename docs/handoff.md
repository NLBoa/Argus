# Argus handoff

Argus is your always-on course assistant for the Raspberry Pi. This page covers what was built, how to run it,
where the tests stand, what is still missing, and what to do in your first week.

## What was built

| Area | What it does |
|---|---|
| **Collectors** | ELMS calendar feed and API (assignments, announcements, modules, files, grades), Gradescope, Piazza (with a Gmail fallback), and umd.io class times. Each source is isolated, polite (backoff, conditional requests), and health-checked. |
| **Merge** | One item per assignment across sources. The earliest official date wins, and disagreements over 5 minutes are flagged. Official sources always beat dates from posts. If you confirm a post's date and Canvas later changes it, Canvas wins again. |
| **Announcements (AI)** | Staff posts become findings: new assessments, date changes, scope, logistics. Guards drop invented dates and unquoted claims. Changes to official dates wait for your OK. |
| **Estimates and planner** | AI or default estimates, calibrated to your logged time, with your overrides on top. A deterministic earliest-deadline-first plan fits 30–120 minute sessions into your free time (waking hours minus classes, walking time, and commitments). Items that won't fit are flagged as at risk. |
| **Reminders** | Due in 24 h, due in 3 h (always sent), at risk, start nudges, new items, announced changes, exam countdowns, morning brief, and system alerts. Quiet hours, class time, a daily cap of 6 (2 slots kept for deadlines), snoozes, idempotent keys, and retries until expiry. None of it depends on the AI. |
| **SMS** | Twilio first, ntfy as the backup. Reply commands: `DONE 1`, `LOG 1 2h`, `SNOOZE 1 2h`, `EST 1 3h`, `OK 1` / `NO 1`, `TODAY`, and `?`. The signed webhook is the only public route. |
| **Study vault** | Concept notes from your course files, generated and then independently verified, with citations. Also exam prep (readiness, a spaced and interleaved review plan, a practice exam), Anki flashcards, a concept map, Today and weekly notes, and a confusion inbox. Argus never changes text you wrote without your approval (Proposals). Graded work is filtered out by code. |
| **Dashboard** | Today, Upcoming, Announcements, Workload, Exam prep, Proposals, Health, item and course pages, search, a command palette (⌘K), and a kiosk view for the Pi's monitor. Light and dark themes, keyboard-first, WCAG 2.2 AA. |
| **Operations** | systemd units, credentials as root-only files, nightly backups (14 days, copied off-device), a dead-man's-switch heartbeat, an installer, deploy script, `argus-admin`, and kiosk autostart. See [runbook.md](runbook.md). |

Design choices and their reasons are in [DECISIONS.md](DECISIONS.md) (D001–D053). The schema is in [schema.md](schema.md).

## How to run and deploy

- **Try it on your laptop with fake data:** `uv run argus serve --demo`, then open http://127.0.0.1:8000.
- **Develop:**
  - `make setup` once;
  - `make dev` for the dashboard and `uv run argus worker` for the loop;
  - `make check` runs everything CI runs.
- **Deploy to the Pi:** `make deploy PI_HOST=argus-pi` (adding `--kiosk-user pi` for the monitor). Then follow
  [runbook.md](runbook.md) for config, credentials, tunnels, the vault remote, backups, and the heartbeat.

## Test and eval results (2026-10-19, at commit `5c1cf5d`)

| Suite | Result |
|---|---|
| Unit and integration (`make cov`) | **876 passed**, 1 skipped (the live smoke test, which needs real credentials) |
| Coverage | **96.7%** overall (floor 85%). Strict areas: **99.0%** (floor 95% per file in `domain/`, `planner/`, `reminders/`, `vault/ownership.py`, `vault/regions.py`) |
| End-to-end flows (Playwright) | **26 passed**, including keyboard paths, focus after swaps, and zero CSP violations |
| Accessibility (axe, both themes, every page) | **64 passed**, 0 violations |
| Visual regression | **56 passed** on macOS and **56 passed** on Linux (the pinned Playwright container) |
| Simulated weeks | **Push:** deadlines, caps, quiet hours, and idempotency all hold. **SMS:** replies through the signed webhook, a replayed reply, a forged request, and a Twilio outage falling back to ntfy all behave correctly |
| Vault integration | Temp git repos for laptop and Pi: region edits, free text, conflicts, apply-once proposals, and `argus-edit: allow` |
| Secrets | gitleaks: no leaks in history; logs redact secrets and phone numbers |
| Weight | About **50 KB** of JS and CSS per page, gzipped (budget 150 KB). The reviewer measured view medians of 2–5 ms on a Mac; Pi numbers are still to be measured |
| **AI evals (`make evals`)** | **Not run: no API key yet** (D034). Ready to run: extraction (44 posts), verifier (22 notes, 32 seeded errors), estimates (8), and integrity (4 attempts to get answers). The scorers are tested in CI |

**Independent review.** A fresh reviewer found 16 issues, 3 of them critical. All 16 are fixed with regression tests;
see [review.md](review.md) §5 and D053.

## Known limitations

- **AI quality is unmeasured** until `make evals` runs with your key. The gates are: zero invented dates, ≥95% recall
  on assessments, every date change handled, ≥90% of seeded errors caught, and ≤10% false rejects. If a gate fails,
  the fix is a new prompt version. Until then, every AI feature has a deterministic fallback.
- **Unofficial integrations.** `gradescopeapi` and `piazza-api` can break when those sites change. Health shows it,
  and three failures in a row text you. An empty or mass-vanishing listing is not believed for 24 hours (review R6).
- **Not verified against real accounts yet:** the live smoke test (D014), the Piazza notification email format,
  whether UMD allows ELMS API tokens, and Twilio delivery (pending verification).
- **Pi performance is unmeasured.** The budgets hold on a laptop and the page weight is fixed; check timings on the Pi.
- **The kiosk autostart** targets labwc (Bookworm's default). Wayfire and X11 are documented but untested.
- **Without an ELMS token** there are no modules, files, grades, or announcements from the API, and so no
  generated concept notes. Deadlines and reminders work from the calendar feed alone.

## What you need to do (only you can)

1. **Submit Twilio verification now** (toll-free sole proprietor, or 10DLC sole-proprietor); it takes days. Until it
   clears, reminders go to ntfy.
2. Provide credentials as files on the Pi ([runbook.md](runbook.md#credentials)): the Canvas iCal URL, an ELMS
   token (if offered), Gradescope and Piazza logins, Twilio, your phone number, an ntfy topic, an Anthropic key
   with a monthly budget, and a healthchecks.io URL.
3. Fill in `/etc/argus/config.yaml`: courses (code, section, Canvas, Gradescope, and Piazza ids), waking window,
   quiet hours, morning brief time, and commitments.
4. Create a private git remote for the vault. Install Obsidian Git (and Obsidian_to_Anki if you want
   flashcards) on your laptop.
5. Install Tailscale on the Pi, laptop, and phone; then `tailscale serve` for the dashboard and `tailscale funnel` for the webhook.
6. Run `make evals` once the key exists, and record the numbers here.

## First-week checklist

- [ ] **Every day, compare** Today and Upcoming against ELMS and Gradescope. Note any missing, duplicated, or wrong-dated items
      (and check Health for failing sources).
- [ ] **Reply to texts** with `DONE`, `LOG`, and `SNOOZE` at least once each, and check the dashboard reflects them.
- [ ] **Spot-check one module's notes against its slides:** every claim's citation should point to a slide that supports it,
      and nothing should come from homework.
- [ ] **Log time on a few assignments** (timer, `LOG n h`, or the item page) so calibration starts learning how long
      things take you.
- [ ] **Edit a generated note** in Obsidian, then wait for Argus's next version to show up as a proposal. Approve or reject it.
- [ ] **Confirm the backups:** a backup file appears nightly in `/var/lib/argus/var/backups` and on your backup target.
      Try a restore on a copy once.
- [ ] **Confirm the heartbeat:** healthchecks.io shows regular pings. Unplug the Pi's network for 20 minutes to see the alert.
