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
