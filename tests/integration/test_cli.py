from datetime import timedelta
from pathlib import Path

import pytest
import respx
import yaml
from typer.testing import CliRunner

from argus import cli
from argus.clock import FakeClock
from argus.collectors.umdio import API as UMDIO
from argus.config import load_config
from argus.domain.models import ItemKind, Source, SourceItem
from argus.store import repo
from argus.store.db import available_migrations, open_db, transaction
from tests.conftest import FIXTURES
from tests.integration.conftest import fixture_bytes

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> Path:
    raw = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    raw["paths"] = {"data_dir": str(tmp_path / "var")}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(cli, "make_clock", lambda: clock)
    for var in [v for v in __import__("os").environ if v.startswith("ARGUS_")]:
        monkeypatch.delenv(var)
    return p


def test_db_migrate(config_path: Path) -> None:
    result = runner.invoke(cli.app, ["db", "migrate", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    n = len(available_migrations())
    assert f"schema version {n} (applied {n})" in result.output
    again = runner.invoke(cli.app, ["db", "migrate", "-c", str(config_path)])
    assert "(applied 0)" in again.output


@respx.mock
def test_sync_without_secrets_runs_only_umdio(config_path: Path) -> None:
    for code, section in (("CMSC351", "0101"), ("MATH240", "0201")):
        low = code.lower()
        respx.get(f"{UMDIO}/courses/{code}/sections/{section}").respond(
            200, content=fixture_bytes("umdio", f"{low}_sections.json")
        )
        respx.get(f"{UMDIO}/courses/{code}").respond(
            200, content=fixture_bytes("umdio", f"{low}_course.json")
        )
    result = runner.invoke(cli.app, ["sync", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "umdio" in result.output
    assert "0 new item(s)" in result.output
    again = runner.invoke(cli.app, ["sync", "-c", str(config_path)])
    assert "nothing due" in again.output


@respx.mock
def test_sync_failure_exits_2(config_path: Path) -> None:
    respx.get(url__startswith=UMDIO).respond(404)  # not retried, so the test stays fast
    result = runner.invoke(cli.app, ["sync", "-c", str(config_path), "--source", "umdio"])
    assert result.exit_code == 2
    assert "error" in result.output


def test_list_groups_by_local_day(config_path: Path, clock: FakeClock) -> None:
    cfg = load_config(config_path)
    conn = open_db(cfg.paths.db_path, clock)
    now = clock.now()  # Mon Oct 19 09:00 EDT
    with transaction(conn):
        ids = repo.sync_courses(conn, cfg, now)
        repo.upsert_source_items(
            conn,
            [
                SourceItem(
                    Source.CANVAS_ICAL,
                    "1",
                    "CMSC351",
                    "Homework 3",
                    ItemKind.ASSIGNMENT,
                    now + timedelta(hours=14, minutes=59),
                    "h",
                ),
                SourceItem(
                    Source.GRADESCOPE,
                    "g",
                    "CMSC351",
                    "HW3",
                    ItemKind.ASSIGNMENT,
                    now + timedelta(hours=14, minutes=30),
                    "h",
                ),
                SourceItem(
                    Source.CANVAS_ICAL, "2", "MATH240", "Reading", ItemKind.ASSIGNMENT, None, "h"
                ),
                SourceItem(
                    Source.CANVAS_ICAL,
                    "3",
                    "MATH240",
                    "Far away",
                    ItemKind.ASSIGNMENT,
                    now + timedelta(days=30),
                    "h",
                ),
            ],
            ids,
            now,
        )
        repo.apply_merge(conn, ids, now)
    result = runner.invoke(cli.app, ["list", "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Mon Oct 19"
    assert lines[1].startswith(
        "  23:30  CMSC351  Homework 3  (canvas+gradescope, upcoming)  [dates differ]"
    )
    assert "No due date" in lines
    assert "Far away" not in result.output


def test_list_empty(config_path: Path) -> None:
    result = runner.invoke(cli.app, ["list", "-c", str(config_path), "--all"])
    assert result.exit_code == 0
    assert result.output.strip() == "nothing due"
