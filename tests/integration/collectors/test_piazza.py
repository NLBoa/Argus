"""Contract tests: the real piazza-api adapter against synthetic Piazza RPC responses."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import responses
from requests import PreparedRequest

from argus.collectors.base import CollectContext, RunStatus
from argus.collectors.http import AuthError, FetchError
from argus.collectors.piazza import MAX_POSTS_PER_CYCLE, PiazzaCollector, is_candidate, parse_post
from argus.domain.models import AuthorRole, Source
from tests.integration.conftest import fixture_bytes

CREDS = {"piazza_email": "student@example.test", "piazza_password": "synthetic-pw"}
API = "https://piazza.com/logic/api"


class FakePiazza:
    """Dispatches Piazza's JSON-RPC by method (and cid) to fixture files."""

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.feed: dict[str, Any] = json.loads(fixture_bytes("piazza", "feed.json"))
        self.fail_method: str | None = None

    def __call__(self, request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        payload = json.loads(request.body or "{}")
        method = payload["method"]
        self.methods.append(method)
        if method == self.fail_method:
            return 200, {}, json.dumps({"result": None, "error": "boom"})
        if method == "network.get_my_feed":
            assert payload["params"]["nid"] == "abc123xyz"
            return 200, {}, json.dumps(self.feed)
        if method == "content.get":
            return (
                200,
                {},
                fixture_bytes("piazza", f"post_{payload['params']['cid']}.json").decode(),
            )
        if method == "network.get_users":
            return 200, {}, fixture_bytes("piazza", "users.json").decode()
        return 404, {}, "{}"


@pytest.fixture
def piazza() -> Iterator[tuple[responses.RequestsMock, FakePiazza]]:
    fake = FakePiazza()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get("https://piazza.com/main/csrf_token", body='CSRF_TOKEN = "synthetic-csrf";')
        rsps.post(
            "https://piazza.com/class",
            body="<html>ok</html>",
            headers={"Set-Cookie": "session_id=synthetic-session; Domain=piazza.com; Path=/"},
        )
        rsps.add_callback(responses.POST, API, callback=fake)
        yield rsps, fake


async def test_collect_fetches_only_staff_relevant_posts(
    piazza: tuple[responses.RequestsMock, FakePiazza], make_ctx: Callable[..., CollectContext]
) -> None:
    _, fake = piazza
    collector = PiazzaCollector()
    ctx = make_ctx(**CREDS)
    assert collector.enabled(ctx)
    result = await collector.collect(ctx)
    assert result.status == RunStatus.OK
    assert fake.methods.count("content.get") == 3  # kq3 (student chatter) is never fetched
    posts = {p.external_id: p for p in result.posts}
    assert set(posts) == {
        "abc123xyz:101",
        "abc123xyz:102",
        "abc123xyz:102:i_answer",
        "abc123xyz:104",
    }

    note = posts["abc123xyz:101"]
    assert note.author_role == AuthorRole.INSTRUCTOR
    assert note.pinned
    assert note.kind == "note"
    assert note.course_code == "CMSC351"
    assert (
        note.body == "Midterm 1 will be held in class on Thursday, Nov 5.\n"
        "It covers lectures 1\u201312. One cheat sheet allowed."
    )
    assert note.posted_at == datetime(2026, 10, 16, 15, tzinfo=UTC)
    assert note.edited_at == datetime(2026, 10, 17, 18, tzinfo=UTC)
    assert note.url == "https://piazza.com/class/abc123xyz/post/101"

    question = posts["abc123xyz:102"]
    assert question.author_role == AuthorRole.STUDENT
    answer = posts["abc123xyz:102:i_answer"]
    assert answer.kind == "instructor_answer"
    assert answer.author_role == AuthorRole.INSTRUCTOR
    assert "due Saturday" in answer.body
    assert posts["abc123xyz:104"].author_role == AuthorRole.TA


async def test_second_cycle_only_refetches_updated_posts(
    piazza: tuple[responses.RequestsMock, FakePiazza], make_ctx: Callable[..., CollectContext]
) -> None:
    _, fake = piazza
    first = await PiazzaCollector().collect(make_ctx(**CREDS))
    states = {(Source.PIAZZA.value, k): v for k, v in first.state_updates.items()}

    fake.methods.clear()
    unchanged = await PiazzaCollector().collect(make_ctx(states, **CREDS))
    assert unchanged.status == RunStatus.UNCHANGED
    assert "content.get" not in fake.methods

    fake.feed["result"]["feed"][3]["updated"] = "2026-10-18T20:00:00Z"
    fake.methods.clear()
    changed = await PiazzaCollector().collect(make_ctx(states, **CREDS))
    assert fake.methods.count("content.get") == 1
    assert [p.external_id for p in changed.posts] == ["abc123xyz:104"]


async def test_per_cycle_cap(
    piazza: tuple[responses.RequestsMock, FakePiazza], make_ctx: Callable[..., CollectContext]
) -> None:
    _, fake = piazza
    template = fake.feed["result"]["feed"][3]
    fake.feed["result"]["feed"] = [
        {**template, "id": "kq4", "updated": f"2026-10-18T14:{i:02d}:00Z"} for i in range(40)
    ]
    await PiazzaCollector().collect(make_ctx(**CREDS))
    assert fake.methods.count("content.get") == MAX_POSTS_PER_CYCLE


async def test_login_failure_is_auth_error(make_ctx: Callable[..., CollectContext]) -> None:
    with responses.RequestsMock() as rsps:
        rsps.get("https://piazza.com/main/csrf_token", body='CSRF_TOKEN = "x";')
        rsps.post(
            "https://piazza.com/class",
            body='<script>var ERROR_MSG = "Email or password incorrect";</script>',
        )
        with pytest.raises(AuthError):
            await PiazzaCollector().collect(make_ctx(**CREDS))


async def test_rpc_error_is_fetch_error(
    piazza: tuple[responses.RequestsMock, FakePiazza], make_ctx: Callable[..., CollectContext]
) -> None:
    piazza[1].fail_method = "network.get_my_feed"
    with pytest.raises(FetchError, match="RequestError"):
        await PiazzaCollector().collect(make_ctx(**CREDS))


def test_disabled_without_credentials(make_ctx: Callable[..., CollectContext]) -> None:
    assert not PiazzaCollector().enabled(make_ctx())


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"pin": 1, "subject": "Welcome"}, True),
        ({"tags": ["instructor-note"], "subject": "Hello"}, True),
        ({"has_i": 1, "subject": "Q"}, True),
        ({"subject": "Is the exam open book?"}, True),
        ({"subject": "lunch?", "content_snipet": "anyone"}, False),
    ],
)
def test_is_candidate(entry: dict[str, Any], expected: bool) -> None:
    assert is_candidate(entry) is expected


def test_post_without_history_is_rejected() -> None:
    with pytest.raises(ValueError, match="history"):
        parse_post({"nr": 1, "history": []}, nid="n", course_code="C", pinned=False, roles={})


def test_anonymous_post_role_unknown() -> None:
    post = {
        "nr": 5,
        "type": "question",
        "change_log": [{}],
        "history": [{"subject": "s", "content": "c", "created": "2026-10-18T00:00:00Z"}],
    }
    [rec] = parse_post(post, nid="n", course_code="C", pinned=False, roles={})
    assert rec.author_role == AuthorRole.UNKNOWN
    assert rec.kind == "question"
