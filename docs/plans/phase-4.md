# Phase 4 — AI, announcements, and planner

Core rule: **the AI judges, code decides.** The AI extracts findings, estimates effort, and words one line per plan.
Code owns matching, applying, timing, scheduling, and sending.

## 4a. AI client (`ai/client.py`, `ai/budget.py`, `ai/schemas.py`, `ai/prompts/`)
- A `Transport` protocol is the only path to Anthropic. `AnthropicTransport` wraps `anthropic.AsyncAnthropic`. Tests use
  `FakeTransport`, which replays recorded responses from `tests/fixtures/ai/`.
- `AIClient.structured(task, tier, prompt, schema)`: JSON schema from Pydantic (unsupported keywords stripped,
  `additionalProperties: false` everywhere) goes into `output_config.format`. The response is validated with Pydantic, retried
  once on validation failure, and then returns `None` so the caller takes its non-AI fallback. Refusals,
  `max_tokens` stops, API errors, and budget exhaustion all end in the fallback too.
- Responses are cached by a hash of (model, prompt version, rendered input). Usage and cost go to `ai_calls`.
- Monthly budget: if month-to-date spend ≥ budget, make no call. A system alert is sent once per month.
- Prompts are versioned files (`ai/prompts/<name>.v<N>.md` with front matter). A unit test renders each one.
  Every prompt states that Argus never drafts answers or solutions to graded work.

## 4b. Announcements → findings
- Posts with `scanned_hash != content_hash` that pass the prefilter (staff, pinned, or keyword) go to Haiku extraction.
- Code-side guards: a quote must appear in the post (normalized), or the finding is dropped. A date is kept only if the
  post contains a date expression (never invent). Dates must fall in the window [posted − 7d, posted + 200d].
- `domain/findings.py` (pure, tests first) handles matching (course, normalized title or token overlap, kind compatibility)
  and the decision table: apply / hold / link / conflict / store only. It also handles linking official records
  to announced items.
- Confirming a held date change sets a student override (`item_overrides`), which is the student's decision. Dismissing keeps
  the official date. Cancellation always needs confirmation.
- Announcements view: per course, pending findings and conflicts first.

## 4c. Estimates and calibration
- `estimates` (versioned, never overwritten). Sonnet runs when an item is new or its input hash changes. The fallback is
  the median of past actuals for (course, kind), or a per-kind default marked "typical".
- `planner/calibration.py` (pure): m = (n·r̄ + k)/(n + k), k = 3. A user override always wins.
- Estimate detail view `/items/{id}`: breakdown, reasoning, assumptions, past actuals, override, re-run, log time.

## 4d. Planner
- `planner/freetime.py`: waking window − (class + walking buffer) − commitments, per day, from now.
- `planner/scheduler.py` (pure): earliest deadline first, chunks of 30 to 120 minutes on a 15-minute grid, sized from the calibrated p80 minus time logged.
- `planner/risk.py`: at risk when free hours before the due time < remaining p80 (the spec rule).
- Replans only when the inputs' hash changes, or hourly, which keeps blocks stable for start nudges.
- Workload view (14 days: needed vs free), planned sessions on Today's rail, start-nudge and at-risk rules.

## Tests first
Calibration math; free time (DST, midnight, buffers); scheduler properties (no double booking, never in
class/commitments/quiet hours, ≤ 2 h, before due, deterministic); finding decision table; quote/date guards;
AI client (retry then fallback, refusal, cache hit, budget cap + single alert, cost accounting); prompt rendering;
simulated week (fake sources → store → mocked AI → planner → reminders, fake clock).

## Gate
Extraction eval gates (`make evals`, real API) need an Anthropic key from the human. Planner property tests and
the simulated-week pipeline test run in `make check`.
