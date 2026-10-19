"""Twilio SMS: the primary channel once the number is verified (ntfy stays as the backup).

One attempt per send. A POST that timed out may still have been accepted by Twilio, and
retrying it here could text the student twice; the reminder engine retries failed sends
itself (at most 3 attempts, until the message expires), and the router falls back to ntfy.
"""

from __future__ import annotations

import base64

import httpx

from argus.collectors.http import AuthError, Fetcher, FetchError
from argus.notify.channels import DeliveryError, OutboundMessage

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


class TwilioChannel:
    name = "sms"

    def __init__(
        self,
        client: httpx.AsyncClient,
        account_sid: str | None,
        auth_token: str | None,
        from_number: str | None,
        to_number: str | None,
    ) -> None:
        self._fetcher = Fetcher(client, max_attempts=1)
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._to = to_number

    def available(self) -> bool:
        return bool(self._sid and self._token and self._from and self._to)

    async def send(self, message: OutboundMessage) -> None:
        sid, token, sender, to = self._sid, self._token, self._from, self._to
        if not (sid and token and sender and to):
            raise DeliveryError("Twilio is not configured")
        basic = base64.b64encode(f"{sid}:{token}".encode()).decode()
        try:
            await self._fetcher.post_form(
                API.format(sid=sid),
                {"To": to, "From": sender, "Body": message.sms_text},
                headers={"Authorization": f"Basic {basic}"},
            )
        except AuthError as e:
            raise DeliveryError(
                "Twilio rejected the credentials (check the SID and auth token)"
            ) from e
        except FetchError as e:
            raise DeliveryError(f"Twilio: {e}") from e
