# Phase 5: Study vault

The vault teaches each course for exams. It is built around practice testing and distributed practice,
which Dunlosky et al. (2013) rate high, with interleaving and self-explanation. It contains no rereading or highlighting aids.

## Order of work
1. **Ownership core (tests first; 95% coverage).** `vault/regions.py` parses and hashes `%% argus:begin <id> %%` regions.
   `vault/ownership.py` is the pure decision table. `vault/proposals.py` computes diffs, applies an approved diff exactly once,
   and expires proposals after 14 days.
2. **Store (migration 0005):** `materials` (extracted text per page or slide, never copied into the vault), `concepts`,
   `practice_items`, `practice_attempts`, `exams`, `vault_regions`, `proposals`, and `vault_runs`.
3. **Writer and git sync.** `vault/layout.py` gives paths. `vault/writer.py` routes every write through ownership.
   `vault/gitsync.py`: pull --rebase → write → commit as `Argus <argus@pi.local>` → push. On conflict it aborts and keeps
   the user's version. It never force-pushes, and it alerts after 3 consecutive failures.
4. **Materials.** The nightly `canvas_files` collector downloads module PDFs and PPTX files, extracts text per page or slide
   (pypdf, python-pptx), and discards the file.
5. **Generator and verifier.** The generator (Sonnet) writes concept notes in which every claim cites a source ref. The verifier (Opus)
   sees only the sources and the note. Code drops unsupported claims and failing questions, and records `verified_at`.
6. **Exams.** Scope, readiness per concept (untested, shaky, or solid) from attempts, a spaced interleaved review plan
   (review blocks feed the planner and reminders), and a practice exam built from verified items with answers folded.
7. **Flashcards** for the Obsidian_to_Anki plugin (syntax verified, see docs/anki.md), plus the confusion inbox.
8. **Views:** Exam prep (practice with self-grading) and Proposals (diff, approve, reject). Items get `obsidian://` links.

## Ownership decision table (exact)
| Situation | Behavior |
|---|---|
| New Argus-created note | Write freely |
| Argus region, hash unchanged | Rewrite |
| Argus region the user edited | Leave it alone; create a proposal with the diff |
| User text anywhere else | Leave it alone; propose only |
| Note or folder with `argus-edit: allow` | Edit freely; log the change in the weekly note |
| Approved proposal | Apply that exact diff once |

## Gate
The vault integration suite: temp git repos simulating the laptop and the Pi. It covers the user editing an Argus region, the user editing free text,
concurrent edits causing a conflict, an approved proposal applying once, and `argus-edit: allow`. The verifier eval gates need a key (D034).

## Status (2026-10-19)
- **Ownership core and store:** done. Unit tests cover every row of the table; migrations 0005 and 0006.
- **Writer and git sync:** done. The vault integration suite (`tests/integration/test_vault_sync.py`) passes against temp
  repos for the laptop and the Pi. It covers region edits, free-text edits, concurrent-edit conflicts, apply-once
  approval, stale and rejected proposals, `argus-edit: allow` on a note and on a folder, expiry, and error reporting.
- **Materials, generator, verifier:** done, with FakeTransport end-to-end tests (`test_study_vault.py`). Unsupported
  claims, wrong practice, and failed concepts are dropped; miscited claims are kept and marked unverified.
- **Exams, review plan, nudges, flashcards, inbox:** done (D035 to D039).
- **Views:** Exam prep (`/exams`, `/exams/{id}`, self-grading), Proposals (diff, approve inside a vault cycle,
  reject), vault status on Health, `obsidian://` links on items. Covered by e2e, axe, and visual tests in both themes.
- **Gate:** the vault integration suite is green. The verifier eval (D041) needs `ARGUS_ANTHROPIC_API_KEY` (D034).
