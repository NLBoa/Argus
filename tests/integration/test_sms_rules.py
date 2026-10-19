"""Twilio delivery and fallback, and the Phase 6 rules end to end through the engine."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from argus.clock import FakeClock, local_datetime
from argus.collectors.http import Fetcher, make_client
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.notify.channels import DeliveryError, OutboundMessage
from argus.notify.twilio_sms import API, TwilioChannel
from argus.reminders.engine import ReminderEngine
from argus.reminders.guards import Verdict
from argus.reminders.rules import Rule
from argus.store.db import iso, open_db
from argus.worker import build_router

SID = "AC00000000000000000000000000000000"
MSG = OutboundMessage("Due today", "[1] HW 3", "Due today: [1] HW 3")
SECRETS = DEMO_SECRETS.model_copy(
    update={
        "twilio_account_sid": SecretStr(SID),
        "twilio_auth_token": SecretStr("tok"),
        "twilio_from_number": SecretStr("+18005550100"),
        "user_phone_number": SecretStr("+13015550123"),
        "ntfy_topic": SecretStr("argus-test-topic"),
    }
)
TWILIO = API.format(sid=SID)


async def test_twilio_posts_one_form_with_basic_auth() -> None:
    async with make_client() as client:
        ch = TwilioChannel(client, SID, "tok", "+18005550100", "+13015550123")
        with respx.mock:
            route = respx.post(TWILIO).mock(return_value=httpx.Response(201, json={"sid": "SM1"}))
            await ch.send(MSG)
        req = route.calls.last.request
        assert req.headers["authorization"].startswith("Basic ")
        assert parse_qs(req.content.decode()) == {
            "To": ["+13015550123"],
            "From": ["+18005550100"],
            "Body": ["Due today: [1] HW 3"],
        }
        with respx.mock:
            failing = respx.post(TWILIO).mock(return_value=httpx.Response(503))
            with pytest.raises(DeliveryError):
                await ch.send(MSG)
            assert failing.call_count == 1  # never retried here: a retry could double-text
        with respx.mock:
            respx.post(TWILIO).mock(return_value=httpx.Response(401))
            with pytest.raises(DeliveryError, match="credentials"):
                await ch.send(MSG)
        assert not TwilioChannel(client, SID, None, "+1", "+1").available()
        with pytest.raises(DeliveryError):
            await TwilioChannel(client, None, None, None, None).send(MSG)


async def test_router_prefers_sms_and_falls_back_to_ntfy() -> None:
    async with make_client() as client:
        router = build_router(DEMO_CONFIG, SECRETS, Fetcher(client, max_attempts=1))
        assert router.available() == ["sms", "ntfy"]
        with respx.mock:
            respx.post(TWILIO).mock(return_value=httpx.Response(201, json={}))
            assert (await router.send(MSG)).channel == "sms"
        with respx.mock:
            respx.post(TWILIO).mock(return_value=httpx.Response(500))
            respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(200))
            d = await router.send(MSG)
            assert (d.ok, d.channel) == (True, "ntfy")
        only_ntfy = build_router(
            DEMO_CONFIG,
            DEMO_SECRETS.model_copy(update={"ntfy_topic": SecretStr("t")}),
            Fetcher(client),
        )
        assert only_ntfy.available() == ["ntfy"]


@pytest.fixture
def conn(tmp_path: Path, clock: FakeClock) -> sqlite3.Connection:
    c = open_db(tmp_path / "r.db", clock)
    seed_demo(c, clock.now())
    return c


class Capture:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.name = "sms"

    def available(self) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)


def engine(conn: sqlite3.Connection, clock: FakeClock) -> tuple[ReminderEngine, Capture]:
    from argus.notify.router import Router

    cap = Capture()
    return ReminderEngine(conn, DEMO_CONFIG, clock, Router([cap])), cap


async def test_morning_brief_countdown_and_announced(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    # The demo midterm is Wednesday next week at 9:30; three days before is Sunday.
    sunday = local_datetime(clock.now().date() + timedelta(days=6), 8, 15)  # Sun 8:15 am
    clock.set(sunday)
    eng, cap = engine(conn, clock)
    snap = eng.snapshot(clock.now())
    assert snap.brief is not None and snap.brief.day == sunday.date()
    [countdown] = snap.countdowns
    assert countdown.days == 3 and countdown.weakest == (
        "Master theorem",
        "Memoization",
    )  # shaky, then untested
    assert snap.announced == []  # the demo's findings are older than two days
    outcomes = {o.rule: o for o in await eng.tick()}
    assert outcomes[Rule.MORNING_BRIEF].verdict is Verdict.SEND
    assert outcomes[Rule.EXAM_COUNTDOWN].verdict is Verdict.SEND
    texts = [m.sms_text for m in cap.sent]
    assert any(t.startswith("Today: ") for t in texts)
    assert any(
        "Exam in 3 days: [1] CMSC351 Midterm 1" in t and "Weakest: Master theorem, Memoization" in t
        for t in texts
    )
    again = {o.rule for o in await eng.tick() if o.verdict is Verdict.SEND}
    assert Rule.MORNING_BRIEF not in again and Rule.EXAM_COUNTDOWN not in again


async def test_brief_waits_for_its_time(conn: sqlite3.Connection, clock: FakeClock) -> None:
    clock.set(local_datetime(clock.now().date() + timedelta(days=1), 7, 0))
    eng, _ = engine(conn, clock)
    assert eng.snapshot(clock.now()).brief is None


async def test_recent_conflict_is_texted_with_ok_no(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    with conn:
        conn.execute("UPDATE findings SET created_at = ?", (iso(clock.now() - timedelta(hours=1)),))
    eng, cap = engine(conn, clock)
    [conflict] = [a for a in eng.snapshot(clock.now()).announced if a.status == "conflict"]
    assert conflict.official is not None and conflict.item_id == 5
    await eng.tick()
    [text] = [m.sms_text for m in cap.sent if m.title == "Dates disagree"]
    assert "Reply OK 1 to use the post's date" in text


async def test_snooze_defers_reminders_for_that_item(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    eng, _ = engine(conn, clock)
    with conn:
        conn.execute(
            "INSERT INTO snoozes (item_id, until, created_at) VALUES (2, ?, ?)",
            (iso(clock.now() + timedelta(hours=2)), iso(clock.now())),
        )
    ctx = eng.guard_context(clock.now())
    assert dict(ctx.snoozed) == {2: clock.now() + timedelta(hours=2)}
