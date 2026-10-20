from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus import __version__
from argus.cli import app
from tests.conftest import REPO_ROOT

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_check_config_never_prints_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_CANVAS_TOKEN", "super-secret-canvas-token")
    result = runner.invoke(app, ["check-config", "-c", str(REPO_ROOT / "config.example.yaml")])
    assert result.exit_code == 0
    assert "config ok: 2 course(s)" in result.output
    assert "canvas_token: set" in result.output
    assert "super-secret-canvas-token" not in result.output


def test_check_config_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "c.yaml"
    bad.write_text("courses: [{code: nope, section: '0101'}]")
    result = runner.invoke(app, ["check-config", "-c", str(bad)])
    assert result.exit_code != 0


def test_check_config_explains_work_windows_and_warns(tmp_path: Path) -> None:
    example = runner.invoke(app, ["check-config", "-c", str(REPO_ROOT / "config.example.yaml")])
    assert example.exit_code == 0
    assert "work windows: 2 in config.yaml (they seed the first run only" in example.output
    assert "Settings" in example.output and "argus windows --reset" in example.output
    old = tmp_path / "old.yaml"
    old.write_text(
        "schedule:\n"
        "  max_session_minutes: 90\n"
        "  work_windows:\n"
        '    - { days: [Mon], start: "18:00", end: "21:00" }\n'
        '    - { days: [Mon], start: "20:00", end: "23:00" }\n'
    )
    result = runner.invoke(app, ["check-config", "-c", str(old)])
    assert result.exit_code == 0  # deprecated keys still load (D057)
    assert "max_session_minutes is no longer used" in result.output
    assert "Mon 18:00-21:00 overlaps Mon 20:00-23:00; they are merged" in result.output
    none = tmp_path / "none.yaml"
    none.write_text("courses: []\n")
    assert "work windows: none" in runner.invoke(app, ["check-config", "-c", str(none)]).output


def test_too_many_windows_on_a_day_is_an_error(tmp_path: Path) -> None:
    busy = tmp_path / "busy.yaml"
    rows = "".join(
        f'    - {{ days: [Tue], start: "{h:02d}:00", end: "{h:02d}:30" }}\n'
        for h in (8, 10, 12, 14)
    )
    busy.write_text("schedule:\n  work_windows:\n" + rows)
    result = runner.invoke(app, ["check-config", "-c", str(busy)])
    assert result.exit_code != 0
