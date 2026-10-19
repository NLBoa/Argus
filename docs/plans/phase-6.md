# Phase 6: Twilio and hardening

## Order of work
1. **Store (0007):** `inbound_messages` (one row per Twilio `MessageSid`, so a replayed webhook is answered once) and
   `snoozes` (item, until).
2. **Twilio channel:** `notify/twilio_sms.py` posts to the Messages API with one attempt. The engine already retries
   pending sends, and a retried POST could double-send. The router tries Twilio first and falls back to ntfy.
3. **Commands (tests first, 95% coverage):** `reminders/commands.py` is a pure rule-based parser for `DONE n`,
   `LOG n h`, `SNOOZE n 2h`, `EST n h`, `OK n`/`NO n`, `Y n`/`N n`, `TODAY`, and `HELP`. Numbers resolve against the
   item ids stored with the most recent outbound message that has that number.
4. **Inbound service:** `argus/sms.py` parses first, then falls back to the LLM (fast tier, schema-validated, acted on
   only when the interpretation is complete and confident), then asks back once. Every reply says exactly what changed.
5. **Webhook app:** a separate ASGI app (`argus serve-webhook`, its own port and systemd unit) with one route,
   `POST /webhooks/twilio/sms`. It checks `X-Twilio-Signature` (HMAC-SHA1 over the public URL and sorted params),
   checks the sender, and answers with TwiML. Only this app goes behind Tailscale Funnel or Cloudflare Tunnel; the
   dashboard stays tailnet-only.
6. **Rules:** morning brief (daily at the configured time), exam countdowns (7, 3, and 1 days, with the weakest
   concepts), and announced quiz/exam changes (with OK/NO when a date conflicts). Snoozes defer everything for an
   item except the 3-hour warning.
7. **Hardening:** CSP (resolves D017); `argus backup` (SQLite online backup, 14 kept, copied off-device with rsync);
   a dead-man's-switch ping after healthy worker ticks; systemd units with restart policies and credentials;
   `deploy/install.sh`; Chromium kiosk autostart; log2ram and SSD guidance; `docs/runbook.md`.

## Gate
- Webhook suite: valid, invalid, and missing signatures; unknown sender; every command; replay of the same message.
- Security suite: gitleaks; logs redact secrets and phone numbers; the webhook rejects unsigned requests; the dashboard
  app has no webhook route and the webhook app has nothing else.
- The simulated week with SMS: Twilio faked at the HTTP layer, and replies sent through the signed webhook.

## Status (2026-10-19)
- Done: migration 0007, Twilio channel and router, the command parser, the reply service, the separate webhook app,
  morning brief, exam countdowns, announced changes, snoozes, CSP, backups and restore, the heartbeat, systemd units,
  `install.sh`, `deploy.sh`, `argus-admin`, kiosk autostart, and the runbook (D042 to D052).
- Gate: the webhook suite (`test_webhook.py`: signatures valid, invalid, missing, and tampered; unknown sender;
  every command; replay; the AI fallback; asking back; app separation) and the security checks (gitleaks; log
  redaction of secrets and phone numbers in `test_logging_redaction.py`; unsigned requests rejected; a CSP with zero violations in e2e)
  pass. So does the SMS simulated week (`test_simulated_week_sms.py`).
- Bug found and fixed on the way: item numbers were sent as "?1?" because `[` and `]` are GSM-7 extension characters (D049).
- Needs the human: Twilio verification and credentials, the Funnel or Tunnel, a healthchecks.io check, a backup target.
