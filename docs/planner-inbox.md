# Planner inbox: files and their text companions

The file adder (`/files/new`, D074) puts a file into the planner's Inbox folder in Google Drive. With each file it
also puts a **text companion** (D077): a Markdown file with the file's details in YAML frontmatter and its text,
page by page or slide by slide. A scheduled Claude task can read the companion instead of opening a PDF or a deck,
and turn it into notes in the Obsidian vault.

Argus only writes to Drive. It never writes these notes into the vault, and it never reads, moves, renames, or
deletes anything in the Inbox afterwards. Keeping track of what has been handled is the scheduled task's job (see
[Finding new files](#finding-new-files)).

## What lands in the folder

Each send makes two files, the original first:

| File | Name |
|---|---|
| The original (PDF, PPTX, DOCX, PNG, or JPG, in its own format) | `<COURSE> · <what> · <type> · by <YYYY-MM-DD HH:MM> · <depth>.<ext>` |
| Its companion (`text/markdown`) | the same name plus `.md`, so it sorts right after its file |

For example, `CMSC351 · Week 9 slides · slides · by 2026-11-10 09:30 · deep.pptx` and
`CMSC351 · Week 9 slides · slides · by 2026-11-10 09:30 · deep.pptx.md`.

A file name never contains ` · ` except as a separator. A name ending in `.<ext>.md` is always a companion, never
a file the student sent. **Anything that turns Inbox files into tasks must skip the `.md` companions**: they are a
second view of a file it has already seen, not new work.

If the companion upload fails, the file is still sent (the student sees a quiet note), so a file can arrive
without a companion. Treat that file the way you would without Argus: read the name, then the file itself.

## The companion format (schema version 1)

```markdown
---
schema_version: 1
format: "argus-planner-inbox-companion"
file: "CMSC351 · Week 9 slides · slides · by 2026-11-10 09:30 · deep.pptx"
course: "CMSC351"
title: "Week 9 slides"
type: "slides"
depth: "deep"
done_by_local: "2026-11-10 09:30"
done_by_utc: "2026-11-10T14:30:00Z"
timezone: "America/New_York"
original_name: "week9.pptx"
mime_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation"
size_bytes: 482113
drive_file_id: "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
drive_link: "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view?usp=drivesdk"
unit: "slide"
count: 3
sections: 3
extracted: "full"
extracted_reason: "all 3 slides read"
uploaded_at: "2026-11-06T15:04:05Z"
---

# Week 9 slides (CMSC351)

Text Argus extracted from CMSC351 · Week 9 slides · slides · by 2026-11-10 09:30 · deep.pptx, in reading order. Speaker notes follow their slide.

## Slide 1

Divide and conquer
Split the input, solve the parts, combine
T(n) = a T(n/b) + f(n)

### Speaker notes

Draw the recursion tree before writing the sum.

## Slide 2

_(No text on this slide.)_

## Slide 3 (hidden)

Extra examples
```

(The example is synthetic.)

**Frontmatter.** Every string is double-quoted, so no YAML reader turns a date or `no` into anything else. Numbers
and `null` are plain. Every value is on one line. Fields always appear, in this order:

| Field | Meaning |
|---|---|
| `schema_version` | `1`. A change that could break a reader raises it; new fields may be added without raising it. |
| `format` | Always `argus-planner-inbox-companion`. |
| `file` | The original's name in Drive (the companion's name minus `.md`). |
| `course` | Course code, uppercase, no space (`CMSC351`). |
| `title` | What the student said the file is (the `<what>` in the name). |
| `type` | `slides`, `notes`, `assignment`, `exam-prep`, `reading`, or `other`. |
| `depth` | `deep`, `normal`, or `skim`: how closely the student wants to go through it. |
| `done_by_local` | When the student wants it done, `YYYY-MM-DD HH:MM`, America/New_York wall time (the same as the name). |
| `done_by_utc` | The same moment in UTC, ISO 8601 with `Z`. |
| `timezone` | Always `America/New_York`. |
| `original_name` | The file's name on the student's device, or `null`. |
| `mime_type` | The original's MIME type. |
| `size_bytes` | The original's size. |
| `drive_file_id`, `drive_link` | The original in Drive, when Drive returned them; otherwise `null`. |
| `unit` | `page` (PDF), `slide` (PPTX), `document` (DOCX), or `image` (PNG, JPG). |
| `count` | Pages or slides in the file. For a DOCX, Word's own page count when it saved one; otherwise `null`. `null` for images. |
| `sections` | How many `## ` sections the body has. |
| `extracted` | `full`, `partial`, or `none` (below). |
| `extracted_reason` | One line for a reader: `all 24 pages read`, `300 pages of 512 extracted (Argus reads at most 300 pages)`, `image — read the file itself`. |
| `uploaded_at` | When Argus sent it, UTC with `Z`. |

**`extracted`.**

- `full`: every page or slide was read. Some may have no text (a picture-only slide says so).
- `partial`: Argus stopped early (300 pages or slides, 250,000 characters, or 30 seconds of reading on the Pi), or
  some pages couldn't be read. The body has what was read; open the file for the rest.
- `none`: no text at all. The reason says why: an image (Argus does no OCR), a scanned PDF with no text layer, a
  password-protected PDF, a damaged file, or a file too large to unpack safely. The body has no sections. Read the
  file itself (`drive_link`), for example with a model that reads PDFs and images.

**Body.** A `# <title> (<COURSE>)` heading, one paragraph about where the text came from, then one section per page
(`## Page n`), slide (`## Slide n`, or `## Slide n (hidden)`), or, for a DOCX, a single `## Document` (Word files
have no fixed pages). A slide's speaker notes follow it under `### Speaker notes`. Text keeps its reading order and
its line breaks; each line is trimmed, and blank lines are at most one in a row.

Only Argus writes heading lines. A line of extracted text that would start with `#`, or would be only `-`, `=`,
`*`, or `_`, gets a backslash in front (`\# not a heading`). So splitting on lines that match
`^## (Page|Slide) \d+( \(hidden\))?$|^## Document$` is safe.

## Finding new files

Argus can't see what the scheduled task has done, and with the `drive.file` scope it can't even see files it
didn't create. So the scheduled task keeps its own record. A simple way:

1. List the Inbox folder's companions, newest first: Drive query
   `'<Inbox folder ID>' in parents and name contains '.md' and trashed = false`, ordered by `createdTime desc`.
   To keep it short, add `and createdTime > '<last run, minus an hour>'` (RFC 3339, UTC).
2. Skip any companion whose `drive_file_id` (or, when that is `null`, its own Drive file id) is already in the
   task's processed list. The hour of overlap and this check together mean nothing is missed or done twice.
3. Also look for originals with no companion (an upload whose companion failed): files that don't end in `.md`
   and have no `<name>.md` beside them.
4. After writing the notes for a file, add its id to the processed list.

Keep the processed list in the vault, in the task's own folder, for example `Planner/Processed.md` with one
`<drive file id>  <file name>` per line. Argus never writes there.

## Prompt snippet for the scheduled task

Adjust the paths to your vault. This goes in the scheduled task's prompt (morning, midday, and night):

```text
Check the planner Inbox folder in Google Drive for new course files.

1. Find companions: files in the Inbox whose names end in ".md" and whose frontmatter has
   format: "argus-planner-inbox-companion". New = its drive_file_id is not listed in
   Planner/Processed.md in my Obsidian vault. Also treat as new any non-.md file with no
   "<name>.md" beside it (read that file directly).
2. For each new one, read the frontmatter (course, title, type, depth, done_by_local) and the
   body (## Page n / ## Slide n sections, ### Speaker notes). If extracted is "none" or
   "partial", open the original via drive_link for what's missing.
3. Write study notes for me in my vault at Courses/<course>/Planner/<title>.md (create the
   folder if needed). Include: a summary, the key ideas and definitions in my own course's
   terms, worked explanations of examples that appear in the material, questions to check my
   understanding, and a "Source" line linking drive_link with the page or slide numbers you
   drew on. Match the depth: deep = thorough, normal = main points, skim = a short overview.
   Link the course note as [[Courses/<course>/<course>]].
4. Never write anything else in the vault: not inside Courses/<course>/ outside Planner/, not
   in Argus/, Review/, or Weekly/, and never between "%% argus:begin" and "%% argus:end"
   markers in any note. Argus owns those and rewrites them. Don't add "argus-edit" to
   frontmatter. If a note at that path already exists and I've edited it, add a new note
   instead of changing mine.
5. Never draft answers or solutions to graded work: no homework, project, quiz, or exam
   answers, even when the file is an assignment. For an assignment, explain the concepts it
   relies on and point to where the material covers them; do not solve the problems.
6. Add each handled file's drive_file_id and name to Planner/Processed.md.
7. Skip the ".md" companions when making tasks or calendar blocks: they describe a file,
   they are not a second piece of work.
```

## Privacy

Companions hold course text, so they stay in the same private Drive folder as the files. Argus logs each
companion's name, size, and whether text was extracted, never its contents. It never logs the folder ID or
Drive's error bodies (D074).
