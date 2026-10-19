#!/usr/bin/env bash
# Install or update Argus on a Raspberry Pi (Raspberry Pi OS Bookworm, 64-bit). Run as root
# from the synced source in /opt/argus:  sudo /opt/argus/deploy/install.sh [--kiosk-user pi]
# Safe to re-run: it never overwrites /etc/argus/config.yaml or any credential.
set -euo pipefail

APP=/opt/argus
DATA=/var/lib/argus
ETC=/etc/argus
CREDS=$ETC/credentials
UNITS=(argus-worker argus-web argus-webhook)
KIOSK_USER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kiosk-user) KIOSK_USER="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -f $APP/pyproject.toml ]] || { echo "sync the source to $APP first (make deploy)" >&2; exit 1; }

echo "==> packages"
apt-get update -qq
apt-get install -y -qq git rsync sqlite3 curl ca-certificates >/dev/null
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

echo "==> user and folders"
id argus >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --create-home --shell /usr/sbin/nologin argus
install -d -o argus -g argus -m 750 "$DATA"
install -d -o root -g argus -m 750 "$ETC"
install -d -o root -g root -m 700 "$CREDS"
if [[ ! -f $ETC/config.yaml ]]; then
  install -o root -g argus -m 640 "$APP/config.example.yaml" "$ETC/config.yaml"
  echo "    wrote $ETC/config.yaml from the example: edit it (courses, schedule, URLs)"
fi

ln -sfn "$ETC/config.yaml" "$DATA/config.yaml"  # the CLI's default path, from /var/lib/argus
install -m 755 "$APP/deploy/argus-admin" /usr/local/bin/argus-admin

echo "==> python environment"
cd "$APP"
UV_PYTHON_INSTALL_DIR=$APP/.python uv sync --frozen --no-dev --python 3.12 >/dev/null
[[ -f $APP/argus/web/static/dist/app.css ]] || echo "    warning: CSS bundle missing; run 'make css' before deploying" >&2

echo "==> credentials ($(find "$CREDS" -type f | wc -l) found in $CREDS)"
for unit in "${UNITS[@]}" argus-backup; do
  dropin=/etc/systemd/system/$unit.service.d
  install -d -m 755 "$dropin"
  {
    echo "# Written by deploy/install.sh: one credential per file in $CREDS (file name = secret name)."
    echo "[Service]"
    for f in "$CREDS"/*; do
      [[ -f $f ]] || continue
      name=$(basename "$f")
      [[ $name =~ ^[a-z_]+$ ]] || { echo "    skipping $name (lowercase letters and _ only)" >&2; continue; }
      echo "LoadCredential=argus_$name:$f"
    done
  } > "$dropin/credentials.conf"
  chmod 644 "$dropin/credentials.conf"
done

echo "==> database"
runuser -u argus -- env -C "$DATA" "$APP/.venv/bin/argus" db migrate -c "$ETC/config.yaml"

echo "==> services"
install -m 644 "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now argus-backup.timer >/dev/null
for unit in "${UNITS[@]}"; do
  systemctl enable "$unit" >/dev/null
  systemctl restart "$unit"
done

if [[ -n $KIOSK_USER ]]; then
  echo "==> kiosk autostart for $KIOSK_USER"
  home=$(getent passwd "$KIOSK_USER" | cut -d: -f6)
  autostart=$home/.config/labwc/autostart
  install -d -o "$KIOSK_USER" -g "$KIOSK_USER" "$(dirname "$autostart")"
  line="$APP/deploy/kiosk/argus-kiosk.sh &"
  grep -qxF "$line" "$autostart" 2>/dev/null || echo "$line" >> "$autostart"
  chown "$KIOSK_USER:$KIOSK_USER" "$autostart"
fi

echo "==> done. Status:"
systemctl --no-pager --lines=0 status "${UNITS[@]}" argus-backup.timer | grep -E "^(●|     Active)" || true
if ! systemctl is-active --quiet log2ram 2>/dev/null; then
  echo "    tip: on an SD card, install log2ram (docs/runbook.md, 'Storage')"
fi
