# Phase 2: Design system, core views, ntfy deadline reminders

## Design plan (see docs/design.md for the token reference)
- **Subject.** One UMD student's always-on course watch. It is read at a glance on a phone between classes and on a laptop while planning,
  and from across the room on the Pi's monitor. Its job is to answer "what do I do now, and is anything slipping?"
- **Identity.** Argus is the hundred-eyed watchman, whose eyes Hera set in the peacock's tail. Palette: peacock teal for the
  brand and interaction, a bronze "now" needle, and deep navy ink on cool porcelain. Dark mode is night blue, not black.
- **Type.** Atkinson Hyperlegible Next, a single family. It was designed for legibility, which suits a room-distance kiosk and AA readability.
  Tabular figures go on every time and count. Headings use weight and tight tracking, not size alone.
- **Hero.** The day rail: a vertical time ruler from the start of the waking window to its end. Classes are solid blocks in course accents,
  planned sessions are outlined blocks, and the bronze needle marks now. Everything else stays quiet.
- **Structure.** Lists are rows with hairline separators, not a wall of cards. Radii vary by role: chips are pills, panels 10px, rows square.
  Shadows appear only on overlays (palette, toast). No all-caps eyebrows, no numbered markers, sentence case throughout.
- **Review against the brief.** My first instinct was an Inter + indigo SaaS card dashboard, which is generic. I changed it to a single
  hyperlegible family, the peacock and bronze palette from the Argus myth, and a timetable-instrument layout where the rail is the hero.

## Scope
1. Assets: self-hosted font, Lucide icon sprite, htmx, Alpine (vendored, pinned, nothing from a CDN at runtime).
2. Tokens (`static/src/tokens.css`) with light, dark, and manual toggle; a contrast test enforces AA on every token pair used for text and UI.
3. Components as Jinja macros (`web/components/`), all shown in every state on `/styleguide`.
4. Views: Today, Upcoming, Health, Kiosk. States: loading (skeleton during HTMX refresh), empty, error with retry,
   stale ("Gradescope last synced 3h ago"), offline (service worker plus banner). Keyboard: `g t`, `g u`, `g h`, `/`, `j`/`k`, and the ⌘K palette.
5. Migration 0002: `notifications`, `time_logs`, `rule_mutes`. Timer start and stop, and mark done, on Today.
6. Reminders: pure rules (due 24h, due 3h, new item batched, system alert) and pure guards (quiet hours, class time, daily cap,
   idempotency, length), an engine, ntfy channel, router, and an `argus worker` loop (sync and reminders every minute).
7. Demo seed (`argus demo`), so the dashboard can be previewed without credentials. The e2e server uses the same synthetic data.

## Tests first
- `tests/unit/test_rules.py`, `test_guards.py`: every rule and guard, DST days, midnight, due inside quiet hours, due during class,
  the cap excluding 3h and at-risk, the 160-character limit. Property: reminders never duplicate across a simulated week.
- `tests/unit/test_risk.py`, `test_viewmodels.py`, `test_tokens_contrast.py`.
- `tests/integration/test_reminder_engine.py` (fake ntfy via respx, idempotency, retry of failed sends),
  `test_web_views.py` (TestClient: every view in every state).
- `tests/e2e`: today timer, mark done, keyboard navigation, palette, offline banner. a11y and visual: every page at
  390, 1280, and kiosk 1920, in both themes.

## Risks
- Visual flakiness from fonts and time: the server runs on a frozen clock, the font is self-hosted, and animations are disabled in snapshots.
- At-risk needs the planner (Phase 4). Until then, risk uses only time to due and status. The rule interface is already final.
