"""ntfy push: the channel until Twilio is verified, and the backup after.

Publishes JSON to the server root, so titles can be UTF-8. The topic name is a secret:
anyone who knows it can read the pushes.
"""

from __future__ import annotations

from argus.collectors.http import AuthError, Fetcher, FetchError
from argus.notify.channels import DeliveryError, OutboundMessage


class NtfyChannel:
    name = "ntfy"

    def __init__(self, fetcher: Fetcher, server: str, topic: str | None) -> None:
        self._fetcher = fetcher
        self._server = server.rstrip("/")
        self._topic = topic

    def available(self) -> bool:
        return bool(self._topic)

    async def send(self, message: OutboundMessage) -> None:
        if not self._topic:
            raise DeliveryError("ntfy topic not configured")
        payload: dict[str, object] = {
            "topic": self._topic,
            "title": message.title,
            "message": message.body,
            "priority": max(1, min(5, message.priority)),
        }
        if message.tags:
            payload["tags"] = list(message.tags)
        if message.click_url:
            payload["click"] = message.click_url
        try:
            await self._fetcher.post_json(self._server + "/", payload)
        except (AuthError, FetchError) as e:
            raise DeliveryError(f"ntfy: {e}") from e
