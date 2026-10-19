"""Contract tests: the real gradescopeapi adapter against synthetic Gradescope HTML."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
import responses

from argus.collectors.base import CollectContext, RunStatus
from argus.collectors.gradescope import GradescopeCollector, GSAssignment, to_source_item
from argus.collectors.http import AuthError, FetchError
from argus.domain.models import ItemKind, Source
from tests.integration.conftest import fixture_bytes

GS = "https://www.gradescope.com"
NOT_AUTHORIZED = {"error": "You are not authorized to access this page."}
CREDS = {"gradescope_email": "student@example.test", "gradescope_password": "synthetic-pw"}


@pytest.fixture
def gs_site() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(GS, body=fixture_bytes("gradescope", "home.html"))
        rsps.post(f"{GS}/login", status=302, headers={"Location": f"{GS}/account"})
        rsps.get(f"{GS}/account", body=fixture_bytes("gradescope", "account.html"))
        for cid, page in (("765432", "course_cmsc351.html"), ("765433", "course_empty.html")):
            rsps.get(f"{GS}/courses/{cid}/assignments", status=401, json=NOT_AUTHORIZED)
            rsps.get(f"{GS}/courses/{cid}", body=fixture_bytes("gradescope", page))
        yield rsps


async def test_collect_parses_student_view(
    gs_site: responses.RequestsMock, make_ctx: Callable[..., CollectContext]
) -> None:
    collector = GradescopeCollector()
    ctx = make_ctx(**CREDS)
    assert collector.enabled(ctx)
    result = await collector.collect(ctx)
    assert result.status == RunStatus.OK
    assert set(result.complete_scopes) == {"CMSC351", "MATH240"}
    items = {i.title: i for i in result.items}
    assert set(items) == {"HW3", "Homework 2", "Programming Project 1", "Midterm 1 (paper)"}
    assert all(i.course_code == "CMSC351" for i in result.items)

    hw3 = items["HW3"]
    assert hw3.source == Source.GRADESCOPE
    assert hw3.external_id == "5550003"
    assert hw3.due_at == datetime(2026, 10, 23, 3, 30, tzinfo=UTC)
    assert hw3.late_due_at == datetime(2026, 10, 25, 3, 59, tzinfo=UTC)
    assert hw3.release_at == datetime(2026, 10, 16, 13, tzinfo=UTC)
    assert hw3.submitted is False and hw3.graded is False
    assert hw3.url == f"{GS}/courses/765432/assignments/5550003"

    hw2 = items["Homework 2"]
    assert hw2.graded is True and hw2.submitted is True and hw2.points == 30.0
    assert items["Programming Project 1"].submitted is True
    assert items["Programming Project 1"].graded is False

    midterm = items["Midterm 1 (paper)"]
    assert midterm.external_id.startswith("name-")
    assert midterm.kind == ItemKind.EXAM
    assert midterm.due_at is None
    assert midterm.url == f"{GS}/courses/765432"

    # Credentials travel only to gradescope.com.
    assert all(c.request.url.startswith(GS) for c in gs_site.calls)


async def test_unchanged_on_second_run(
    gs_site: responses.RequestsMock, make_ctx: Callable[..., CollectContext]
) -> None:
    first = await GradescopeCollector().collect(make_ctx(**CREDS))
    states = {(Source.GRADESCOPE.value, k): v for k, v in first.state_updates.items()}
    second = await GradescopeCollector().collect(make_ctx(states, **CREDS))
    assert second.status == RunStatus.UNCHANGED
    assert second.items == ()


async def test_bad_credentials_raise_auth_error(make_ctx: Callable[..., CollectContext]) -> None:
    with responses.RequestsMock() as rsps:
        rsps.get(GS, body=fixture_bytes("gradescope", "home.html"))
        rsps.post(f"{GS}/login", body="<html>Invalid email/password combination.</html>")
        with pytest.raises(AuthError):
            await GradescopeCollector().collect(make_ctx(**CREDS))


async def test_changed_login_page_is_an_error_not_a_crash(
    make_ctx: Callable[..., CollectContext],
) -> None:
    with responses.RequestsMock() as rsps:
        rsps.get(GS, body="<html>maintenance</html>")
        with pytest.raises(FetchError, match="page format changed"):
            await GradescopeCollector().collect(make_ctx(**CREDS))


async def test_changed_course_page_is_a_clear_error(
    gs_site: responses.RequestsMock, make_ctx: Callable[..., CollectContext]
) -> None:
    gs_site.replace(
        responses.GET,
        f"{GS}/courses/765432",
        body="<tr role='row'></tr><tr role='row'></tr><tr role='row'></tr>",
    )
    with pytest.raises(FetchError, match="page format changed"):
        await GradescopeCollector().collect(make_ctx(**CREDS))


def test_disabled_without_credentials(make_ctx: Callable[..., CollectContext]) -> None:
    assert not GradescopeCollector().enabled(make_ctx())
    assert not GradescopeCollector().enabled(make_ctx(gradescope_email="a@b.test"))


def test_naive_dates_are_read_as_local_time() -> None:
    a = GSAssignment("1", "HW", None, datetime(2026, 12, 1, 23, 59), None, None, None, None)  # noqa: DTZ001
    item = to_source_item(a, "CMSC351", "765432")
    assert item.due_at == datetime(2026, 12, 2, 4, 59, tzinfo=UTC)


def test_unknown_status_text_is_unknown() -> None:
    a = GSAssignment("1", "HW", None, None, None, "Late", None, None)
    item = to_source_item(a, "CMSC351", "765432")
    assert item.submitted is None and item.graded is None
