# Design reference

**Subject.** One student's always-on course watch. It is glanced at on a phone between classes, used on a laptop while
planning, and read from across the room on the Pi's monitor. It answers "what do I do now, and is anything slipping?"

**Identity.** Argus is the hundred-eyed watchman: calm, nocturnal, precise. Ink is deep navy on cool porcelain
(`--paper`); dark mode is night blue, not black. Boldness is spent in one place: the next task on Today, set large in
the display face. Everything around it stays quiet.

**Color roles (D065).**
- Teal (`--brand`) means four things only: now and next, the primary action, the active nav, focus.
- Bronze (`--needle`) is the "now" needle and nothing else (the mark's needle is the same one).
- Links are ink with an underline.
- Course color is used once: a 3 px stripe on rows. Elsewhere the course code is muted text; the chip is for item,
  exam and course headers.
- Status is an icon plus a label, never color alone, and the fill gets heavier with severity: creeping (no fill),
  at risk (soft fill), overdue (solid), "Did you submit?" (neutral outline). The hues pass the dataviz validator's
  normal-vision and colorblind separation in both themes.

**Type (D066).** Atkinson Hyperlegible Next for body and data; times and counts use tabular figures (`.tnum`), and the
slashed zero keeps 0 and O apart. Bricolage Grotesque, the display face, is for page titles, section titles, and big
numbers (the timer, stats, the kiosk clock). The scale is a perfect fourth: 12.5 / 14 / 16 / 21 / 28 /
37 / 50, and 96 on the kiosk. Both faces are self-hosted, preloaded, and swap in over metric-matched local fallbacks.

**Mark (D068).** The wordmark is "argus" in the display face, the g's ear a bronze needle; the square mark is the
"a" plus the needle, light on the night-blue tile. The header shows the wordmark alone. Both live in
`components/brand.html`; `uv run python scripts/make_icons.py` re-renders the favicon and app icons from it.

**Structure.** Lists are rows with hairline separators, not cards. A row is the title, a muted line (course, the kind
unless it's an assignment, "~4 h"), the due date and relative time, and a marker only when something is wrong.
Row controls show on hover or focus, or sit in a "..." menu on touch (D064). Panels hold groups. Radii vary by role
(pills for chips, 10px for panels, 8px for controls, none for rows). Shadows appear only on overlays. Labels are
sentence case; no all-caps eyebrows, no decorative numbering, no middle-dot meta strings.

**Navigation (D067).** Text-only header nav: Today, Upcoming, Exams, and "More". Phones get four tabs. Under the
header, a status line carries quiet notes ("Gradescope 3h behind", "1 note edit to review", offline); full banners
are only for real errors.

**Charts.** Hand-built SVG with HTML text, so labels never scale. One hours axis, never two. The workload chart is two
cumulative step lines (window time neutral, work due teal) with the shortfall shaded and labelled; a legend, a
per-day readout on hover or with the arrow keys, and a table toggle. Forced colors get dashes and a hatch.

**Tokens.** `argus/web/static/src/tokens.css` holds color, course accents, type, elevation, and motion. Edit the light
block and the `[data-theme="dark"]` block; `uv run python scripts/gen_tokens.py` writes the dark media-query copy and
the font fallbacks. Tailwind gets the tokens through `@theme inline` in `app.css`.
`tests/unit/test_tokens_contrast.py` enforces AA (4.5:1 for text, 3:1 for UI edges) in both themes, the status hue
separation, and that the generated blocks are current.

**States.** Every view renders data, empty (an invitation to act), error (what happened, plus Retry), stale (a status
note such as "Gradescope 3h behind"), offline (a status note, with the service worker serving the last copy), and
loading (the top progress bar and dimmed content during HTMX navigation).

**Writing.** Name things by what the student sees ("Mark done", "Sync now"). Toasts echo the action ("Marked
Homework 3 done"). Errors say what happened and what to do, and never apologize. Keep each line to one job.
