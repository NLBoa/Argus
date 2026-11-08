"""Reading Goals/ from the vault (D079): read-only, last good version kept, after the pull."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from argus.capabilities import derive
from argus.clock import FakeClock
from argus.config import AppConfig, GoalsConfig, PathsConfig, Secrets
from argus.store import repo
from argus.vault import service
from argus.vault.goals_scan import scan_goals
from argus.vault.ownership import DesiredNote
from argus.vault.writer import VaultWriter
from tests.conftest import REPO_ROOT

VALID = REPO_ROOT / "tests" / "fixtures" / "goals" / "valid"
BAD = REPO_ROOT / "tests" / "fixtures" / "goals" / "bad"


def vault_with_goals(root: Path) -> Path:
    shutil.copytree(VALID, root / "Goals")
    return root


def test_scan_stores_valid_docs(tmp_path: Path, db: sqlite3.Connection, clock: FakeClock) -> None:
    root = vault_with_goals(tmp_path / "vault")
    (root / "Goals" / "Readme.md").write_text("# Plain notes, no block\n")
    summary = scan_goals(db, root, "Goals", clock.now())
    docs = {d.path: d for d in repo.goal_docs(db)}
    assert sorted(docs) == [
        "Goals/Chess.md",
        "Goals/Spanish.md",
        "Goals/Weeks/2026-W46.md",
        "Goals/Woodwork.md",
    ]
    assert all(d.error is None for d in docs.values())
    assert docs["Goals/Weeks/2026-W46.md"].kind == "week"
    assert (summary.read, summary.problems) == (4, 0)
    assert repo.goal_tags(db) == {"Woodwork", "Spanish", "Chess"}


def test_scan_keeps_last_good_on_error(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    root = vault_with_goals(tmp_path / "vault")
    scan_goals(db, root, "Goals", clock.now())
    first = clock.now()
    clock.advance(timedelta(minutes=15))
    shutil.copy(BAD / "bad_status.md", root / "Goals" / "Chess.md")
    summary = scan_goals(db, root, "Goals", clock.now())
    chess = next(d for d in repo.goal_docs(db) if d.path == "Goals/Chess.md")
    assert chess.good is not None and chess.good_at == first
    assert chess.error is not None and chess.error.line == 12
    assert summary.problems == 1


def test_deleted_file_marked_missing(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    root = vault_with_goals(tmp_path / "vault")
    scan_goals(db, root, "Goals", clock.now())
    (root / "Goals" / "Chess.md").unlink()
    (root / "Goals" / "Spanish.md").write_text("# Block removed\n")
    scan_goals(db, root, "Goals", clock.now())
    assert repo.goal_tags(db) == {"Woodwork"}


def test_block_in_the_wrong_folder_is_an_error(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    root = vault_with_goals(tmp_path / "vault")
    shutil.copy(VALID / "Weeks" / "2026-W46.md", root / "Goals" / "Oops.md")
    scan_goals(db, root, "Goals", clock.now())
    oops = next(d for d in repo.goal_docs(db) if d.path == "Goals/Oops.md")
    assert oops.error is not None and "Weeks/" in oops.error.message


def test_duplicate_tag_is_an_error_on_the_later_file(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    root = vault_with_goals(tmp_path / "vault")
    shutil.copy(root / "Goals" / "Woodwork.md", root / "Goals" / "Zwood.md")
    scan_goals(db, root, "Goals", clock.now())
    z = next(d for d in repo.goal_docs(db) if d.path == "Goals/Zwood.md")
    assert z.error is not None and z.good is None and "Goals/Woodwork.md" in z.error.message


def test_big_files_and_symlinks_are_skipped(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    root = vault_with_goals(tmp_path / "vault")
    (root / "Goals" / "Huge.md").write_text("x" * (256 * 1024 + 1))
    outside = tmp_path / "secret.md"
    shutil.copy(VALID / "Woodwork.md", outside)
    (root / "Goals" / "Link.md").symlink_to(outside)
    scan_goals(db, root, "Goals", clock.now())
    docs = {d.path: d for d in repo.goal_docs(db)}
    assert "Goals/Link.md" not in docs
    assert (
        docs["Goals/Huge.md"].error is not None and "256 KB" in docs["Goals/Huge.md"].error.message
    )


def test_no_folder_reads_nothing(tmp_path: Path, db: sqlite3.Connection, clock: FakeClock) -> None:
    scan_goals(db, tmp_path / "vault", "Goals", clock.now())
    assert repo.goal_docs(db) == []


def test_writer_refuses_goals_folder(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    writer = VaultWriter(tmp_path, db, clock, read_only=("Goals",))
    with pytest.raises(ValueError, match="read-only"):
        writer.write("Goals/Projects.md", DesiredNote({}, (("core", "x"),), ""))
    with pytest.raises(ValueError, match="read-only"):
        writer.write("Goals/Weeks/2026-W46.md", DesiredNote({}, (("core", "x"),), ""))
    writer.write("Goalsetting.md", DesiredNote({}, (("core", "x"),), ""))  # only the folder itself


# --- a full vault cycle ------------------------------------------------------------------------


def sh(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def config_for(tmp_path: Path, goals: bool) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(data_dir=tmp_path / "var", vault_dir=tmp_path / "pi"),
        goals=GoalsConfig() if goals else None,
    )


def remote_and_laptop(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    laptop = tmp_path / "laptop"
    sh(tmp_path, "clone", "-q", str(remote), str(laptop))
    sh(laptop, "config", "user.name", "Student")
    sh(laptop, "config", "user.email", "student@example.test")
    (laptop / "README.md").write_text("My vault\n")
    sh(laptop, "add", ".")
    sh(laptop, "commit", "-qm", "init")
    sh(laptop, "push", "-q", "origin", "main")
    sh(tmp_path, "clone", "-q", str(remote), str(tmp_path / "pi"))
    return laptop


def test_cycle_reads_goals_after_pull(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    laptop = remote_and_laptop(tmp_path)
    vault_with_goals(laptop)
    sh(laptop, "add", ".")
    sh(laptop, "commit", "-qm", "goals")
    sh(laptop, "push", "-q")
    config = config_for(tmp_path, goals=True)
    result = service.run_cycle(db, config, clock, caps=derive(config, Secrets()))
    assert result.status == "ok", result.error
    assert repo.goal_tags(db) == {"Woodwork", "Spanish", "Chess"}
    pi_copy = tmp_path / "pi" / "Goals" / "Woodwork.md"
    assert pi_copy.read_text() == (VALID / "Woodwork.md").read_text()  # read, never rewritten


def test_no_goals_block_no_scan(tmp_path: Path, db: sqlite3.Connection, clock: FakeClock) -> None:
    laptop = remote_and_laptop(tmp_path)
    vault_with_goals(laptop)
    sh(laptop, "add", ".")
    sh(laptop, "commit", "-qm", "goals")
    sh(laptop, "push", "-q")
    config = config_for(tmp_path, goals=False)
    assert service.run_cycle(db, config, clock, caps=derive(config, Secrets())).status == "ok"
    assert repo.goal_docs(db) == []
