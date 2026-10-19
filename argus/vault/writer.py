"""The only way Argus writes into the vault.

Every write goes through `ownership.plan_note`: Argus writes new notes and its own unedited
regions; anything the student touched becomes a proposal; notes or folders marked
`argus-edit: allow` are edited freely and the edit is logged for the weekly note.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from argus.clock import Clock
from argus.store.db import iso, parse_iso, transaction
from argus.vault.ownership import DesiredNote, Plan, ProposalDraft, plan_note
from argus.vault.proposals import apply_proposal, is_expired, unified_diff
from argus.vault.regions import allows_edit, parse, region_hash, split_frontmatter

UPSERT_REGION = """INSERT INTO vault_regions (path, region_id, hash, written_at) VALUES (?, ?, ?, ?)
    ON CONFLICT (path, region_id) DO UPDATE SET hash = excluded.hash, written_at = excluded.written_at"""


@dataclass
class WriteLog:
    """What a vault cycle did, for the weekly note and the commit message."""

    written: list[str] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    allowed_edits: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)


@dataclass(frozen=True)
class ProposalRow:
    id: int
    path: str
    region_ids: tuple[str, ...]
    reason: str
    base_hash: str
    before: str
    after: str
    diff: str
    status: str
    created_at: datetime


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".argus-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class VaultWriter:
    def __init__(self, root: Path, conn: sqlite3.Connection, clock: Clock) -> None:
        self.root = root
        self.conn = conn
        self.clock = clock
        self.log = WriteLog()

    # ---------------------------------------------------------------- reading

    def read(self, rel: str) -> str | None:
        path = self.root / rel
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def allowed(self, rel: str, text: str | None) -> bool:
        """`argus-edit: allow` on the note itself, or on a folder note of any parent folder."""
        if text is not None and allows_edit(split_frontmatter(text)[0]):
            return True
        parent = Path(rel).parent
        while str(parent) not in ("", "."):
            folder_note = self.root / parent / f"{parent.name}.md"
            if folder_note.is_file() and allows_edit(
                split_frontmatter(folder_note.read_text(encoding="utf-8"))[0]
            ):
                return True
            parent = parent.parent
        return False

    def stored_hashes(self, rel: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT region_id, hash FROM vault_regions WHERE path = ?", (rel,)
        ).fetchall()
        return {r["region_id"]: r["hash"] for r in rows}

    # ---------------------------------------------------------------- writing

    def write(self, rel: str, desired: DesiredNote) -> Plan:
        current = self.read(rel)
        plan = plan_note(
            current, desired, self.stored_hashes(rel), allow=self.allowed(rel, current)
        )
        now = iso(self.clock.now())
        if plan.text is not None:
            _atomic_write(self.root / rel, plan.text)
            self.log.written.append(rel)
        with transaction(self.conn):
            for region_id, h in plan.written.items():
                self.conn.execute(
                    UPSERT_REGION,
                    (rel, region_id, h, now),
                )
            for draft in plan.proposals:
                self._propose(rel, draft)
        if plan.log:
            self.log.allowed_edits.append((rel, plan.log))
        return plan

    def _propose(self, rel: str, draft: ProposalDraft) -> None:
        now = iso(self.clock.now())
        open_rows = self.conn.execute(
            "SELECT id, after_text, base_hash FROM proposals WHERE path = ? AND status = 'open'",
            (rel,),
        ).fetchall()
        declined = self.conn.execute(
            """SELECT 1 FROM proposals WHERE path = ? AND status IN ('rejected', 'expired')
               AND after_text = ? AND base_hash = ?""",
            (rel, draft.after, draft.base_hash),
        ).fetchone()
        if declined:
            return  # the student already said no to exactly this change
        for r in open_rows:
            if r["after_text"] == draft.after and r["base_hash"] == draft.base_hash:
                return  # already waiting for the student
            self.conn.execute(
                "UPDATE proposals SET status = 'superseded', decided_at = ? WHERE id = ?",
                (now, r["id"]),
            )
        self.conn.execute(
            """INSERT INTO proposals (path, region_ids, reason, base_hash, before_text, after_text, diff, status,
                                     created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                rel,
                json.dumps(list(draft.region_ids)),
                draft.reason,
                draft.base_hash,
                draft.before,
                draft.after,
                unified_diff(draft.before, draft.after, rel),
                now,
            ),
        )
        self.log.proposed.append(rel)

    # ---------------------------------------------------------------- proposals

    def proposals(self, status: str = "open") -> list[ProposalRow]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status = ? ORDER BY created_at, id", (status,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def proposal(self, proposal_id: int) -> ProposalRow | None:
        r = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r: sqlite3.Row) -> ProposalRow:
        return ProposalRow(
            r["id"],
            r["path"],
            tuple(json.loads(r["region_ids"])),
            r["reason"],
            r["base_hash"],
            r["before_text"],
            r["after_text"],
            r["diff"],
            r["status"],
            parse_iso(r["created_at"]),
        )

    def approve(self, proposal_id: int) -> str:
        """Apply the exact proposed note once. Returns 'applied', 'stale', or raises LookupError."""
        p = self.proposal(proposal_id)
        if p is None or p.status != "open":
            raise LookupError("This proposal is no longer open")
        current = self.read(p.path) or ""
        result = apply_proposal(current, p.base_hash, p.after)
        now = iso(self.clock.now())
        with transaction(self.conn):
            if result is None:
                self.conn.execute(
                    "UPDATE proposals SET status = 'stale', decided_at = ? WHERE id = ?",
                    (now, p.id),
                )
                return "stale"
            _atomic_write(self.root / p.path, result)
            self.conn.execute(
                "UPDATE proposals SET status = 'applied', decided_at = ?, applied_at = ? WHERE id = ?",
                (now, now, p.id),
            )
            regions = parse(result).regions
            for rid in p.region_ids:
                if rid in regions:
                    self.conn.execute(
                        UPSERT_REGION,
                        (p.path, rid, region_hash(regions[rid].content), now),
                    )
        self.log.written.append(p.path)
        return "applied"

    def reject(self, proposal_id: int) -> None:
        p = self.proposal(proposal_id)
        if p is None or p.status != "open":
            raise LookupError("This proposal is no longer open")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE proposals SET status = 'rejected', decided_at = ? WHERE id = ?",
                (iso(self.clock.now()), p.id),
            )

    def expire(self) -> int:
        now = self.clock.now()
        expired = [p.id for p in self.proposals() if is_expired(p.created_at, now)]
        with transaction(self.conn):
            for pid in expired:
                self.conn.execute(
                    "UPDATE proposals SET status = 'expired', decided_at = ? WHERE id = ?",
                    (iso(now), pid),
                )
        return len(expired)
