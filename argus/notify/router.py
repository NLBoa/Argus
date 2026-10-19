"""Pick a channel: the first available in priority order (SMS, then ntfy); fall back on failure."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from argus.logging import get_logger, redactor
from argus.notify.channels import Channel, DeliveryError, OutboundMessage

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Delivery:
    ok: bool
    channel: str | None
    error: str | None = None


class Router:
    def __init__(self, channels: Sequence[Channel]) -> None:
        self.channels = list(channels)

    def available(self) -> list[str]:
        return [c.name for c in self.channels if c.available()]

    async def send(self, message: OutboundMessage) -> Delivery:
        errors: list[str] = []
        for channel in self.channels:
            if not channel.available():
                continue
            try:
                await channel.send(message)
            except DeliveryError as e:
                errors.append(redactor(str(e)))
                log.warning("delivery failed", extra={"channel": channel.name, "error": errors[-1]})
                continue
            return Delivery(ok=True, channel=channel.name)
        return Delivery(ok=False, channel=None, error="; ".join(errors) or "no channel configured")
