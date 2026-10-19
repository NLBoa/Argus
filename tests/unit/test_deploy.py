"""The deployment files keep their promises: restart policies, the argus user, localhost
binds (Tailscale does the exposing), no secrets inline, and CLI commands that exist."""

import configparser
import re
import subprocess
from pathlib import Path

import pytest

from argus import cli

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "deploy" / "systemd"
SERVICES = ["argus-worker", "argus-web", "argus-webhook", "argus-backup"]


def unit(name: str) -> configparser.ConfigParser:
    p = configparser.ConfigParser(strict=False, interpolation=None)
    p.optionxform = str  # type: ignore[assignment,method-assign]
    p.read(UNITS / name)
    return p


@pytest.mark.parametrize("name", SERVICES)
def test_service_runs_as_argus_and_is_hardened(name: str) -> None:
    svc = unit(f"{name}.service")["Service"]
    assert svc["User"] == "argus" and svc["Group"] == "argus"
    assert svc["NoNewPrivileges"] == "yes" and svc["ProtectSystem"] == "strict"
    assert svc["ReadWritePaths"] == "/var/lib/argus"
    if name != "argus-backup":
        assert svc["Restart"] == "always"
    command = svc["ExecStart"].split()
    assert command[0] == "/opt/argus/.venv/bin/argus"
    registered = {
        c.name or (c.callback.__name__ if c.callback else "") for c in cli.app.registered_commands
    }
    assert command[1] in registered
    text = (UNITS / f"{name}.service").read_text()
    assert "ARGUS_" not in text and not re.search(
        r"(?i)token|password|secret\s*=", text.split("[Service]")[1]
    )


def test_servers_bind_localhost_only() -> None:
    for name, port in (("argus-web", "8000"), ("argus-webhook", "8001")):
        cmd = unit(f"{name}.service")["Service"]["ExecStart"]
        assert "--host 127.0.0.1" in cmd and f"--port {port}" in cmd


def test_backup_timer_is_nightly_and_persistent() -> None:
    timer = unit("argus-backup.timer")["Timer"]
    assert timer["OnCalendar"] == "*-*-* 03:30:00" and timer["Persistent"] == "true"


def test_scripts_parse() -> None:
    for script in (
        "deploy/install.sh",
        "deploy/deploy.sh",
        "deploy/kiosk/argus-kiosk.sh",
        "deploy/argus-admin",
    ):
        path = ROOT / script
        assert path.stat().st_mode & 0o111, f"{script} is not executable"
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_installer_never_overwrites_config_or_credentials() -> None:
    text = (ROOT / "deploy" / "install.sh").read_text()
    assert "if [[ ! -f $ETC/config.yaml ]]" in text
    assert "LoadCredential=argus_$name:$f" in text
    assert '> "$CREDS' not in text and "rm " not in text
