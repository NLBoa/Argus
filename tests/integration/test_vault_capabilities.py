"""The vault follows the same capabilities as the web app (D055): no Modules section, grade,
module notes, or study artifacts a missing source can't support, including data stored before
the token was removed."""

from __future__ import annotations

from pathlib import Path

import pytest

from argus.capabilities import derive
from argus.clock import FakeClock
from argus.config import PathsConfig
from argus.demo import DEMO_CONFIG, DemoProfile, demo_secrets, seed_demo
from argus.store.db import open_db
from argus.vault import layout
from argus.vault.service import Builder
from argus.vault.writer import VaultWriter


def build(tmp_path: Path, clock: FakeClock, seeded: DemoProfile, served: DemoProfile) -> Path:
    config = DEMO_CONFIG.model_copy(
        update={"paths": PathsConfig(data_dir=tmp_path, vault_dir=tmp_path / "vault")}
    )
    conn = open_db(tmp_path / "v.db", clock)
    seed_demo(conn, clock.now(), seeded)
    root = config.paths.vault_dir
    root.mkdir()
    caps = derive(config, demo_secrets(served))
    Builder(conn, config, clock, VaultWriter(root, conn, clock), caps).build()
    return root


def test_full_setup_writes_modules_grade_and_study(tmp_path: Path, clock: FakeClock) -> None:
    root = build(tmp_path, clock, DemoProfile.FULL, DemoProfile.FULL)
    course = (root / layout.course_note("CMSC351")).read_text()
    assert "## Modules" in course
    assert "**Current grade:** 91.5% (A-)" in course
    assert any((root / "Courses" / "CMSC351" / "Modules").iterdir())


@pytest.mark.parametrize(
    "seeded", [DemoProfile.NO_CANVAS_TOKEN, DemoProfile.FULL], ids=["no-token", "token-removed"]
)
def test_without_token_no_modules_or_grade(
    tmp_path: Path, clock: FakeClock, seeded: DemoProfile
) -> None:
    root = build(tmp_path, clock, seeded, DemoProfile.NO_CANVAS_TOKEN)
    course = (root / layout.course_note("CMSC351")).read_text()
    assert "## Modules" not in course
    assert "Current grade" not in course
    assert not (root / "Courses" / "CMSC351" / "Modules").exists()
    assert "**Due:**" in (root / layout.assignment_note("CMSC351", "Homework 3")).read_text()
