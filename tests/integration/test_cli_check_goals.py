"""`argus check-goals`: validate goal and week files before pushing them (D079). No config,
database, or secrets needed, so it runs on the student's Mac."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from argus.cli import app
from tests.conftest import REPO_ROOT

VALID = REPO_ROOT / "tests" / "fixtures" / "goals" / "valid"
BAD = REPO_ROOT / "tests" / "fixtures" / "goals" / "bad"
runner = CliRunner()


def vault(tmp_path: Path) -> Path:
    shutil.copytree(VALID, tmp_path / "Goals")
    return tmp_path


def test_check_goals_clean_exit_0(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check-goals", str(vault(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "Woodwork: now Basic joints" in result.output
    assert "2026-W46: Woodwork 240 min, Spanish 120 min" in result.output
    assert "no problems" in result.output


def test_check_goals_accepts_the_goals_folder_itself(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check-goals", str(vault(tmp_path) / "Goals")])
    assert result.exit_code == 0, result.output


def test_check_goals_reports_line_and_exit_1(tmp_path: Path) -> None:
    root = vault(tmp_path)
    shutil.copy(BAD / "bad_status.md", root / "Goals" / "Chess.md")
    result = runner.invoke(app, ["check-goals", str(root)])
    assert result.exit_code == 1
    assert "Goals/Chess.md:12: track[1].status: must be done, current or next" in result.output


def test_check_goals_cross_file_problems(tmp_path: Path) -> None:
    root = vault(tmp_path)
    shutil.copy(root / "Goals" / "Woodwork.md", root / "Goals" / "Zwood.md")
    week = root / "Goals" / "Weeks" / "2026-W46.md"
    week.write_text(
        week.read_text()
        .replace("topic: dovetails", "topic: nope")
        .replace("  Spanish:", "  Knitting:")
    )
    result = runner.invoke(app, ["check-goals", str(root)])
    assert result.exit_code == 1
    out = result.output
    assert "Goals/Zwood.md: tag: Goals/Woodwork.md already uses the tag Woodwork" in out
    assert "Goals/Weeks/2026-W46.md: goals.Knitting: No goal file has the tag Knitting" in out
    assert "Goals/Weeks/2026-W46.md: tests[0].topic: Woodwork has no topic nope" in out


def test_check_goals_with_nothing_to_check(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check-goals", str(tmp_path)])
    assert result.exit_code == 1 and "No goal files" in result.output


def test_check_goals_from_inside_the_goals_folder(tmp_path: Path, monkeypatch: object) -> None:
    root = vault(tmp_path)
    shutil.copy(BAD / "bad_status.md", root / "Goals" / "Chess.md")
    monkeypatch.chdir(root / "Goals")  # type: ignore[attr-defined]
    result = runner.invoke(app, ["check-goals"])
    assert "Goals/Chess.md:12: track[1].status" in result.output
