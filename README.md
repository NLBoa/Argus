# Argus

A personal, always-on course hub for one UMD student, running on a Raspberry Pi 4B. It pulls deadlines,
announcements, and course resources from ELMS (Canvas), Gradescope, Piazza, and umd.io, and reminds the student
by SMS or ntfy. The full guide to working on it is [CLAUDE.md](CLAUDE.md), and the guide to running it on the Pi is
[docs/runbook.md](docs/runbook.md).

## File adder

On the dashboard, More → **Add a file** sends a PDF, PPTX, DOCX, PNG, or JPG to the morning planner's Google Drive
inbox, named `<COURSE> · <what> · <type> · by <YYYY-MM-DD HH:MM> · <depth>.<ext>` so the planner knows the course,
deadline, and depth. "Done by" suggests the course's next class and its upcoming deadlines. The page is tailnet-only
and hidden until Drive is set up: one Google Cloud OAuth client, then `argus drive-auth` (setup in
[docs/runbook.md](docs/runbook.md#file-adder-google-drive), design in D074). How it handles Google data: [PRIVACY.md](PRIVACY.md).

## Claude connector (MCP)

### What it does
`argus serve-mcp` is a small, **read-only** [MCP](https://modelcontextprotocol.io) server. Claude (claude.ai chats, or
a scheduled morning task) uses it to read the deadlines Argus has already collected, for example to schedule them
in Reclaim. It never contacts Canvas or Gradescope itself. It reads the Argus database through a read-only SQLite
connection. It returns **deadlines and links only**: never grades, scores, points, descriptions, or submission
contents. Design notes: [D073](docs/DECISIONS.md).

| Tool | Returns |
|---|---|
| `list_courses()` | `courses`: `code`, `name`, and `meetings` (`kind`, `days` like `["Tue","Thu"]`, `start`/`end` local `HH:MM`, `location`) |
| `get_upcoming_work(days=14)` | `items` due in the next `days` (1–60), soonest first, **plus** work up to 7 days past due that isn't done (`overdue: true`). Also `as_of`. |
| `get_changes(since)` | `changes`: items that are new, had their due date moved, or were removed after `since` (ISO 8601 with an offset). Also `as_of`: pass it as the next `since`. |

Each item has these fields:

| Field | Meaning |
|---|---|
| `id` | Stable Argus item id (a string). The same work keeps its id across syncs. |
| `course` | Course code, e.g. `CMSC250` |
| `title` | As the source names it |
| `type` | `assignment` · `quiz` · `exam` · `project` · `event` (`slides` and `file` are reserved and never sent today) |
| `due` | ISO 8601 with the America/New_York offset (`2026-11-10T23:59:00-05:00`), or `null`. For an `event`, its start time. |
| `link` | Canvas, Gradescope, or the post the date came from |
| `source` | `canvas` · `gradescope` · `piazza` (a date announced in a post) · `manual` (entered by you) |
| `updated_at` | When Argus last saw the item created or its date moved |
| `done` | Submitted (Gradescope) or marked done in Argus. An exam or event counts as done once it starts. |
| `overdue` | Past due and not done |
| `status`, `old_due` | `get_changes` only: `new` · `changed` · `removed`, and the due date before `since` for `changed` |

How `type` is chosen:
- Exams and quizzes come from the item's kind. A Canvas calendar event whose title says exam, midterm, final, or
  quiz is already classified as an exam or quiz. "Midterm review session" stays an `event`.
- An assignment whose title names a project ("Project 2", "Proj3") becomes `project`.
- Other calendar events (office hours, review sessions) are `event`.

A caveat: with only the Canvas calendar feed (no Canvas token) and no Gradescope for a course, Argus can't see
submissions. A past-due item in that course stays `done: false, overdue: true` until you mark it done in Argus
(reply `DONE n`, or use Mark done on the dashboard). After 7 days it drops off by itself.

### Auth
The URL path holds a long random secret: `https://argus.<tailnet>.ts.net/mcp/<secret>`. Every other path is a 404.
The secret is a credential like any other: it lives in `.env` on your laptop and in
`/etc/argus/credentials/mcp_path_secret` on the Pi. It never goes in git, and it is redacted from logs (the access
log is off). Anyone who has the full URL can read your deadlines, so treat the URL as a password.

Two more checks apply. The `Host` header must be localhost or one of `mcp.allowed_hosts`, otherwise the request gets
a 421. A browser `Origin` other than localhost or claude.ai gets a 403. OAuth isn't used: claude.ai would need a full
authorization server for it (see D073).

### Run it locally
```sh
cp .env.example .env    # if you haven't yet; it is git-ignored
python3 -c 'import secrets; print("ARGUS_MCP_PATH_SECRET=" + secrets.token_urlsafe(32))' >> .env
uv run argus serve --demo        # once, to create var/demo.db with synthetic data; stop it with Ctrl-C
uv run --env-file .env argus serve-mcp --db var/demo.db    # http://127.0.0.1:8002/mcp/<secret>
```
In another terminal, open the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):
```sh
npx @modelcontextprotocol/inspector
```
Choose **Streamable HTTP**, enter `http://127.0.0.1:8002/mcp/<secret>` (the secret from `.env`), click Connect, then
**Tools → List Tools** and run each tool. The demo data's dates are fixed, so `get_upcoming_work` may come back empty
on a later day. Use `days=60`, or `get_changes` with an early `since`.

### Deploy on the Pi
Run these in a Pi shell (Pi Connect or SSH).
1. Create the secret without printing it. Older code ignores the extra file, so this is safe to do first:
   ```sh
   sudo sh -c 'umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /etc/argus/credentials/mcp_path_secret'
   ```
2. Update: `sudo /opt/argus-src/deploy/update-on-pi.sh <branch>`. It backs up first, then installs. The installer
   loads the new credential and starts `argus-mcp`. (The unit is skipped, not failed, until the secret file exists.)
3. Only after the update, add your Funnel name to `/etc/argus/config.yaml`. Older code rejects the unknown `mcp`
   key. `tailscale status` shows the name; use the full `argus.<tailnet>.ts.net`:
   ```yaml
   mcp:
     allowed_hosts: ["argus.<tailnet>.ts.net"]
   ```
   Then run `sudo systemctl restart argus-mcp` and `sudo argus-admin check-config`. The secret should show as `set`,
   with no `mcp` warnings.
4. Make it public with Tailscale Funnel on port 443, and move the dashboard to tailnet-only 8443 first.
   Step by step: [runbook → Tunnels](docs/runbook.md#tunnels-dashboard-private-connector-and-webhook-public). Then
   update `notifications.dashboard_url` and the Mac widget's `Local.xcconfig` to the `:8443` dashboard URL.
5. From outside your network (phone on cellular, or the laptop with Tailscale off):
   ```sh
   curl -s -o /dev/null -w '%{http_code}\n' https://argus.<tailnet>.ts.net/            # 404
   curl -s https://argus.<tailnet>.ts.net/mcp/<secret> -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'                       # three tools
   ```
   To print the secret on the Pi when you need it, run `sudo cat /etc/argus/credentials/mcp_path_secret`. Paste
   it directly into Claude or the Inspector, and nowhere else.

### Add it to Claude
1. claude.ai → **Settings → Connectors → Add custom connector**.
2. Name it `Argus`, and set the URL to `https://argus.<tailnet>.ts.net/mcp/<secret>`. Leave the OAuth fields empty.
3. In a chat, turn on the Argus connector (the tools menu) and ask, for example: "Use get_upcoming_work to list
   what's due this week." A scheduled morning task can call `get_changes` with the previous run's `as_of`, so it
   only touches what changed.

### Rotate the secret
Replace the file on the Pi (step 1 above), run `sudo /opt/argus/deploy/install.sh`, then edit the connector's URL in
claude.ai. The old URL stops working right away.

### Troubleshooting
| Symptom | Cause |
|---|---|
| 404 | Wrong or missing secret in the path, or a trailing path segment |
| 421 Invalid Host header | The Funnel name isn't in `mcp.allowed_hosts` (it must match `tailscale status` exactly) |
| 403 Invalid Origin header | A browser called it directly; use claude.ai or the Inspector's proxy |
| `argus-mcp` inactive, "condition failed" | `/etc/argus/credentials/mcp_path_secret` doesn't exist |
| Empty `get_upcoming_work` | Nothing is due in the window; check the dashboard, or that the worker is syncing (Health) |
