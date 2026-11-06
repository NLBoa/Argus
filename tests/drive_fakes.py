"""A stand-in for the planner inbox uploader (D074): records what it was sent."""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass

from argus.domain.inbox_companion import SUFFIX
from argus.drive import DriveFailure, Uploaded

LINK = "https://drive.google.com/file/d/file-1/view"


@dataclass
class Sent:
    name: str
    mime_type: str
    size: int
    data: bytes


class RecordingDrive:
    """`fail` refuses every upload; `fail_companion` refuses only text companions (D077)."""

    def __init__(
        self, fail: DriveFailure | None = None, fail_companion: DriveFailure | None = None
    ) -> None:
        self.sent: list[Sent] = []
        self.fail = fail
        self.fail_companion = fail_companion

    async def upload(
        self, *, name: str, mime_type: str, size: int, chunks: AsyncIterable[bytes]
    ) -> Uploaded | DriveFailure:
        data = b"".join([c async for c in chunks])
        self.sent.append(Sent(name, mime_type, size, data))
        if self.fail is not None:
            return self.fail
        if self.fail_companion is not None and name.endswith(SUFFIX):
            return self.fail_companion
        return Uploaded(file_id="file-1", name=name, size=size, link=LINK)
