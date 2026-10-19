"""Operations: backups (snapshot, verify, 14-day prune, off-device copy, restore), the CLI
commands for them, and the worker's dead-man's-switch heartbeat."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from pydantic import SecretStr
from typer.testing import CliRunner

from argus import backup, cli
from argus.clock import FakeClock
from argus.config import Secrets
from argus.store.db import open_db
from argus.worker import Worker
from tests.conftest import FIXTURES

runner = CliRunner()


@pytest.fixture
def db_file(tmp_path: Path, clock: FakeClock) -> Path:
    path = tmp_path / "var" / "argus.db"
    conn = open_db(path, clock)
    conn.execute(
        "INSERT INTO rule_mutes (rule, muted_until, created_at) VALUES ('start_nudge', NULL, '2026-10-19T13:00:00Z')"
    )
    conn.close()
    return path


def test_snapshot_verify_prune_and_restore(db_file: Path, tmp_path: Path, clock: FakeClock) -> None:
    out = tmp_path / "backups"
    first = backup.snapshot(db_file, out, clock.now())
    assert first.name == "argus-20261019-0900.db.gz"
    backup.verify(first)
    for days in (13, 15, 40):
        old = backup.snapshot(db_file, out, clock.now() - timedelta(days=days))
        assert old.exists()
    (out / "notes.txt").write_text("not a backup")
    removed = backup.prune(out, clock.now())
    assert sorted(p.name for p in removed) == [
        "argus-20260909-0900.db.gz",
        "argus-20261004-0900.db.gz",
    ]
    assert len(list(out.glob("argus-*.db.gz"))) == 2 and (out / "notes.txt").exists()

    with sqlite3.connect(db_file) as c:
        c.execute("DELETE FROM rule_mutes")
    kept = backup.restore(first, db_file, clock.now())
    assert kept is not None and kept.exists()
    with sqlite3.connect(db_file) as c:
        assert c.execute("SELECT COUNT(*) FROM rule_mutes").fetchone()[0] == 1


def test_corrupt_backup_is_refused(tmp_path: Path, db_file: Path, clock: FakeClock) -> None:
    bad = tmp_path / "argus-20261019-0900.db.gz"
    with gzip.open(bad, "wb") as f:
        f.write(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.restore(bad, db_file, clock.now())
    assert db_file.exists()  # untouched


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_off_device_copy_mirrors_the_14_days(
    tmp_path: Path, db_file: Path, clock: FakeClock
) -> None:
    out, remote = tmp_path / "backups", tmp_path / "remote"
    remote.mkdir()
    (remote / "argus-20200101-0000.db.gz").write_bytes(b"stale")
    backup.snapshot(db_file, out, clock.now())
    (out / "scratch.tmp").write_text("x")
    backup.copy_off_device(out, str(remote))
    assert sorted(p.name for p in remote.iterdir()) == ["argus-20261019-0900.db.gz"]
    with pytest.raises(backup.BackupError):
        backup.copy_off_device(out, "--rsh=evil")


@pytest.fixture
def config_path(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, db_file: Path
) -> Path:
    raw = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    raw["paths"] = {
        "data_dir": str(tmp_path / "var"),
        "vault_dir": str(tmp_path / "vault"),
        "backup_dir": str(tmp_path / "backups"),
        "backup_remote": str(tmp_path / "remote"),
    }
    (tmp_path / "remote").mkdir()
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(cli, "make_clock", lambda: clock)
    for var in [v for v in __import__("os").environ if v.startswith("ARGUS_")]:
        monkeypatch.delenv(var)
    return p


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_backup_and_restore_commands(config_path: Path, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["backup", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "argus-20261019-0900.db.gz" in result.output and "copied off-device" in result.output
    assert (tmp_path / "remote" / "argus-20261019-0900.db.gz").exists()
    archive = tmp_path / "backups" / "argus-20261019-0900.db.gz"
    result = runner.invoke(cli.app, ["restore", str(archive), "-c", str(config_path), "--yes"])
    assert result.exit_code == 0, result.output
    assert "previous database is at" in result.output
    declined = runner.invoke(
        cli.app, ["restore", str(archive), "-c", str(config_path)], input="n\n"
    )
    assert declined.exit_code != 0


def test_backup_copy_failure_exits_nonzero(config_path: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(config_path.read_text())
    raw["paths"]["backup_remote"] = "--not-a-target"
    config_path.write_text(yaml.safe_dump(raw))
    result = runner.invoke(cli.app, ["backup", "-c", str(config_path)])
    assert result.exit_code == 1 and (tmp_path / "backups").exists()


# ---------------------------------------------------------------- heartbeat

PING = "https://hc-ping.example.test/1234abcd"


class NoSync:
    async def run_cycle(self) -> None:
        return None


async def test_heartbeat_every_five_minutes_and_fail_on_crash(
    tmp_path: Path, clock: FakeClock, app_config: object
) -> None:
    conn = open_db(tmp_path / "h.db", clock)
    secrets = Secrets(healthchecks_url=SecretStr(PING))
    async with httpx.AsyncClient() as client:
        worker = Worker(conn, app_config, secrets, clock, client, runner_factory=lambda f: NoSync())  # type: ignore[arg-type]
        with respx.mock:
            ok = respx.get(PING).mock(return_value=httpx.Response(200))
            fail = respx.get(PING + "/fail").mock(return_value=httpx.Response(200))
            await worker.tick()
            clock.advance(timedelta(minutes=1))
            await worker.tick()
            assert ok.call_count == 1
            clock.advance(timedelta(minutes=5))
            await worker.tick()
            assert ok.call_count == 2

            async def boom() -> None:
                raise RuntimeError("reminders broke")

            worker.engine.tick = boom  # type: ignore[method-assign]
            await worker.tick()
            assert fail.call_count == 1
        with respx.mock:
            respx.get(PING).mock(return_value=httpx.Response(500))
            clock.advance(timedelta(minutes=10))
            worker.engine.tick = _none  # type: ignore[method-assign]
            await worker.tick()  # a failed ping is logged, never raised


async def _none() -> None:
    return None
