"""Vault integration: a student's laptop and the Pi share a git remote.

Covers the gate: the student edits an Argus region, edits free text, concurrent edits produce
a conflict, an approved proposal applies exactly once, and `argus-edit: allow`.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from argus.clock import FakeClock
from argus.vault.gitsync import GitSync
from argus.vault.ownership import DesiredNote
from argus.vault.regions import parse, replace_region
from argus.vault.writer import VaultWriter

NOTE = "Courses/CMSC351/Concepts/Master theorem.md"
TAIL = "\n## Explain it yourself\n\n## My notes\n"


def desired(version: str) -> DesiredNote:
    return DesiredNote({"course": "CMSC351"}, (("core", f"The master theorem, {version}."),), TAIL)


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class Laptop:
    """The student's clone, edited by hand (as Obsidian + Obsidian Git would)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        sh(root, "config", "user.name", "Student")
        sh(root, "config", "user.email", "student@example.test")

    def pull(self) -> None:
        sh(self.root, "pull", "-q", "--rebase")

    def edit(self, rel: str, change: Callable[[str], str], message: str = "my edit") -> None:
        self.pull()
        path = self.root / rel
        path.write_text(change(path.read_text()))
        sh(self.root, "commit", "-qam", message)
        sh(self.root, "push", "-q")

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text()


@pytest.fixture
def world(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> tuple[Laptop, GitSync, VaultWriter, Path]:
    remote = tmp_path / "remote.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "README.md").write_text("My vault\n")
    sh(seed, "-c", "user.name=Student", "-c", "user.email=s@example.test", "add", ".")
    sh(seed, "-c", "user.name=Student", "-c", "user.email=s@example.test", "commit", "-qm", "init")
    sh(seed, "push", "-q", "origin", "main")
    pi_root, laptop_root = tmp_path / "pi", tmp_path / "laptop"
    sh(tmp_path, "clone", "-q", str(remote), str(pi_root))
    sh(tmp_path, "clone", "-q", str(remote), str(laptop_root))
    return Laptop(laptop_root), GitSync(pi_root), VaultWriter(pi_root, db, clock), remote


def cycle(git: GitSync, writer: VaultWriter, d: DesiredNote) -> str:
    return git.cycle(lambda: (writer.write(NOTE, d), "Argus: update notes")[1]).status


def test_argus_writes_and_commits_as_argus(
    world: tuple[Laptop, GitSync, VaultWriter, Path],
) -> None:
    laptop, git, writer, _ = world
    assert cycle(git, writer, desired("v1")) == "ok"
    laptop.pull()
    assert "The master theorem, v1." in laptop.read(NOTE)
    assert sh(laptop.root, "log", "-1", "--format=%an <%ae>") == "Argus <argus@pi.local>"


def test_user_edits_region_then_approves_proposal_once(
    world: tuple[Laptop, GitSync, VaultWriter, Path],
) -> None:
    laptop, git, writer, _ = world
    cycle(git, writer, desired("v1"))
    laptop.edit(NOTE, lambda t: replace_region(t, "core", "The master theorem, v1. MY ADDITION."))
    assert cycle(git, writer, desired("v2")) == "ok"
    laptop.pull()
    assert "MY ADDITION" in laptop.read(NOTE)  # left alone
    [p] = writer.proposals()
    assert (
        "-The master theorem, v1. MY ADDITION." in p.diff and "+The master theorem, v2." in p.diff
    )
    assert cycle(git, writer, desired("v2")) == "ok"
    assert len(writer.proposals()) == 1  # not proposed twice

    assert (
        git.cycle(lambda: (writer.approve(p.id), "Argus: apply approved change")[1]).status == "ok"
    )
    laptop.pull()
    assert parse(laptop.read(NOTE)).regions["core"].content == "The master theorem, v2."
    with pytest.raises(LookupError):
        writer.approve(p.id)  # exactly once
    # Argus owns the region again: the next version is a plain rewrite.
    cycle(git, writer, desired("v3"))
    laptop.pull()
    assert "v3." in laptop.read(NOTE) and writer.proposals() == []


def test_free_text_edits_survive_rewrites(world: tuple[Laptop, GitSync, VaultWriter, Path]) -> None:
    laptop, git, writer, _ = world
    cycle(git, writer, desired("v1"))
    laptop.edit(NOTE, lambda t: t.replace("## My notes\n", "## My notes\nCase 2 trips me up.\n"))
    cycle(git, writer, desired("v2"))
    laptop.pull()
    text = laptop.read(NOTE)
    assert "The master theorem, v2." in text and "Case 2 trips me up." in text
    assert writer.proposals() == []


def test_concurrent_edits_conflict_keep_the_students_version(
    world: tuple[Laptop, GitSync, VaultWriter, Path],
) -> None:
    laptop, git, writer, remote = world
    cycle(git, writer, desired("v1"))
    # The Pi writes v2 and commits, but its push never happens (network down)...
    writer.write(NOTE, desired("v2"))
    git.commit("Argus: update notes")
    # ...while the student edits the very same lines and pushes.
    laptop.edit(NOTE, lambda t: replace_region(t, "core", "The master theorem, STUDENT VERSION."))
    assert cycle(git, writer, desired("v3")) == "conflict"
    assert "STUDENT VERSION" in (git.root / NOTE).read_text()
    assert sh(git.root, "status", "--porcelain") == ""
    # Next cycle: the student's edit is respected and becomes a proposal, not an overwrite.
    assert cycle(git, writer, desired("v3")) == "ok"
    laptop.pull()
    assert "STUDENT VERSION" in laptop.read(NOTE)
    assert len(writer.proposals()) == 1
    # The student's commit is in the remote history: nothing was force-pushed away.
    assert "my edit" in sh(remote, "log", "--format=%s", "main")


def test_stale_proposal_is_not_applied(world: tuple[Laptop, GitSync, VaultWriter, Path]) -> None:
    laptop, git, writer, _ = world
    cycle(git, writer, desired("v1"))
    laptop.edit(NOTE, lambda t: replace_region(t, "core", "mine"))
    cycle(git, writer, desired("v2"))
    [p] = writer.proposals()
    laptop.edit(NOTE, lambda t: t + "\nanother thought\n")
    git.cycle(lambda: "noop")  # pull the newer edit
    assert writer.approve(p.id) == "stale"
    assert "mine" in (git.root / NOTE).read_text()


def test_rejected_proposal_is_not_repeated(
    world: tuple[Laptop, GitSync, VaultWriter, Path],
) -> None:
    laptop, git, writer, _ = world
    cycle(git, writer, desired("v1"))
    laptop.edit(NOTE, lambda t: replace_region(t, "core", "mine"))
    cycle(git, writer, desired("v2"))
    [p] = writer.proposals()
    writer.reject(p.id)
    cycle(git, writer, desired("v2"))
    assert writer.proposals() == []
    cycle(git, writer, desired("v3"))  # a genuinely new change is proposed again
    assert len(writer.proposals()) == 1


def test_argus_edit_allow_on_note_and_folder(
    world: tuple[Laptop, GitSync, VaultWriter, Path],
) -> None:
    laptop, git, writer, _ = world
    cycle(git, writer, desired("v1"))
    laptop.edit(
        NOTE,
        lambda t: replace_region(t, "core", "mine").replace(
            "course: CMSC351", "course: CMSC351\nargus-edit: allow"
        ),
    )
    cycle(git, writer, desired("v2"))
    laptop.pull()
    assert parse(laptop.read(NOTE)).regions["core"].content == "The master theorem, v2."
    assert writer.proposals() == []
    assert writer.log.allowed_edits[-1] == (NOTE, ("rewrote core",))

    other = "Courses/CMSC351/Concepts/Heaps.md"
    git.cycle(lambda: (writer.write(other, desired("heaps v1")), "Argus: notes")[1])
    laptop.pull()
    folder_note = laptop.root / "Courses/CMSC351/Concepts/Concepts.md"
    folder_note.write_text("---\nargus-edit: allow\n---\nFolder note.\n")
    sh(laptop.root, "add", ".")
    sh(laptop.root, "commit", "-qm", "allow folder")
    sh(laptop.root, "push", "-q")
    laptop.edit(other, lambda t: replace_region(t, "core", "student scribble"))
    git.cycle(lambda: (writer.write(other, desired("heaps v2")), "Argus: notes")[1])
    assert "heaps v2" in (git.root / other).read_text()
    assert writer.proposals() == []


def test_proposals_expire_after_14_days(
    world: tuple[Laptop, GitSync, VaultWriter, Path], clock: FakeClock
) -> None:
    from datetime import timedelta

    laptop, git, writer, _ = world
    cycle(git, writer, desired("v1"))
    laptop.edit(NOTE, lambda t: replace_region(t, "core", "mine"))
    cycle(git, writer, desired("v2"))
    clock.advance(timedelta(days=13))
    assert writer.expire() == 0
    clock.advance(timedelta(days=1))
    assert writer.expire() == 1
    cycle(git, writer, desired("v2"))
    assert writer.proposals() == []  # expired means ignored; not re-raised for the same change


def test_errors_are_reported_not_raised(
    tmp_path: Path, db: sqlite3.Connection, clock: FakeClock
) -> None:
    git = GitSync(tmp_path / "solo")
    writer = VaultWriter(tmp_path / "solo", db, clock)
    assert (
        git.cycle(lambda: (writer.write(NOTE, desired("v1")), "Argus: notes")[1]).status == "ok"
    )  # no remote: local only
    sh(git.root, "remote", "add", "origin", str(tmp_path / "missing.git"))
    result = git.cycle(lambda: "Argus: notes")
    assert result.status == "error" and result.error
