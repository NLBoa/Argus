#!/usr/bin/env bash
# Start Chromium in kiosk mode on the Pi's monitor once the dashboard answers.
# Autostarted from the desktop session (see install.sh --kiosk-user and docs/runbook.md).
set -euo pipefail
URL="${ARGUS_KIOSK_URL:-http://127.0.0.1:8000/kiosk}"
for _ in $(seq 1 120); do
  curl -fsS -o /dev/null http://127.0.0.1:8000/healthz && break
  sleep 2
done
BROWSER="$(command -v chromium-browser || command -v chromium)"
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=Translate --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 "$URL"
