#!/usr/bin/env bash
# Update Argus on the Pi itself, from GitHub (for Raspberry Pi Connect: no SSH from a laptop).
# It does what deploy.sh + the runbook's deploy checklist do: back up, build the CSS, check the
# config with the new code, then sync to /opt/argus and run the installer.
#
# In the Pi Connect shell:
#   curl -fsSL https://raw.githubusercontent.com/NLBoa/Argus/<branch>/deploy/update-on-pi.sh | sudo bash -s -- <branch>
# or, once a checkout exists:
#   sudo /opt/argus-src/deploy/update-on-pi.sh <branch> [--kiosk-user pi]
set -euo pipefail

BRANCH="${1:-main}"
shift || true
REPO="https://github.com/NLBoa/Argus.git"
SRC=/opt/argus-src
APP=/opt/argus
CONFIG=/etc/argus/config.yaml

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
command -v git >/dev/null && command -v rsync >/dev/null || apt-get install -y -qq git rsync >/dev/null
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

echo "==> source: $REPO ($BRANCH)"
[[ -d $SRC/.git ]] || git clone -q "$REPO" "$SRC"
git -C "$SRC" fetch -q --prune origin
git -C "$SRC" checkout -q -B "$BRANCH" "origin/$BRANCH"
echo "    at $(git -C "$SRC" log -1 --format='%h %s')"

echo "==> backup (before anything changes)"
if command -v argus-admin >/dev/null; then
  argus-admin backup
else
  echo "    no argus-admin yet (first install): nothing to back up"
fi

echo "==> stylesheet"
(cd "$SRC" && ./scripts/fetch_tailwind.sh && bin/tailwindcss -i argus/web/static/src/app.css \
  -o argus/web/static/dist/app.css --minify >/dev/null 2>&1)

echo "==> config check with the new code (secrets show as missing here: the services load them"
echo "    from /etc/argus/credentials; only the 'config ok' line matters)"
(cd "$SRC" && UV_PYTHON_INSTALL_DIR=$SRC/.python uv sync -q --frozen --no-dev --python 3.12 \
  && .venv/bin/argus check-config -c "$CONFIG") || {
  echo "config check failed: nothing was installed. Fix $CONFIG and run this again." >&2
  exit 1
}

echo "==> sync to $APP"
rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude '.python/' --exclude 'var/' --exclude 'tests/' \
  --exclude '.env' --exclude 'config.yaml' --exclude '*.db' --exclude '__pycache__/' \
  --exclude 'node_modules/' --exclude '.*_cache/' --exclude 'htmlcov/' --exclude 'bin/' \
  "$SRC/" "$APP/"

"$APP/deploy/install.sh" "$@"

echo "==> running"
systemctl --no-pager is-active argus-worker argus-web argus-webhook || true
journalctl -u argus-worker -u argus-web --since "-2 min" --no-pager -n 15 -o cat || true
echo "Done. If anything looks wrong: sudo argus-admin restore <snapshot from the backup step> --yes"
