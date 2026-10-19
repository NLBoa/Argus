"""Proposals: changes Argus wants to make to student-owned text, waiting for approval.

A proposal stores the full proposed note and the hash of the note it was computed from.
Approval applies that exact result only if the note is still byte-for-byte what it was
(otherwise the proposal is stale and Argus proposes afresh). Once applied, a proposal is
never applied again. Ignored proposals expire after 14 days.
"""

from __future__ import annotations

import difflib
import hashlib
from datetime import datetime, timedelta

EXPIRY = timedelta(days=14)


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def is_expired(created_at: datetime, now: datetime) -> bool:
    return now - created_at >= EXPIRY


def apply_proposal(current_text: str, base_hash: str, after_text: str) -> str | None:
    """The exact proposed note, or None if the note changed since the proposal was made."""
    return after_text if file_hash(current_text) == base_hash else None


def unified_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (yours)",
            tofile=f"{path} (proposed)",
            lineterm="\n",
        )
    ).replace("(yours)\n", "(yours)\n", 1)
