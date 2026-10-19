"""Polite HTTP for collectors: retries with full-jitter exponential backoff, Retry-After,
conditional GET (ETag / Last-Modified), and Link-header pagination that refuses to follow
links to another host (so a token is never sent off-site).
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from argus import __version__
from argus.logging import redactor

USER_AGENT = f"Argus/{__version__} (personal course hub; single user)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class AuthError(Exception):
    """Credentials were rejected. Not retried; the runner records `auth_error`."""


class FetchError(Exception):
    """A request failed permanently or retries were exhausted."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]
    url: str
    next_url: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        max_attempts: int = 4,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._rand = rand
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def backoff(self, attempt: int, retry_after: str | None) -> float:
        """Delay before retry number `attempt` (0-based): Retry-After if given, else full jitter."""
        if retry_after is not None:
            try:
                return min(self.max_delay, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(self.max_delay, self.base_delay * (2.0**attempt)) * self._rand()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        auth_statuses: frozenset[int] = frozenset({401, 403}),
    ) -> Response:
        hdrs = dict(headers or {})
        if etag:
            hdrs["If-None-Match"] = etag
        if last_modified:
            hdrs["If-Modified-Since"] = last_modified
        last_error = "no attempts made"
        for attempt in range(self.max_attempts):
            retry_after: str | None = None
            try:
                r = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers=hdrs,
                    data=data,
                    json=dict(json_body) if json_body is not None else None,
                )
            except httpx.TransportError as e:
                last_error = f"{type(e).__name__}"
            else:
                if r.status_code in auth_statuses:
                    raise AuthError(f"{method} {_redacted(url)} -> {r.status_code}")
                if r.status_code in RETRY_STATUSES:
                    last_error = f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After")
                elif r.status_code >= 400:
                    raise FetchError(f"{method} {_redacted(url)} -> {r.status_code}")
                else:
                    nxt = r.links.get("next", {}).get("url")
                    return Response(r.status_code, r.content, dict(r.headers), str(r.url), nxt)
            if attempt + 1 < self.max_attempts:
                await self._sleep(self.backoff(attempt, retry_after))
        raise FetchError(
            f"{method} {_redacted(url)} failed after {self.max_attempts} attempts: {last_error}"
        )

    async def post_json(self, url: str, payload: Mapping[str, Any]) -> Response:
        return await self.request("POST", url, json_body=payload)

    async def get(self, url: str, **kw: Any) -> Response:
        return await self.request("GET", url, **kw)

    async def get_all_pages(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_pages: int = 50,
    ) -> list[Response]:
        """Follow `Link: rel="next"` headers. Refuses cross-host links."""
        host = urlsplit(url).netloc
        pages = [await self.get(url, params=params, headers=headers)]
        while pages[-1].next_url:
            nxt = pages[-1].next_url
            if urlsplit(nxt).netloc != host:
                raise FetchError(
                    f"refusing pagination link to another host: {urlsplit(nxt).netloc}"
                )
            if len(pages) >= max_pages:
                raise FetchError(f"more than {max_pages} pages; giving up")
            pages.append(await self.get(nxt, headers=headers))
        return pages


def _redacted(url: str) -> str:
    """Scheme, host, and path only (query strings may carry tokens), then the log redactor
    (paths may too: the Canvas feed URL embeds its token)."""
    parts = urlsplit(url)
    return redactor(f"{parts.scheme}://{parts.netloc}{parts.path}")
