# Design reference

**Subject.** One student's always-on course watch. It is glanced at on a phone between classes, used on a laptop while
planning, and read from across the room on the Pi's monitor. It answers "what do I do now, and is anything slipping?"

**Identity.** Argus is the hundred-eyed watchman, whose eyes ended up in the peacock's tail. The mark is a peacock eye.
Peacock teal (`--brand`) is the brand and interaction color, and a bronze needle (`--needle`) marks "now". Ink is deep
navy on cool porcelain (`--paper`). Dark mode is night blue, not black.

**Type.** Atkinson Hyperlegible Next (variable, 200 to 800), self-hosted. It is the only family. Headings are weight 750 with
tight tracking. Times and counts use tabular figures (`.tnum`). The slashed zero is deliberate: it keeps 0 and O distinct.

**The hero** is the day rail (`components/timeline.html`). Classes are solid blocks in course accents,
planned sessions are dashed brand outlines, commitments are hatched, and the needle is now. Everything else stays quiet.

**Structure.** Lists are rows with hairline separators, not cards. Panels hold groups. Radii vary by role
(pills for chips, 10px for panels, 8px for controls, none for rows). Shadows appear only on overlays.
Labels are sentence case, with no all-caps eyebrows and no decorative numbering.

**Tokens.** `argus/web/static/src/tokens.css` holds color (light, dark, and the manual toggle), course accents
(stripe, background, ink), elevation, and motion. Tailwind gets them through `@theme inline` in `app.css`.
`tests/unit/test_tokens_contrast.py` enforces AA: 4.5:1 for text pairs and 3:1 for UI edges, in both themes.

**States.** Every view renders data, empty (an invitation to act), error (what happened, plus Retry),
stale (an "X last synced 3h ago" banner), offline (a banner, with the service worker serving the last copy), and loading
(the top progress bar and dimmed content during HTMX navigation).

**Writing.** Name things by what the student sees ("Mark done", "Sync now"). Toasts echo the action ("Marked
Homework 3 done"). Errors say what happened and what to do, and never apologize.
