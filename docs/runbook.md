# Runbook

How to install, configure, expose, back up, and fix Argus on the Raspberry Pi. Commands marked
**(Pi)** run on the Pi; **(laptop)** run in your clone of this repo.

## How it runs

| Piece | systemd unit | Listens on | Reached through |
|---|---|---|---|
| Worker: sync, plan, reminders, vault | `argus-worker` | nothing | (outbound only) |
| Dashboard | `argus-web` | 127.0.0.1:8000 | `tailscale serve`: your tailnet only |
| Twilio webhook | `argus-webhook` | 127.0.0.1:8001 | `tailscale funnel` on port 8443 (or Cloudflare Tunnel): public |
| Nightly backup | `argus-backup.timer` | | 03:30 every night |
| Kiosk | Chromium autostart | | `http://127.0.0.1:8000/kiosk` on the Pi's monitor |

Files on the Pi:
- `/opt/argus`: code and virtualenv.
- `/etc/argus/config.yaml`: settings.
- `/etc/argus/credentials/`: one file per secret, root-only.
- `/var/lib/argus/var/`: `argus.db`, `vault/`, and `backups/`.

Use `sudo argus-admin <command>` to run any CLI command exactly as the services do (same user, folder,
and credentials). For example: `sudo argus-admin check-config`.

## Install

1. **Hardware and OS.** Raspberry Pi 4B with Raspberry Pi OS Bookworm (64-bit, desktop if you want the kiosk). Prefer
   booting from a USB SSD (see [Storage](#storage)).
2. **Tailscale (Pi, laptop, phone).** Install Tailscale on each and sign in to the same tailnet. On the Pi, run
   `sudo tailscale up --ssh` so you can deploy over Tailscale SSH, and allow your user passwordless `sudo` (deploy needs it).
3. **Deploy (laptop).** Run `make deploy PI_HOST=argus-pi`, adding `--kiosk-user pi` (your desktop user) to set up the kiosk:
   `./deploy/deploy.sh argus-pi --kiosk-user pi`. This does three things:
   - builds the CSS;
   - syncs the code to `/opt/argus`;
   - runs `deploy/install.sh`, which installs packages and `uv`, creates the `argus` user, sets up Python and the
     database, installs and starts the units, and enables the backup timer.

   Re-running it is safe: it never overwrites your config or credentials.
4. **Configure (Pi).** Edit `/etc/argus/config.yaml`: courses (code, section, `canvas_id`, `gradescope_id`, `piazza_network_id`),
   `schedule` (waking window, quiet hours, morning brief, commitments), `notifications` (`dashboard_url`,
   `webhook_url`), `ai.monthly_budget_usd`, and `paths.backup_remote`. Then `sudo argus-admin check-config` and
   `sudo systemctl restart argus-worker argus-web argus-webhook`.
5. **Credentials.** See below, then restart the units again.
6. **Vault.** See [Vault git remote](#vault-git-remote).

## Credentials

Each secret is a file in `/etc/argus/credentials/` whose name is the secret's name. The file holds only the value.
The installer turns each file into a systemd credential. Secrets never appear in the environment, logs, or git.

```sh
sudo install -m 600 /dev/null /etc/argus/credentials/twilio_auth_token
sudo nano /etc/argus/credentials/twilio_auth_token           # paste the value, save
sudo /opt/argus/deploy/install.sh                            # regenerates the credential list
sudo argus-admin check-config                                # every secret shows set / missing, never its value
```

| File | Where to get it |
|---|---|
| `canvas_ical_url` | ELMS → Calendar → Calendar Feed (the URL is the secret) |
| `canvas_token` | ELMS → Account → Settings → New Access Token (only if UMD shows it) |
| `gradescope_email`, `gradescope_password` | Gradescope; with UMD SSO, set a password at gradescope.com first |
| `piazza_email`, `piazza_password` | Piazza |
| `twilio_account_sid`, `twilio_auth_token`, `twilio_from_number` | Twilio Console → Account info, and your verified number (E.164, `+1...`) |
| `user_phone_number` | Your phone (E.164). The only number Argus texts and the only one it takes commands from |
| `ntfy_topic` | Any long random name; subscribe to it in the ntfy app. Anyone who knows it can read your pushes |
| `anthropic_api_key` | console.anthropic.com → API keys. Without it, every AI feature uses its fallback |
| `healthchecks_url` | healthchecks.io → your check's ping URL (see [Dead-man's switch](#dead-mans-switch)) |

## Tunnels: dashboard private, webhook public

**Dashboard (tailnet only):**
```sh
sudo tailscale serve --bg 8000                 # https://argus-pi.<tailnet>.ts.net/ → 127.0.0.1:8000
```
Set `notifications.dashboard_url` to that URL. Never put the dashboard behind Funnel. Funnel exposes a whole port,
which is why the webhook runs as its own app on its own port.

**Webhook with Tailscale Funnel (recommended):**
```sh
sudo tailscale funnel --bg --https=8443 8001   # public https://argus-pi.<tailnet>.ts.net:8443/ → 127.0.0.1:8001
tailscale funnel status
```
The first time, the admin console asks you to allow Funnel for this node. Set
`notifications.webhook_url: https://argus-pi.<tailnet>.ts.net:8443/webhooks/twilio/sms`. It must match exactly what
Twilio calls, because the signature check signs that URL.

**Webhook with Cloudflare Tunnel (alternative, needs a domain on Cloudflare):** install `cloudflared` and create a
tunnel (`cloudflared tunnel login`, `cloudflared tunnel create argus`). Then expose only the webhook path:
```yaml
# /etc/cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
ingress:
  - hostname: argus-hook.example.com
    path: ^/webhooks/twilio/sms$
    service: http://127.0.0.1:8001
  - service: http_status:404
```
Run `cloudflared tunnel route dns argus argus-hook.example.com` and `sudo cloudflared service install`. Then set
`webhook_url: https://argus-hook.example.com/webhooks/twilio/sms`.

**Twilio side:** Console → Phone Numbers → Manage → Active numbers → your number → Messaging configuration → "A
message comes in": Webhook, your `webhook_url`, HTTP POST. (If the number is in a Messaging Service, set it on
the service's Integration page instead.) Test: `sudo argus-admin notify-test`, then reply `TODAY`.

Until Twilio verification finishes, leave the Twilio credentials out: reminders go to ntfy.

## SMS commands

| Reply | Does |
|---|---|
| `DONE 1` | Marks item [1] done; its reminders stop |
| `LOG 1 2h` (or `90m`, `1h30m`) | Logs time on [1]; the plan updates |
| `SNOOZE 1 2h` | No reminders about [1] for 2 hours (default 1h, up to 3 days); the 3-hour warning still comes |
| `EST 1 3h` | Sets your estimate for [1] |
| `OK 1` / `Y 1`, `NO 1` / `N 1` | Answers the question Argus asked about [1] (for example, which date to trust) |
| `TODAY` | Today's list, numbered fresh |
| `?` | The command list |

Numbers refer to the `[n]` in Argus's latest text. Anything else goes to the AI, which may only pick one of these
commands, and the reply says how your text was read. If Argus still doesn't understand, it asks once. `STOP`,
`START`, and `HELP` are handled by Twilio itself.

## Dead-man's switch

On healthchecks.io, create a check with a period of 10 minutes and a grace of 10 minutes, and add your phone or email
as an integration. Put its ping URL in `healthchecks_url`. While reminders work, the worker pings every 5 minutes, and
it pings `/fail` when the reminder step crashes. If the Pi, the worker, power, or the network goes down, the pings
stop and healthchecks.io alerts you.

## Backups and restore

Every night at 03:30, `argus backup` runs:
1. takes an online SQLite snapshot to `/var/lib/argus/var/backups/argus-YYYYMMDD-HHMM.db.gz`;
2. checks it with `PRAGMA integrity_check` and deletes files older than 14 days;
3. if `paths.backup_remote` is set, mirrors the folder there with `rsync`, so the remote also keeps 14 days.

The vault is not in these backups: its git remote is its backup.

**Off-device target:** set `backup_remote` to something like `backup@nas:/srv/backups/argus` (SSH over Tailscale),
or to a mounted USB drive path. For SSH, create a key for the `argus` user (`sudo -u argus ssh-keygen -t ed25519`,
then add `/var/lib/argus/.ssh/id_ed25519.pub` to the target) and run `sudo -u argus ssh backup@nas true` once to accept
the host key. Check it works with `sudo systemctl start argus-backup && journalctl -u argus-backup -n 20`.

**Restore:**
```sh
sudo systemctl stop argus-worker argus-web argus-webhook
sudo argus-admin restore /var/lib/argus/var/backups/argus-20261019-0330.db.gz --yes
sudo systemctl start argus-worker argus-web argus-webhook
```
Restore verifies the archive first and keeps the replaced database beside it (`argus.db.before-restore-…`). From an
off-device copy, copy the file back into the backups folder first.

## Vault git remote

1. Create a private repository for the vault, and clone it on your laptop. Open it in Obsidian with the Obsidian Git
   plugin, and with Obsidian_to_Anki if you want flashcards ([anki.md](anki.md)).
2. On the Pi, create a key and add `/var/lib/argus/.ssh/id_ed25519.pub` as a deploy key **with write access**:
   ```sh
   sudo -u argus ssh-keygen -t ed25519 -N "" -f /var/lib/argus/.ssh/id_ed25519
   ```
3. Clone it where Argus expects it:
   ```sh
   sudo systemctl stop argus-worker
   sudo -u argus git clone git@github.com:<you>/<vault>.git /var/lib/argus/var/vault
   sudo systemctl start argus-worker
   ```
Argus pulls, writes, commits as `Argus <argus@pi.local>`, and pushes every 15 minutes. It never force-pushes.
On a conflict it keeps your version. Health shows the vault status, and three failed cycles in a row text you.

## Rotating secrets

1. Create the new value at the source (new Twilio auth token, new ELMS token, new Anthropic key, new ntfy topic).
2. Overwrite the file in `/etc/argus/credentials/` and run `sudo systemctl restart argus-worker argus-web argus-webhook`.
3. Check it: `sudo argus-admin check-config`, `sudo argus-admin notify-test` (Twilio and ntfy), and
   `sudo argus-admin sync --force` (sources).
4. Revoke the old value at the source. For the Twilio auth token, promote the secondary token in the console, then
   delete the old one; the webhook rejects requests signed with a token it doesn't have.
5. If a secret leaked into git, rotating is not optional: rotate first, then clean the history.

## Storage

SD cards wear out under constant small writes (SQLite WAL, logs). Either:
- **Boot from a USB SSD (best):** use Raspberry Pi Imager to write the OS to the SSD, and set the boot order to USB
  (`sudo raspi-config` → Advanced → Boot Order).
- **Or install log2ram** on an SD card. It keeps `/var/log` in RAM and writes it out daily. Set `SIZE=128M` in
  `/etc/log2ram.conf`, and set `SystemMaxUse=50M` in `/etc/systemd/journald.conf`. The database stays on the card,
  which is why the nightly off-device backup matters.

## Kiosk

`install.sh --kiosk-user pi` adds `deploy/kiosk/argus-kiosk.sh` to `~/.config/labwc/autostart`, which is the
Bookworm default since late 2024. On a Pi still using Wayfire, add `argus = /opt/argus/deploy/kiosk/argus-kiosk.sh` under
`[autostart]` in `~/.config/wayfire.ini`. On X11/LXDE, add `@/opt/argus/deploy/kiosk/argus-kiosk.sh` to
`~/.config/lxsession/LXDE-pi/autostart`. Turn on desktop auto-login (`raspi-config` → System → Boot / Auto Login).
The kiosk page shifts its content slowly to protect the screen from burn-in.

## Updating

Run `make deploy PI_HOST=argus-pi` from an up-to-date clone. Migrations run automatically.

## Logs and common failures

`make logs PI_HOST=argus-pi` tails every unit. Or on the Pi: `journalctl -u argus-worker -f`. Logs are JSON and redact
secrets and phone numbers.

| Symptom | Likely cause and fix |
|---|---|
| Health shows a source failing, "auth_error" | Password or token changed or expired: update the credential file, restart |
| Gradescope always `auth_error` with SSO | Set a Gradescope password at gradescope.com, use that |
| No texts, ntfy works | Twilio not verified yet, wrong `twilio_from_number`, or `user_phone_number` not E.164; check `sudo argus-admin notify-test` |
| Replies get no answer | `webhook_url` doesn't match exactly (scheme, host, port, path), so the signature check fails (journal: "invalid signature"). Or the Funnel/Tunnel is down (`tailscale funnel status`). Or the reply came from another number |
| Reply answers "Not sure what you meant" | Use the command forms above; numbers refer to the latest text |
| Reminders late | Quiet hours, class time, the daily cap of 6, or a snooze hold reminders back until they can go. Health lists what was sent and when |
| healthchecks.io alert | `systemctl status argus-worker`; check power and network; `journalctl -u argus-worker -n 100` |
| Vault "Failing" on Health | Deploy key missing write access, host key not accepted, or remote moved: run `sudo -u argus git -C /var/lib/argus/var/vault pull` to see the error |
| Proposals keep coming back | That is a change you edited: approve it, or reject it (rejected changes are never proposed again) |
| AI features stopped | Monthly budget reached (Health shows spend), or no key. Everything falls back to deterministic behavior |
| Dashboard unreachable | `systemctl status argus-web`; `tailscale serve status`; the device must be on your tailnet |
| Database corrupted | Restore the latest backup (above) |
