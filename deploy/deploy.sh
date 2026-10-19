#!/usr/bin/env bash
# Deploy to the Pi over Tailscale SSH: build the CSS, sync the source, run the installer.
# Usage: ./deploy/deploy.sh argus-pi [--kiosk-user pi]
set -euo pipefail
HOST="${1:?usage: deploy.sh <pi-host> [installer options]}"
shift
cd "$(dirname "$0")/.."
make css >/dev/null
rsync -az --delete \
  --exclude '.git/' --exclude '.venv/' --exclude '.python/' --exclude 'var/' --exclude 'tests/' \
  --exclude '.env' --exclude 'config.yaml' --exclude '*.db' --exclude '__pycache__/' \
  --exclude 'node_modules/' --exclude '.*_cache/' --exclude 'htmlcov/' --exclude 'bin/' \
  --rsync-path="sudo rsync" ./ "$HOST:/opt/argus/"
ssh "$HOST" sudo /opt/argus/deploy/install.sh "$@"
