"""Channel contract shared by SMS and push."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    title: str
    body: str
    sms_text: str  # GSM-7, one segment where possible
    priority: int = 3  # 1 .. 5
    tags: tuple[str, ...] = field(default=())
    click_url: str | None = None


class DeliveryError(Exception):
    """A channel could not deliver. The message is redacted-safe (no secrets)."""


class Channel(Protocol):
    name: str

    def available(self) -> bool:
        """Configured and allowed to send."""
        ...

    async def send(self, message: OutboundMessage) -> None:
        """Deliver or raise DeliveryError."""
        ...
