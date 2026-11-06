"""The Drive client (D074) against a mocked Google API (respx): token refresh, the resumable
upload, every failure it reports, and what it logs."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from argus.clock import FakeClock
from argus.config import AppConfig, PlannerInboxConfig, Secrets
from argus.drive import (
    ABOUT_URL,
    FILES_URL,
    TOKEN_URL,
    UPLOAD_URL,
    DriveClient,
    DriveCredentials,
    DriveFailure,
    FolderInfo,
    Problem,
    Uploaded,
)

FOLDER = "Folder_ID-test-0123456789"
SESSION = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=XYZ"
CREDS = DriveCredentials("client.apps.googleusercontent.test", "client-secret", "refresh-tok")
NAME = "STAT400 · Ch5 lecture slides · slides · by 2026-11-09 09:00 · deep.pdf"
BODY = b"%PDF-1.7\n" + b"x" * 5000
PDF = "application/pdf"


async def chunks(data: bytes = BODY, size: int = 1024) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


def token_ok(token: str = "access-1") -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": token, "expires_in": 3599, "scope": "x", "token_type": "Bearer"}
    )


def api_error(status: int, reason: str = "") -> httpx.Response:
    errors = [{"reason": reason, "message": f"File not found: {FOLDER}."}] if reason else []
    return httpx.Response(
        status, json={"error": {"code": status, "message": f"about {FOLDER}", "errors": errors}}
    )


@pytest.fixture
def drive(clock: FakeClock) -> DriveClient:
    return DriveClient(CREDS, FOLDER, clock)


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(return_value=token_ok())
        mock.post(UPLOAD_URL).mock(return_value=httpx.Response(200, headers={"Location": SESSION}))
        mock.put(SESSION).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "file-1",
                    "name": NAME,
                    "webViewLink": "https://drive.google.com/file/d/file-1/view",
                },
            )
        )
        yield mock


async def send(drive: DriveClient, data: bytes = BODY) -> Uploaded | DriveFailure:
    return await drive.upload(name=NAME, mime_type=PDF, size=len(data), chunks=chunks(data))


# ---------------------------------------------------------------- the happy path


async def test_upload_refreshes_starts_a_session_and_puts_the_bytes(
    drive: DriveClient, google: respx.MockRouter
) -> None:
    result = await send(drive)
    assert result == Uploaded(
        file_id="file-1",
        name=NAME,
        size=len(BODY),
        link="https://drive.google.com/file/d/file-1/view",
    )

    token_req = google.routes[0].calls.last.request
    assert parse_qs(token_req.content.decode()) == {
        "client_id": [CREDS.client_id],
        "client_secret": ["client-secret"],
        "refresh_token": ["refresh-tok"],
        "grant_type": ["refresh_token"],
    }

    start = google.routes[1].calls.last.request
    assert start.url.params["uploadType"] == "resumable"
    assert start.url.params["supportsAllDrives"] == "true"
    assert start.headers["Authorization"] == "Bearer access-1"
    assert start.headers["X-Upload-Content-Type"] == PDF
    assert start.headers["X-Upload-Content-Length"] == str(len(BODY))
    # Into the inbox folder, as its own format (never a Google Docs type).
    assert json.loads(start.content) == {"name": NAME, "parents": [FOLDER], "mimeType": PDF}

    put = google.routes[2].calls.last.request
    assert put.read() == BODY
    assert put.headers["Content-Length"] == str(len(BODY))
    assert put.headers["Content-Type"] == PDF
    assert "Authorization" not in put.headers  # the session URL is the credential
    assert "transfer-encoding" not in put.headers


async def test_access_token_is_reused_until_it_expires(
    drive: DriveClient, google: respx.MockRouter, clock: FakeClock
) -> None:
    await send(drive)
    await send(drive)
    assert google.routes[0].call_count == 1
    clock.advance(timedelta(minutes=59))  # inside the 60 s margin of the 3599 s token
    await send(drive)
    assert google.routes[0].call_count == 2


async def test_a_401_gets_one_fresh_token_and_a_retry(
    drive: DriveClient, google: respx.MockRouter
) -> None:
    google.routes[0].side_effect = [token_ok("stale"), token_ok("fresh")]
    google.routes[1].side_effect = [
        httpx.Response(401),
        httpx.Response(200, headers={"Location": SESSION}),
    ]
    assert isinstance(await send(drive), Uploaded)
    auth = [c.request.headers["Authorization"] for c in google.routes[1].calls]
    assert auth == ["Bearer stale", "Bearer fresh"]


async def test_a_second_401_is_reported(drive: DriveClient, google: respx.MockRouter) -> None:
    google.routes[1].mock(return_value=httpx.Response(401))
    result = await send(drive)
    assert isinstance(result, DriveFailure)
    assert result.problem is Problem.AUTH
    assert google.routes[1].call_count == 2


async def test_link_from_somewhere_else_is_dropped(
    drive: DriveClient, google: respx.MockRouter
) -> None:
    google.routes[2].mock(
        return_value=httpx.Response(200, json={"id": "f", "webViewLink": "javascript:alert(1)"})
    )
    result = await send(drive)
    assert isinstance(result, Uploaded)
    assert result.link is None
    assert result.name == NAME


# ---------------------------------------------------------------- failures


@pytest.mark.parametrize(
    ("response", "problem", "says"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), Problem.AUTH, "drive-auth"),
        (httpx.Response(401, json={"error": "invalid_client"}), Problem.AUTH, "drive_client_id"),
        (httpx.Response(400, json={"error": "invalid_request"}), Problem.AUTH, "HTTP 400"),
        (httpx.Response(503, text="down"), Problem.UNAVAILABLE, "HTTP 503"),
        (httpx.Response(200, json={}), Problem.AUTH, "no access token"),
    ],
)
async def test_token_failures(
    drive: DriveClient,
    google: respx.MockRouter,
    response: httpx.Response,
    problem: Problem,
    says: str,
) -> None:
    google.routes[0].mock(return_value=response)
    result = await send(drive)
    assert isinstance(result, DriveFailure)
    assert result.problem is problem
    assert says in result.message
    assert google.routes[1].call_count == 0


@pytest.mark.parametrize(
    ("response", "problem", "says"),
    [
        (api_error(404, "notFound"), Problem.FOLDER, "--full-drive"),
        (api_error(403, "storageQuotaExceeded"), Problem.QUOTA, "full"),
        (api_error(403, "insufficientPermissions"), Problem.SCOPE, "drive-auth"),
        (api_error(403, "userRateLimitExceeded"), Problem.RATE, "minute"),
        (api_error(429), Problem.RATE, "minute"),
        (api_error(403, "forbidden"), Problem.PERMISSION, "can't add files"),
        (api_error(500, "backendError"), Problem.UNAVAILABLE, "HTTP 500"),
        (api_error(400, "badRequest"), Problem.REJECTED, "HTTP 400"),
        (httpx.Response(400, text="not json"), Problem.REJECTED, "HTTP 400"),
    ],
)
async def test_session_failures(
    drive: DriveClient,
    google: respx.MockRouter,
    response: httpx.Response,
    problem: Problem,
    says: str,
) -> None:
    google.routes[1].mock(return_value=response)
    result = await send(drive)
    assert isinstance(result, DriveFailure)
    assert result.problem is problem
    assert says in result.message
    assert FOLDER not in result.message  # Drive's error text echoes the folder ID
    assert google.routes[2].call_count == 0


async def test_scope_reason_in_details(drive: DriveClient, google: respx.MockRouter) -> None:
    google.routes[1].mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {"code": 403, "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}
            },
        )
    )
    result = await send(drive)
    assert isinstance(result, DriveFailure)
    assert result.problem is Problem.SCOPE


async def test_upload_body_failure(drive: DriveClient, google: respx.MockRouter) -> None:
    google.routes[2].mock(return_value=api_error(503, "backendError"))
    result = await send(drive)
    assert isinstance(result, DriveFailure)
    assert result.problem is Problem.UNAVAILABLE


async def test_session_url_elsewhere_is_never_sent_the_file(
    drive: DriveClient, google: respx.MockRouter
) -> None:
    google.routes[1].mock(
        return_value=httpx.Response(200, headers={"Location": "https://evil.example/upload"})
    )
    evil = google.put("https://evil.example/upload")
    result = await send(drive)
    assert isinstance(result, DriveFailure)
    assert result.problem is Problem.REJECTED
    assert evil.call_count == 0


async def test_network_error(drive: DriveClient, google: respx.MockRouter) -> None:
    google.routes[2].side_effect = httpx.ConnectError("no route")
    result = await send(drive)
    assert isinstance(result, DriveFailure)
    assert result.problem is Problem.UNAVAILABLE
    assert "internet" in result.message


# ---------------------------------------------------------------- logs


async def test_logs_name_and_size_never_contents_or_tokens(
    drive: DriveClient, google: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    secret_body = b"%PDF-1.7 SECRET-CONTENTS " + b"y" * 100
    with caplog.at_level(logging.INFO, logger="argus.drive"):
        await send(drive, secret_body)
    [record] = [r for r in caplog.records if r.name == "argus.drive"]
    assert record.file_name == NAME  # type: ignore[attr-defined]
    assert record.bytes == len(secret_body)  # type: ignore[attr-defined]
    dumped = json.dumps([vars(r) for r in caplog.records], default=str)
    for leak in ("SECRET-CONTENTS", "access-1", "refresh-tok", "client-secret"):
        assert leak not in dumped


async def test_failure_logs_problem_and_status_not_drive_text(
    drive: DriveClient, google: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    google.routes[1].mock(return_value=api_error(404, "notFound"))
    with caplog.at_level(logging.INFO, logger="argus.drive"):
        await send(drive)
    [record] = [r for r in caplog.records if r.name == "argus.drive"]
    assert record.levelno == logging.WARNING
    assert record.problem == "folder"  # type: ignore[attr-defined]
    assert record.status == 404  # type: ignore[attr-defined]
    assert FOLDER not in json.dumps(vars(record), default=str)


# ---------------------------------------------------------------- setup checks


async def test_folder_check(drive: DriveClient, google: respx.MockRouter) -> None:
    route = google.get(f"{FILES_URL}/{FOLDER}").respond(
        200,
        json={
            "name": "Inbox",
            "mimeType": "application/vnd.google-apps.folder",
            "trashed": False,
            "capabilities": {"canAddChildren": True},
        },
    )
    info = await drive.folder()
    assert info == FolderInfo("Inbox", is_folder=True, can_add_children=True, trashed=False)
    assert info.usable
    assert route.calls.last.request.url.params["supportsAllDrives"] == "true"
    assert drive.granted_scope == "x"


async def test_folder_check_not_visible(drive: DriveClient, google: respx.MockRouter) -> None:
    google.get(f"{FILES_URL}/{FOLDER}").mock(return_value=api_error(404, "notFound"))
    result = await drive.folder()
    assert isinstance(result, DriveFailure)
    assert result.problem is Problem.FOLDER


async def test_folder_check_a_file_is_not_usable(
    drive: DriveClient, google: respx.MockRouter
) -> None:
    google.get(f"{FILES_URL}/{FOLDER}").respond(200, json={"name": "x.pdf", "mimeType": PDF})
    info = await drive.folder()
    assert isinstance(info, FolderInfo)
    assert not info.usable


async def test_folder_check_failures(drive: DriveClient, google: respx.MockRouter) -> None:
    google.routes[0].mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    result = await drive.folder()
    assert isinstance(result, DriveFailure) and result.problem is Problem.AUTH
    google.routes[0].mock(return_value=token_ok())
    google.get(f"{FILES_URL}/{FOLDER}").side_effect = httpx.ConnectError("down")
    result = await drive.folder()
    assert isinstance(result, DriveFailure) and result.problem is Problem.UNAVAILABLE


async def test_account_email(drive: DriveClient, google: respx.MockRouter) -> None:
    route = google.get(ABOUT_URL).respond(200, json={"user": {"emailAddress": "s@example.test"}})
    assert await drive.account_email() == "s@example.test"
    assert route.calls.last.request.url.params["fields"] == "user(emailAddress)"
    route.mock(return_value=api_error(403, "insufficientPermissions"))
    result = await drive.account_email()
    assert isinstance(result, DriveFailure) and result.problem is Problem.SCOPE


async def test_account_email_failures(drive: DriveClient, google: respx.MockRouter) -> None:
    google.get(ABOUT_URL).side_effect = httpx.ConnectError("down")
    result = await drive.account_email()
    assert isinstance(result, DriveFailure) and result.problem is Problem.UNAVAILABLE
    google.routes[0].mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    drive._token = None
    result = await drive.account_email()
    assert isinstance(result, DriveFailure) and result.problem is Problem.AUTH


def test_credentials_from_secrets_need_all_three(clock: FakeClock) -> None:
    full = Secrets(
        drive_client_id=SecretStr("id"),
        drive_client_secret=SecretStr("sec"),
        drive_refresh_token=SecretStr("tok"),
    )
    creds = DriveCredentials.from_secrets(full)
    assert creds == DriveCredentials("id", "sec", "tok")
    assert "sec" not in repr(creds) and "tok" not in repr(creds)
    assert (
        DriveCredentials.from_secrets(full.model_copy(update={"drive_refresh_token": None})) is None
    )
    assert (
        DriveCredentials.from_secrets(full.model_copy(update={"drive_client_secret": None})) is None
    )
    assert DriveClient.from_config(AppConfig(), full, clock) is None
    inbox = AppConfig(planner_inbox=PlannerInboxConfig(folder_id=FOLDER))
    assert isinstance(DriveClient.from_config(inbox, full, clock), DriveClient)
    assert DriveClient.from_config(inbox, Secrets(), clock) is None
