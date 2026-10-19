"""Nightly SQLite backups: an online snapshot, gzip, 14 days kept, copied off the Pi.

The snapshot uses SQLite's backup API, so it is consistent while the worker and dashboard keep
running. Off-device copy is `rsync -a --delete` of the backup folder to `paths.backup_remote`
(an SSH target over Tailscale, or a mounted drive), so the remote also keeps exactly 14 days.
The vault is not included: its git remote is its backup.
"""

from __future__ import annotations

import gzip
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from argus.clock import LOCAL_TZ, to_local

KEEP = timedelta(days=14)
_NAME = re.compile(r"^argus-(\d{8})-(\d{4})\.db\.gz$")
RSYNC_TIMEOUT_S = 600


class BackupError(Exception):
    pass


def snapshot(db_path: Path, backup_dir: Path, now: datetime) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"argus-{to_local(now):%Y%m%d-%H%M}.db.gz"
    raw = backup_dir / (name + ".tmp-db")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    out = backup_dir / name
    with raw.open("rb") as f_in, gzip.open(out, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw.unlink()
    return out


def prune(backup_dir: Path, now: datetime) -> list[Path]:
    """Delete backups older than 14 days (by the date in the name). Returns what was removed."""
    removed = []
    cutoff = now - KEEP
    for path in sorted(backup_dir.glob("argus-*.db.gz")):
        m = _NAME.match(path.name)
        if m is None:
            continue
        taken = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(tzinfo=LOCAL_TZ)
        if taken < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def copy_off_device(backup_dir: Path, remote: str) -> None:
    if remote.startswith("-"):
        raise BackupError("backup_remote must be a path or host:path, not an option")
    result = subprocess.run(  # noqa: S603 (fixed binary, no shell)
        [  # noqa: S607
            "rsync",
            "-a",
            "--delete",
            "--include=argus-*.db.gz",
            "--exclude=*",
            f"{backup_dir}/",
            remote.rstrip("/") + "/",
        ],
        capture_output=True,
        text=True,
        timeout=RSYNC_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        raise BackupError(f"rsync failed ({result.returncode}): {result.stderr.strip()[:300]}")


def verify(path: Path) -> None:
    """Decompress to a temp file and run PRAGMA integrity_check."""
    tmp = path.with_suffix(".verify")
    try:
        with gzip.open(path, "rb") as f_in, tmp.open("wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        conn = sqlite3.connect(tmp)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if ok != "ok":
            raise BackupError(f"{path.name} failed the integrity check: {ok}")
    finally:
        tmp.unlink(missing_ok=True)


def restore(archive: Path, db_path: Path, now: datetime) -> Path | None:
    """Replace the database with a backup. The current file is kept beside it. Stop services first."""
    verify(archive)
    kept = None
    if db_path.exists():
        kept = db_path.with_name(f"{db_path.name}.before-restore-{to_local(now):%Y%m%d-%H%M%S}")
        db_path.replace(kept)
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    with gzip.open(archive, "rb") as f_in, db_path.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return kept
