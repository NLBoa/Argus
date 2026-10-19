import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx

from argus.collectors.base import CollectContext, RunStatus
from argus.collectors.http import AuthError
from argus.collectors.piazza_email import GMAIL, PiazzaEmailCollector, parse_message
from argus.config import AppConfig
from argus.domain.models import AuthorRole, Source
from tests.integration.conftest import fixture_bytes

TOKEN_URI = "https://oauth2.googleapis.com/token"


def gmail_message(mid: str, fixture: str, subject: str, date: str | None) -> dict[str, Any]:
    text = fixture_bytes("piazza_email", fixture)
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": "Piazza <no-reply@piazza.com>"},
    ]
    if date:
        headers.append({"name": "Date", "value": date})
    return {
        "id": mid,
        "internalDate": "1792411200000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<p>html</p>").decode()},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(text).decode().rstrip("=")},
                },
            ],
        },
    }


MESSAGES = {
    "m1": gmail_message(
        "m1",
        "instructor_note.txt",
        "[CMSC351] Instr Note: Midterm 1 logistics",
        "Sat, 17 Oct 2026 14:00:00 -0400",
    ),
    "m2": gmail_message(
        "m2", "student_question.txt", "New Question in CMSC351: Is the quiz open book?", None
    ),
    "m3": gmail_message(
        "m3", "other_class.txt", "[HIST289] Note: Reading list", "Sat, 17 Oct 2026 10:00:00 -0400"
    ),
}


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    p = tmp_path / "gmail-token.json"
    p.write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rtok",
                "token_uri": TOKEN_URI,
            }
        )
    )
    return p


def mock_gmail() -> respx.Route:
    token = respx.post(TOKEN_URI).respond(200, json={"access_token": "atok", "expires_in": 3599})
    respx.get(GMAIL).respond(
        200, json={"messages": [{"id": m} for m in MESSAGES], "resultSizeEstimate": 3}
    )
    for mid, msg in MESSAGES.items():
        respx.get(f"{GMAIL}/{mid}").respond(200, json=msg)
    return token


@respx.mock
async def test_collect_parses_piazza_emails(
    make_ctx: Callable[..., CollectContext], token_file: Path
) -> None:
    token = mock_gmail()
    ctx = make_ctx(gmail_token_path=token_file)
    collector = PiazzaEmailCollector()
    assert collector.enabled(ctx)
    result = await collector.collect(ctx)
    assert result.status == RunStatus.OK
    assert b"refresh_token=rtok" in token.calls[0].request.content
    posts = {p.external_id: p for p in result.posts}
    assert set(posts) == {"abc123xyz:101", "abc123xyz:110"}  # HIST289 is not configured

    note = posts["abc123xyz:101"]
    assert note.via == Source.PIAZZA_EMAIL
    assert note.platform == "piazza"
    assert note.title == "Midterm 1 logistics"
    assert note.author_role == AuthorRole.INSTRUCTOR
    assert note.posted_at == datetime(2026, 10, 17, 18, tzinfo=UTC)
    assert "cheat sheet" in note.body
    assert "Search or link" not in note.body
    assert note.url == "https://piazza.com/class/abc123xyz/post/101"

    question = posts["abc123xyz:110"]
    assert question.title == "Is the quiz open book?"
    assert question.author_role == AuthorRole.UNKNOWN
    assert question.posted_at == datetime.fromtimestamp(1792411200, tz=UTC)


@respx.mock
async def test_seen_messages_are_not_refetched(
    make_ctx: Callable[..., CollectContext], token_file: Path
) -> None:
    mock_gmail()
    first = await PiazzaEmailCollector().collect(make_ctx(gmail_token_path=token_file))
    states = {(Source.PIAZZA_EMAIL.value, k): v for k, v in first.state_updates.items()}
    second = await PiazzaEmailCollector().collect(make_ctx(states, gmail_token_path=token_file))
    assert second.status == RunStatus.UNCHANGED


@respx.mock
async def test_empty_inbox(make_ctx: Callable[..., CollectContext], token_file: Path) -> None:
    respx.post(TOKEN_URI).respond(200, json={"access_token": "atok"})
    respx.get(GMAIL).respond(200, json={"resultSizeEstimate": 0})
    result = await PiazzaEmailCollector().collect(make_ctx(gmail_token_path=token_file))
    assert result.status == RunStatus.UNCHANGED


@respx.mock
@pytest.mark.parametrize(
    "response", [(400, {"error": "invalid_grant"}), (200, {"token_type": "Bearer"})]
)
async def test_revoked_refresh_token_is_auth_error(
    make_ctx: Callable[..., CollectContext], token_file: Path, response: tuple[int, dict[str, str]]
) -> None:
    respx.post(TOKEN_URI).respond(response[0], json=response[1])
    with pytest.raises(AuthError):
        await PiazzaEmailCollector().collect(make_ctx(gmail_token_path=token_file))


def test_disabled_without_token_file(
    make_ctx: Callable[..., CollectContext], tmp_path: Path
) -> None:
    assert not PiazzaEmailCollector().enabled(make_ctx())
    assert not PiazzaEmailCollector().enabled(make_ctx(gmail_token_path=tmp_path / "missing.json"))


def test_message_without_link_is_ignored(app_config: AppConfig) -> None:
    msg = {
        "payload": {
            "mimeType": "text/plain",
            "headers": [],
            "body": {"data": base64.urlsafe_b64encode(b"hello").decode()},
        }
    }
    assert parse_message(msg, app_config) is None
