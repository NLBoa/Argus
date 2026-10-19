# Phase 3 — Canvas tier 2 and search

## Scope
- Canvas tier 2, beyond Phase 1's assignments and submissions:
  - **Announcements**, every 30 minutes with the assignments, become `posts` (platform `canvas`, kind `announcement`).
  - **Modules with their items**, plus the **syllabus** and the **current grade**, come from a new nightly `canvas_modules` collector (03:00 local).
  - Files are linked, never copied: a module item keeps its `html_url`.
- Migration 0003 adds `modules`; grade columns on `courses`; and `search_index` (an FTS5 table: porter stemming, unicode61, diacritics removed).
- The search index is rebuilt after every sync cycle. It is small (hundreds of rows), so a rebuild is simpler and safer than incremental triggers.
- A search view (`/search`), plus a header search box that `/` focuses (⌘K stays the palette). Results are grouped by kind,
  with highlighted snippets (escaped safely).
- A Course view (`/courses/{code}`): meetings, grade, modules (with digests once Phase 5 writes them), items, announcements.
  Course chips link to it.
- **No token:** the Course view shows meetings, items, and posts; the grade and modules sections are absent (not empty boxes).
  Health shows "Not set up" for ELMS (API) and ELMS (modules).

## Tests first
- `tests/integration/collectors/test_canvas_modules.py`: modules (both the inline-items and the `items_url` pagination shape),
  syllabus, grade, an empty course, a 401, and an unchanged payload.
- `tests/integration/collectors/test_canvas_api.py`: announcements become posts, with the window and an HTML body.
- `tests/integration/test_search.py`: stemming, prefixes, course filter, snippet escaping (`<script>` in a post body),
  hostile query syntax (`"`, `*`, `NEAR`, `-`), and a rebuild after removal.
- `tests/integration/test_web_course.py`: the Course view and Search, each with and without a token. Gate: both configurations pass.
- e2e, a11y, and visual coverage for `/courses/CMSC351` and `/search?q=…`.
