"""`argus discover`: the ELMS feed, Gradescope, and Piazza lists become a courses block."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
import yaml
from typer.testing import CliRunner

from argus import cli
from argus.collectors import gradescope, piazza
from argus.config import load_config
from tests.conftest import FIXTURES
from tests.unit.test_discover import FEED

FEED_URL = "https://umd.instructure.com/feeds/calendars/user_FAKEFEED.ics"
runner = CliRunner()


class FakeGS:
    def login(self, email: str, password: str) -> None:
        return None

    def courses(self) -> list[tuple[str, str, str]]:
        return [
            ("900", "CMSC216 Intro to Computer Systems", "Spring 2026"),
            ("901", "CMSC216 Intro to Computer Systems", "Fall 2026"),
            ("777", "MATH141 Calculus II", "Fall 2025"),
        ]


class FakePiazza:
    def login(self, email: str, password: str) -> None:
        return None

    def classes(self) -> list[tuple[str, str, str]]:
        return [
            ("pz216", "CMSC 216 Computer Systems", "Fall 2026"),
            ("pz250", "CMSC250 Discrete Structures", "Fall 2026"),
        ]


class BrokenPiazza(FakePiazza):
    def login(self, email: str, password: str) -> None:
        raise RuntimeError("piazza changed")


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text((FIXTURES / "config.yaml").read_text())
    for var in [v for v in __import__("os").environ if v.startswith("ARGUS_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("ARGUS_CANVAS_ICAL_URL", FEED_URL)
    monkeypatch.setenv("ARGUS_GRADESCOPE_EMAIL", "s@example.test")
    monkeypatch.setenv("ARGUS_GRADESCOPE_PASSWORD", "pw")
    monkeypatch.setenv("ARGUS_PIAZZA_EMAIL", "s@example.test")
    monkeypatch.setenv("ARGUS_PIAZZA_PASSWORD", "pw")
    monkeypatch.setattr(gradescope, "LibGradescopeClient", FakeGS)
    monkeypatch.setattr(piazza, "LibPiazzaClient", FakePiazza)
    return p


@respx.mock
def test_discover_builds_a_working_config(config_path: Path, tmp_path: Path) -> None:
    respx.get(FEED_URL).respond(200, content=FEED)
    result = runner.invoke(cli.app, ["discover", "-c", str(config_path), "--config"])
    assert result.exit_code == 0, result.output
    assert "ELMS feed: CMSC216, CMSC250, ENGL101" in result.stderr
    assert "Left out (earlier terms): MATH140 (Spring 2026)" in result.stderr
    assert "Gradescope: CMSC216 (not followed: MATH141)" in result.stderr
    assert "Piazza: CMSC216, CMSC250" in result.stderr
    out = tmp_path / "new.yaml"
    out.write_text(result.stdout)
    cfg = load_config(out)  # what it prints is a valid config
    by_code = {c.code: c for c in cfg.courses}
    assert set(by_code) == {"CMSC216", "CMSC250", "ENGL101"}  # not last term's MATH140
    assert (
        by_code["CMSC216"].gradescope_id == "901"
        and by_code["CMSC216"].piazza_network_id == "pz216"
    )
    assert by_code["CMSC216"].canvas_id == 1111111 and by_code["CMSC216"].section == "0101"
    assert by_code["CMSC250"].section == "0201" and by_code["CMSC250"].gradescope_id is None
    assert cfg.schedule.commitments  # the rest of the config is kept
    assert "check your section on Testudo" in result.stdout  # ENGL101 had no section in the feed
    assert (
        yaml.safe_load(result.stdout)["schedule"]
        == yaml.safe_load(config_path.read_text())["schedule"]
    )


@respx.mock
def test_discover_goes_on_without_piazza_and_explains_missing_feed(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(FEED_URL).respond(200, content=FEED)
    monkeypatch.setattr(piazza, "LibPiazzaClient", BrokenPiazza)
    result = runner.invoke(cli.app, ["discover", "-c", str(config_path)])
    assert result.exit_code == 0
    assert "Piazza: skipped (RuntimeError: piazza changed)" in result.stderr
    assert result.stdout.startswith("courses:\n  - code: CMSC216")
    respx.get(FEED_URL).respond(200, content=b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
    assert runner.invoke(cli.app, ["discover", "-c", str(config_path)]).exit_code == 1
    monkeypatch.delenv("ARGUS_CANVAS_ICAL_URL")
    missing = runner.invoke(cli.app, ["discover", "-c", str(config_path)])
    assert missing.exit_code == 1 and "canvas_ical_url is missing" in missing.stderr
