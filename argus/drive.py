"""Google Drive for the file adder (D074): put one file into the morning planner's inbox folder.

Auth is an OAuth "installed app" client with a stored refresh token, not a service account
(service accounts have no Drive storage of their own). `argus drive-auth` gets the token once
(`argus/drive_auth.py`). Uploads use Drive's resumable protocol in a single PUT, so a 50 MB file
streams from disk instead of sitting in memory, and the file's real MIME type means Drive keeps
its format (no conversion to Docs or Slides).

Every call returns an outcome; nothing raises past this module. Logs carry the file name, its
size, and HTTP status codes, never file contents, tokens, or Drive's error bodies (they echo the
folder ID).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from argus.clock import Clock
from argus.collectors.http import USER_AGENT
from argus.config import AppConfig, Secrets
from argus.logging import get_logger

log = get_logger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
SCOPE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPE_FULL = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
_TOKEN_MARGIN = timedelta(seconds=60)
RERUN = "Run `argus drive-auth` again and copy the new token to the Pi (docs/runbook.md)."


@dataclass(frozen=True, slots=True)
class DriveCredentials:
    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    @classmethod
    def from_secrets(cls, secrets: Secrets) -> DriveCredentials | None:
        if not (secrets.drive_client_id and secrets.drive_client_secret):
            return None
        if not secrets.drive_refresh_token:
            return None
        return cls(
            secrets.drive_client_id.get_secret_value(),
            secrets.drive_client_secret.get_secret_value(),
            secrets.drive_refresh_token.get_secret_value(),
        )


class Problem(StrEnum):
    AUTH = "auth"  # the refresh token expired or was revoked, or the client was rejected
    SCOPE = "scope"  # the grant doesn't include Drive
    FOLDER = "folder"  # the folder is missing or this grant can't see it
    PERMISSION = "permission"  # the account can't add files to the folder
    QUOTA = "quota"
    RATE = "rate"
    UNAVAILABLE = "unavailable"  # network trouble or a Google 5xx
    REJECTED = "rejected"  # any other refusal


@dataclass(frozen=True, slots=True)
class DriveFailure:
    problem: Problem
    message: str  # for the student
    status: int | None = None


@dataclass(frozen=True, slots=True)
class Uploaded:
    file_id: str
    name: str
    size: int
    link: str | None  # Drive's web link; None when nothing was really uploaded (the demo)


@dataclass(frozen=True, slots=True)
class FolderInfo:
    name: str
    is_folder: bool
    can_add_children: bool
    trashed: bool

    @property
    def usable(self) -> bool:
        return self.is_folder and self.can_add_children and not self.trashed


class Uploader(Protocol):
    async def upload(
        self, *, name: str, mime_type: str, size: int, chunks: AsyncIterable[bytes]
    ) -> Uploaded | DriveFailure: ...


class DriveClient:
    """Uploads into one folder. Keeps its access token until shortly before it expires."""

    def __init__(
        self,
        credentials: DriveCredentials,
        folder_id: str,
        clock: Clock,
        *,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._creds = credentials
        self._folder_id = folder_id
        self._clock = clock
        self._timeout = timeout
        self._transport = transport
        self._token: tuple[str, datetime] | None = None
        self.granted_scope: str | None = None  # from the last token refresh

    @classmethod
    def from_config(cls, config: AppConfig, secrets: Secrets, clock: Clock) -> DriveClient | None:
        creds = DriveCredentials.from_secrets(secrets)
        folder = config.planner_inbox.folder_id
        if creds is None or not folder:
            return None
        return cls(creds, folder, clock)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=15.0),
            headers={"User-Agent": USER_AGENT},
            transport=self._transport,
        )

    async def _access_token(self, client: httpx.AsyncClient, *, fresh: bool = False) -> str:
        """A valid access token. Raises _DriveError."""
        now = self._clock.now()
        if not fresh and self._token is not None and self._token[1] > now:
            return self._token[0]
        resp = await client.post(
            TOKEN_URL,
            data={
                "client_id": self._creds.client_id,
                "client_secret": self._creds.client_secret,
                "refresh_token": self._creds.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise _DriveError(_token_failure(resp))
        body = _json(resp)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise _DriveError(DriveFailure(Problem.AUTH, "Google sent no access token. " + RERUN))
        expires = int(body.get("expires_in") or 3600)
        self._token = (token, now + timedelta(seconds=expires) - _TOKEN_MARGIN)
        self.granted_scope = str(body.get("scope") or "")
        return token

    async def _authed(
        self, client: httpx.AsyncClient, method: str, url: str, **kw: Any
    ) -> httpx.Response:
        """A Drive API call with the access token; one fresh token and retry on a 401."""
        for fresh in (False, True):
            token = await self._access_token(client, fresh=fresh)
            headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {token}"}
            resp = await client.request(method, url, headers=headers, **kw)
            if resp.status_code != 401 or fresh:
                return resp
            kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
        raise AssertionError("unreachable")  # pragma: no cover

    async def upload(
        self, *, name: str, mime_type: str, size: int, chunks: AsyncIterable[bytes]
    ) -> Uploaded | DriveFailure:
        try:
            async with self._client() as client:
                result = await self._upload(client, name, mime_type, size, chunks)
        except _DriveError as f:
            result = f.failure
        except httpx.HTTPError as e:
            log.warning("drive upload: network error", extra={"error": type(e).__name__})
            result = DriveFailure(
                Problem.UNAVAILABLE,
                "Couldn't reach Google Drive. Check the Pi's internet connection and try again.",
            )
        if isinstance(result, Uploaded):
            log.info("planner inbox upload", extra={"file_name": name, "bytes": size})
        else:
            log.warning(
                "planner inbox upload failed",
                extra={
                    "file_name": name,
                    "bytes": size,
                    "problem": result.problem.value,
                    "status": result.status,
                },
            )
        return result

    async def _upload(
        self,
        client: httpx.AsyncClient,
        name: str,
        mime_type: str,
        size: int,
        chunks: AsyncIterable[bytes],
    ) -> Uploaded | DriveFailure:
        start = await self._authed(
            client,
            "POST",
            UPLOAD_URL,
            params={
                "uploadType": "resumable",
                "supportsAllDrives": "true",
                "fields": "id,name,webViewLink",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size),
            },
            content=json.dumps(
                {"name": name, "parents": [self._folder_id], "mimeType": mime_type}
            ).encode(),
        )
        if start.status_code != 200:
            return _api_failure(start)
        session = start.headers.get("location", "")
        if not _is_google(session):
            return DriveFailure(
                Problem.REJECTED, "Google Drive didn't start the upload. Try again.", 200
            )
        # The session URL is the upload's own credential: no Authorization header (Drive docs).
        done = await client.put(
            session,
            headers={"Content-Length": str(size), "Content-Type": mime_type},
            content=chunks,
        )
        if done.status_code not in (200, 201):
            return _api_failure(done)
        body = _json(done)
        link = body.get("webViewLink")
        return Uploaded(
            file_id=str(body.get("id", "")),
            name=str(body.get("name") or name),
            size=size,
            link=link if isinstance(link, str) and _is_drive_link(link) else None,
        )

    async def folder(self) -> FolderInfo | DriveFailure:
        """What this grant can see of the inbox folder (setup and check-config)."""
        try:
            async with self._client() as client:
                resp = await self._authed(
                    client,
                    "GET",
                    f"{FILES_URL}/{self._folder_id}",
                    params={
                        "fields": "name,mimeType,trashed,capabilities(canAddChildren)",
                        "supportsAllDrives": "true",
                    },
                )
        except _DriveError as f:
            return f.failure
        except httpx.HTTPError:
            return DriveFailure(Problem.UNAVAILABLE, "Couldn't reach Google Drive.")
        if resp.status_code != 200:
            return _api_failure(resp)
        body = _json(resp)
        caps = body.get("capabilities") or {}
        return FolderInfo(
            name=str(body.get("name", "")),
            is_folder=body.get("mimeType") == FOLDER_MIME,
            can_add_children=bool(caps.get("canAddChildren")),
            trashed=bool(body.get("trashed")),
        )

    async def account_email(self) -> str | DriveFailure:
        """The Google account the refresh token belongs to (shown once, at setup)."""
        try:
            async with self._client() as client:
                resp = await self._authed(
                    client, "GET", ABOUT_URL, params={"fields": "user(emailAddress)"}
                )
        except _DriveError as f:
            return f.failure
        except httpx.HTTPError:
            return DriveFailure(Problem.UNAVAILABLE, "Couldn't reach Google Drive.")
        if resp.status_code != 200:
            return _api_failure(resp)
        return str((_json(resp).get("user") or {}).get("emailAddress", ""))


class _DriveError(Exception):
    def __init__(self, failure: DriveFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _is_google(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc == "www.googleapis.com"


def _is_drive_link(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc in ("drive.google.com", "docs.google.com")


def _token_failure(resp: httpx.Response) -> DriveFailure:
    error = str(_json(resp).get("error", ""))
    if error == "invalid_grant":
        return DriveFailure(
            Problem.AUTH,
            "Google turned down Argus's Drive sign-in: the refresh token expired or was revoked. "
            + RERUN,
            resp.status_code,
        )
    if error in ("invalid_client", "unauthorized_client"):
        return DriveFailure(
            Problem.AUTH,
            "Google doesn't accept the Drive OAuth client. Check drive_client_id and "
            "drive_client_secret.",
            resp.status_code,
        )
    if resp.status_code >= 500:
        return _unavailable(resp.status_code)
    return DriveFailure(
        Problem.AUTH,
        f"Google refused Argus's Drive sign-in (HTTP {resp.status_code}). " + RERUN,
        resp.status_code,
    )


def _api_failure(resp: httpx.Response) -> DriveFailure:
    status = resp.status_code
    error = _json(resp).get("error")
    reasons: set[str] = set()
    if isinstance(error, dict):
        reasons = {
            str(e.get("reason", "")) for e in error.get("errors") or [] if isinstance(e, dict)
        }
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                reasons.add(str(detail["reason"]))
    if status == 404:
        return DriveFailure(
            Problem.FOLDER,
            "Argus can't see the planner's Inbox folder. Check planner_inbox.folder_id. If it's "
            "right, the drive.file scope can't reach a folder Argus didn't create: run "
            "`argus drive-auth --full-drive`.",
            status,
        )
    if status == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        return DriveFailure(
            Problem.RATE, "Google Drive is limiting requests. Try again in a minute.", status
        )
    if reasons & {"storageQuotaExceeded", "quotaExceeded"}:
        return DriveFailure(Problem.QUOTA, "That Google Drive is full.", status)
    if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
        return DriveFailure(
            Problem.SCOPE,
            "Argus's Google sign-in doesn't include Drive access. " + RERUN,
            status,
        )
    if status == 401:
        return DriveFailure(Problem.AUTH, "Google Drive rejected Argus's sign-in. " + RERUN, status)
    if status == 403:
        return DriveFailure(
            Problem.PERMISSION,
            "This Google account can't add files to the Inbox folder.",
            status,
        )
    if status >= 500:
        return _unavailable(status)
    return DriveFailure(
        Problem.REJECTED, f"Google Drive refused the upload (HTTP {status}).", status
    )


def _unavailable(status: int) -> DriveFailure:
    return DriveFailure(
        Problem.UNAVAILABLE, f"Google Drive had a problem (HTTP {status}). Try again.", status
    )
