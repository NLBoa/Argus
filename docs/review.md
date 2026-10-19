# Argus independent review

## 1. Summary

**Scope.** I read the reminder rules, guards and engine; the merge, `apply_merge`, overrides and the findings and announcements pipeline; vault ownership, regions, writer, service and git sync; the Twilio webhook and SMS command handling; the notify router and channels; logging and redaction; the worker loop; the collectors (iCal, Canvas API, Gradescope, runner); the web app's state-changing routes; and the key templates and JS.

**What I ran**
- `uv run pytest -q tests/unit tests/integration`: 856 passed, 1 skipped (the live smoke test).
- `pytest tests/e2e -m a11y`: 62 passed.
- `gitleaks git`: 8 commits scanned, no leaks.
- A TestClient timing pass on demo data: medians of 2–5 ms per main view on a dev Mac.
- Throwaway scripts in my scratchpad, against temp DBs, to confirm each finding marked "verified".

I modified no repository files.

**Overall.** The engineering is careful and well tested. Idempotency, strict region parsing, the proposal mechanics and webhook authentication are all solid. The serious problems sit where subsystems meet:
- **A post-sourced date can permanently beat the official date.** A confirmed override is sticky, and an SMS "OK" can apply the wrong finding.
- **Frontmatter rewrites destroy user-written YAML.**
- **Deadline alerts can be lost permanently.** This happens after a ~3-minute delivery outage, or when the reminder step is starved by slow AI or network steps.
- **A silently empty Gradescope listing removes items,** and their reminders with them.

## 2. Findings

### Critical

**R1 — A confirmed post date overrides Canvas forever; later official changes are silenced**
- **Where:** `argus/store/repo.py:387-390` (apply), `repo.py:1477` (`COALESCE(excluded.due_at, due_at)`), set by `argus/announcements.py:198`.
- **What's wrong:** Once the student confirms a "Dates disagree" finding, `item_overrides.due_at` replaces the resolved due date on every merge, with no end condition.
  - `resolve_item` computes `date_conflict` only from the source records. With a single Canvas record the item then shows no conflict.
  - If Canvas later moves the date, the override still wins: no conflict is shown, and reminders key on the override date.
  - Confirmed cancellations work the same way (`if cancelled: continue`). The item stays hidden even if Canvas later re-dates it.
- **Failure scenario:**
  1. Canvas has Quiz 2 due in 3 days. The student OKs a post's date, 2 days after that.
  2. Canvas then moves the quiz to tomorrow.
  3. The item stays at the override date with `due_source=manual` and `date_conflict=()`.
  4. Snapshot candidates contain no due_24h or due_3h for the real deadline.
- **Verified:** by script (`apply_merge` after an official change). The output was "item due 2026-10-24 … source manual conflict ()" against an official 2026-10-20.
- **Fix:**
  - Store the official date the override was made against.
  - When any official record's date changes afterwards, drop the override (or re-raise a conflict and text it).
  - Never let an override hide a newer official date.
  - Apply the same rule to cancellations: an official record that changes after the cancellation un-hides the item.

**R2 — SMS "OK n" / "NO n" applies the *oldest* waiting finding, not the one just texted**
- **Where:** `argus/sms.py:220` (`f = waiting[-1]`). `repo.list_findings` orders by `created_at DESC`.
- **What's wrong:**
  - Because of the DESC ordering, `[-1]` is the oldest open finding for the item.
  - The reply ("Using the announced date for Midterm 1") never states which date was applied, so the student cannot notice.
  - `domain/findings.py:248` returns CONFLICT before checking trust, so a *student's* speculative post also creates a conflict. That conflict is texted at priority 4 (see R11).
  - Combined with R1, the wrong date becomes permanent.
- **Failure scenario:**
  1. A student post yesterday says "midterm Nov 10?". That creates conflict F1.
  2. Today staff post "moved to Nov 5" (F2), and Argus texts about F2.
  3. The student replies "OK 1". F1 is confirmed and the midterm is set to Nov 10.
- **Verified:** by script. The result was `{2: 'conflict', 1: 'confirmed'}`, with the item due 2026-11-10.
- **Fix:**
  - Record the finding id in the notification (or `inbound_messages`) context, and resolve OK/NO against that exact finding.
  - Include the applied date in the reply.
  - Refuse to act when more than one finding is waiting and none is pinned by context.

**R3 — Frontmatter updates rewrite the student's YAML without a proposal**
- **Where:**
  - `argus/vault/ownership.py:115` (`_safe_rewrites`) calls `regions.update_frontmatter` (`regions.py:133-138`).
  - That function re-serializes the whole block with `yaml.safe_dump`.
- **What's wrong:** When Argus-owned keys are unchanged since Argus wrote them, it rewrites the entire frontmatter. This deletes YAML comments and reformats the student's own keys: flow lists become block lists, and folded scalars are re-quoted with a trailing newline. This breaks the "never edits user-written text" non-negotiable. It happens routinely, because Argus-owned keys change often: `due` on assignment notes, `date` on `Review/Today.md`, `verified` on concepts.
- **Failure scenario:** The student adds `# keep: my reminder to self` and `aliases: [hw1, "Homework 1"]` to an assignment note. Canvas then moves the due date. The next cycle deletes the comment and rewrites the aliases, with no proposal.
- **Verified:** by script calling `plan_note`. The action was `rewrite`, and the comment line was gone from the output.
- **Fix:**
  - Edit only Argus-owned keys, line by line (or with a round-trip YAML library such as ruamel), leaving all other bytes unchanged.
  - Alternatively, if the non-owned part of the block would change, turn the change into a proposal.
  - Add a test that the bytes outside owned keys are unchanged.

### High

**R4 — Delivery retries are used up in ~3 minutes; the deadline alert is then lost for good**
- **Where:** `argus/store/repo.py:936` (`max_attempts=3`), `argus/reminders/engine.py:258` (retry once per tick, no backoff).
- **What's wrong:**
  - The key is claimed on the first attempt, so the rules never produce that reminder again.
  - After 3 failed ticks (about 3 minutes), the notification stays `failed` forever.
  - No system alert covers failed notifications.
  - Retries also bypass the guards (quiet hours, mute, snooze).
- **Failure scenario:** HW due in 2 h. The Pi's Wi-Fi or router is down for 3 minutes as the due_3h warning fires. Delivery works again from minute 4, but the warning is never sent.
- **Verified:** by script with a router that fails 3 times and then succeeds, over 30 ticks: `send attempts: 3 [('due_3h','failed',3)]`.
- **Fix:**
  - Retry with backoff until `expires_at` rather than a fixed count; for due_3h, until due.
  - Raise a system alert when a deadline notification fails.
  - Re-check the guards on retry.

**R5 — Deadline reminders run behind AI and network steps in a single sequential tick**
- **Where:** `argus/worker.py:89-97`, `argus/ai/client.py:87` (`timeout=120, max_retries=2`).
- **What's wrong:** The reminders step runs only after sync, announcements, estimates, study and plan. Those steps make up to:
  - 20 extract calls,
  - 5 estimate calls,
  - module generation plus verification,
  - 3 targeted-practice calls,
  - and an explain call.

  Each call can take about 6 minutes when the API hangs (120 s × 3 SDK attempts). Sync is bounded at 180 s per collector, though collectors run concurrently. A degraded Anthropic API can therefore delay the reminder engine by tens of minutes to hours. That is past the due_3h window (which expires at the deadline) and the 10-minute start-nudge window. Alert timing then depends on the AI, against the non-negotiable.
- **Failure scenario:** Start of term, with 20 unscanned posts, and the API timing out. Reminders do not run for about 2 hours, so a 3-hour warning that fell due in that time is sent late or dropped.
- **Verified:** by reading the code path (step order and SDK timeout settings).
- **Fix:**
  - Run the reminder engine in its own asyncio task on its own 60-second cadence, independent of the other steps.
  - Wrap the AI steps in `asyncio.wait_for` with a per-tick budget.
  - At minimum, run reminders first and again at the end.

**R6 — An empty Gradescope listing counts as complete and removes every Gradescope-only item**
- **Where:** `argus/collectors/gradescope.py:179`, `collectors/runner.py:209-220` (`mark_absent`), `repo.apply_merge` (orphans removed).
- **What's wrong:** `gradescopeapi.get_assignments` returns `[]` instead of raising in two cases:
  - when the page has no matching rows (layout change);
  - when a 200 response is a login page (`check_page_auth` returns any 200, and the table parsers return empty).

  The collector then marks the course scope complete, marks every Gradescope record absent, and removes the items. No error is recorded (status ok), so there is no system alert either. The iCal collector has the same exposure to a valid but empty VCALENDAR.
- **Failure scenario:** Gradescope changes its course-page markup. Every Gradescope-only homework disappears, along with its due_24h and due_3h reminders.
- **Verified:** by reading the vendored `gradescopeapi/classes/account.py:119-138` and `_assignment_helpers.py:14-32, 87-98`.
- **Fix:**
  - If a scope that had present records now returns zero, record an error (or require 2 consecutive empty fetches) rather than marking all of them absent.
  - Consider a ">50% vanished" guard for every source.

**R7 — SMS item numbers move to whichever numbered message was sent last, including ntfy pushes**
- **Where:** `argus/sms.py:44-62` (`numbered`).
- **What's wrong:** `[n]` resolves against the latest sent notification with `item_ids`, on any channel.
- **Failure scenario:**
  1. 20:00: "Due tomorrow: [1] HW3".
  2. 20:05: a nudge or new-item push about [1] Project 1.
  3. 20:06: the student replies "DONE 1", meaning HW3.

  Project 1 is marked done, which silences its 24 h and 3 h reminders. HW3 stays open.
- **Verified:** by script. The reply was "Marked [1] CMSC351 Project 1 done", and the status was `Project 1: submitted`.
- **Fix:**
  - Number items stably per day (or globally) so the same item always has the same number.
  - Or resolve against the most recent *SMS* only, and refuse when another numbered text was sent within N minutes before the reply.
  - Consider including a short item code in texts.

### Medium

**R8 — The daily text cap is shared with non-deadline rules and can drop the 24-hour warning**
- **Where:** `argus/reminders/guards.py:55`, `rules.py` (DUE_24H `counts_toward_cap=True`, `expires_at=due-3h`).
- **What's wrong / scenario:** Six capped texts go out by mid-afternoon: brief, nudges, new items, announced. Something due at 09:00 tomorrow has its 24 h window opening at 09:00 today. It is deferred for the cap until midnight, then by quiet hours until 08:00, and it expires at 06:00, so it is dropped. Only the 3 h warning arrives (at 06:00).
- **Verified:** by reading the guard order and expiry.
- **Fix:** Exempt due_24h from the cap, or reserve cap slots for deadline rules.

**R9 — Keyboard focus is lost after the "Done" and timer HTMX swaps**
- **Where:** `argus/web/components/ui.html:69-80` (row `outerHTML` swap; the buttons have no `id`).
- **What's wrong / scenario:** Tab to "Done" and press Enter. Focus goes to `<body>`, and the next Tab restarts at the top of the page. This breaks the "full keyboard path" target (WCAG 2.4.3). axe does not catch it.
- **Verified:** with Playwright against a temp-DB server: before, focus was on the button; after, `BODY`.
- **Fix:** Give the buttons stable ids (htmx restores focus by id), or refocus the row's button in `htmx:afterSettle`. Add an e2e assertion.

**R10 — The "never draft solutions to graded work" rule is enforced only by prompts**
- **Where:**
  - `argus/collectors/canvas_modules.py` passes every module File and Page into `study.generate_module`.
  - `generate.v1.md:19` asks for "a worked example … from the sources".
  - `targeted_practice` feeds raw Inbox lines to the generator.
- **What's wrong / scenario:**
  - A module containing `HW3.pdf`, or an Inbox line pasting a homework question, goes to the generator.
  - Nothing in code excludes graded material.
  - The verifier checks only support against the sources, not graded-work overlap.
- **Verified:** by reading the code. I found no deterministic filter.
- **Fix:**
  - Exclude materials whose titles match tracked items or graded-work patterns (hw, homework, project, quiz, exam, solutions).
  - Drop Inbox lines that match item titles.
  - Add a code-side similarity check of generated practice against item descriptions.

**R11 — Conflicts from untrusted (student) posts are texted as override prompts**
- **Where:** `argus/domain/findings.py:248` (CONFLICT before the trust check), `engine.py:151` (only `pending` is filtered).
- **What's wrong:** Any student's Piazza guess at a quiz or exam date triggers a priority-4 "Dates disagree … Reply OK 1 to use the post's date" text. Every other untrusted path uses HOLD, which is not texted.
- **Verified:** by reading the decision table (exercised in R2's script).
- **Fix:** For non-staff or low-confidence posts, HOLD (dashboard only) instead of texting a conflict.

### Low

**R12 — Quiet hours with `start == end` defer every non-3h reminder forever.** `config.py:36` (`end <= start` counts as wrapping, so `contains()` is always true). Setting `00:00–00:00` to mean "no quiet hours" silences everything except due_3h. Fix: add a validator.

**R13 — uvicorn loggers bypass the redactor and JSON formatter.**
- `uvicorn.run` installs `uvicorn` and `uvicorn.access` handlers with `propagate: false`.
- Unhandled route exceptions ("Exception in ASGI application" tracebacks) are therefore logged unredacted.
- Verified via `uvicorn.config.LOGGING_CONFIG`.
- Fix: pass `log_config=None` (or a config using `RedactingFilter`) in `cli.py:199/283`.

**R14 — The public webhook parses an unbounded form body before the signature check.**
- `webhook.py:85` calls `await request.form()`.
- Multipart file parts are spooled to disk with no size limit, so on the Pi's SD card this is a disk and CPU DoS on the one internet-facing route.
- Fix: require `application/x-www-form-urlencoded`, cap Content-Length (e.g. 16 KB), then parse.

**R15 — No-JS error paths return raw JSON.**
- `actions.py:329` (stale or failed approve) and the 409/422 `HTTPException`s on plain forms render `{"detail": …}`.
- They do not render the error state or 303 back as the UI rules require.
- Fix: add an exception handler that renders an error page, or redirect with a flash message.

**R16 — An AI-interpreted SMS can mark an item done.**
- `sms.py:137`: a confidence ≥ 0.8 reading of free text ("ugh I'm done with this") becomes DONE.
- That silences deadline reminders on the AI's judgement. The reply does show "Read as DONE 1".
- Fix: never execute DONE or OK from an AI reading without an explicit confirmation reply.

## 3. Untested paths worth covering
- Override or cancellation, followed by a later official date change (R1).
- Two waiting findings for one item, answered by SMS OK/NO (R2). Also: the reply should state the applied date.
- Frontmatter with comments, flow lists or block scalars, plus an Argus-key change: the bytes outside owned keys must be unchanged (R3).
- A delivery outage longer than 3 ticks, and the retry/guard interaction (R4).
- Worker tick latency with a slow or hanging fake transport: reminders must still run within 60 s (R5).
- A collector returning an empty listing for a scope that previously had records (R6, and the iCal equivalent).
- `numbered()` when another numbered push lands between a text and its reply, including ntfy-only sends (R7).
- Cap interplay: deadline reminders deferred by the cap across quiet hours (R8).
- Focus retention after HTMX swaps (R9).
- Webhook paths: non-LookupError exceptions inside command handling (currently a 500, and the inbound message is not recorded), and oversized or multipart bodies.
- `TimeWindow` with `start == end`.

## 4. Things that look solid
- **Notification idempotency:** claim-before-send with UNIQUE `notification_keys`, multi-key atomic claims, and keys that include the due date, so a moved date re-arms reminders. Guards are pure and well tested.
- **Region safety:** region parsing is strict (duplicate, nested or unterminated markers become user-owned). Damaged or missing regions are never re-inserted without approval. Proposals apply only on an exact base-hash match, and declined proposals are not re-proposed.
- **Vault boundaries:** Inbox is read-only. Vault cycles are serialized by flock. Path names are sanitized against traversal.
- **Git sync:** never force-pushes; on conflict it aborts and keeps the remote; it alerts after 3 errors.
- **Webhook:** HMAC-SHA1 over the configured URL with `compare_digest`, a sender allowlist, MessageSid replay protection, serialized handling, and Twilio keywords ignored. The AI's command reading is re-parsed through the rule parser.
- **AI wrapper:** schema validation, one retry, budget cap with an alert, cache revalidation, and every prompt carries the graded-work sentence. No AI call bypasses `structured()`.
- **Redaction and secrets:** the redactor covers registered secrets, feed tokens, phone numbers and query tokens. gitleaks finds no secrets in history; test fixtures use 555 numbers.
- **Dashboard hardening:** cross-origin POST refusal, a tight CSP and frame-deny, and the dashboard bound to 127.0.0.1 behind `tailscale serve`.
- **Quality targets:** axe passes on every page in both themes (62 tests), view medians are well under budget, and JS+CSS is about 160 KB raw (well under 150 KB gzipped).

## 5. Resolution (2026-10-19)

Every finding was fixed, with a regression test. The rationale for judgment calls is in DECISIONS.md, D053.

| ID | Fix | Test |
|---|---|---|
| R1 | Overrides record their official basis and are dropped when an official date changes | `test_announcements.py` (override yields; cancellation undone) |
| R2 | OK/NO resolves to the texted finding, or refuses; replies state the applied date | `test_webhook.py::test_ok_answers_the_finding_that_was_texted` |
| R3 | Line-level frontmatter edits; unsafe cases become proposals | `test_vault_regions.py` (bytes kept; flow mapping → proposal) |
| R4 | Retries with backoff until expiry, through the guards; heartbeat `/fail` on broken delivery | `test_reminder_engine.py` (20-minute outage; quiet-hours retry); `test_ops.py` |
| R5 | Reminders first and last each tick; time budgets on sync and AI steps | `test_ops.py::test_reminders_never_wait_for_a_hung_ai_step` |
| R6 | Suspicious empty or mass-vanish listings are kept, reported, refetched, and believed after 24 h | `test_sync.py::test_empty_listing_keeps_items_until_it_persists_a_day` |
| R7 | SMS-only numbering; ambiguous `[n]` refused | `test_webhook.py::test_numbers_come_from_texts_and_ambiguity_is_refused` |
| R8 | Last 2 cap slots kept for 24-hour warnings | `test_guards.py::test_last_cap_slots_are_kept_for_24_hour_warnings` |
| R9 | Stable button ids, so htmx restores focus | `tests/e2e/test_flows.py::test_keyboard_focus_stays_on_the_button_after_a_row_swap` |
| R10 | Deterministic graded-work filter on materials and inbox lines | `test_study_verification.py`, `test_study_vault.py::test_graded_work_never_feeds_generation` |
| R11 | Untrusted conflicting posts are HOLD, not texted | `test_findings.py` decision table |
| R12 | `TimeWindow` start ≠ end | `test_config.py` |
| R13 | `log_config=None` on every `uvicorn.run` | `test_logging_redaction.py::test_servers_log_through_the_redactor` |
| R14 | URL-encoded bodies of 16 KB or less only, before parsing | `test_webhook.py::test_oversized_or_non_form_bodies_are_refused_before_parsing` |
| R15 | HTML error page for non-HTMX requests | `test_web_study.py::test_errors_without_javascript_render_a_page` |
| R16 | AI readings of DONE, OK, NO, and SNOOZE ask first | `test_webhook.py::test_free_text_goes_to_the_llm_and_the_reply_says_how_it_was_read` |
| Also | Exceptions inside SMS commands are answered and recorded (no rerun on retry) | `test_webhook.py::test_a_crash_is_answered_and_recorded_so_retries_do_not_rerun` |
