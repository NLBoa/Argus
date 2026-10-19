"""The simulated week over SMS: Twilio is faked at the HTTP layer, and the student answers
through the signed webhook (TODAY, DONE, LOG, SNOOZE, a replay, and one forged request).

Same fake sources as test_simulated_week. Checks: every text fits one GSM-7 segment, quiet
hours and the daily cap hold, one morning brief a day, replies change what gets sent next,
a replayed reply acts once, Twilio failures fall back to ntfy, and forged requests do nothing.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import respx
from pydantic import SecretStr

from argus.clock import FakeClock, to_local
from argus.collectors.canvas_ical import CanvasIcalCollector
from argus.collectors.gradescope import GradescopeCollector
from argus.collectors.http import Fetcher
from argus.collectors.piazza import PiazzaCollector
from argus.collectors.runner import SyncRunner
from argus.config import AppConfig, Secrets
from argus.domain.models import MeetingRecord, Weekday
from argus.notify.twilio_sms import API
from argus.reminders.text import SEGMENT, gsm_length, to_gsm7
from argus.store import repo
from argus.store.db import open_db, transaction
from argus.web.webhook import PATH, create_webhook_app, signature
from argus.worker import Worker
from tests.ai_fakes import FakeTransport
from tests.integration.simweek import (
    END,
    FEED,
    NTFY,
    QUIET,
    START,
    FakeGS,
    FakePiazza,
    at,
    feed,
    model,
)

SID = "AC11111111111111111111111111111111"
TWILIO = API.format(sid=SID)
TOKEN = "week-token"
STUDENT = "+13015550123"
HOOK = "https://argus.example.test" + PATH


async def test_simulated_week_over_sms(tmp_path: Any, app_config: AppConfig) -> None:
    clock = FakeClock(START)
    FakeGS.clock = clock
    FakePiazza.clock = clock
    db_path = tmp_path / "week.db"
    conn = open_db(db_path, clock)
    secrets = Secrets(
        canvas_ical_url=SecretStr(FEED),
        gradescope_email="s@example.test",
        gradescope_password=SecretStr("pw"),
        piazza_email="s@example.test",
        piazza_password=SecretStr("pw"),
        ntfy_topic=SecretStr("argus-week"),
        twilio_account_sid=SecretStr(SID),
        twilio_auth_token=SecretStr(TOKEN),
        twilio_from_number=SecretStr("+18005550100"),
        user_phone_number=SecretStr(STUDENT),
    )
    config = app_config.model_copy(
        update={"notifications": app_config.notifications.model_copy(update={"webhook_url": HOOK})}
    )
    lecture = MeetingRecord(
        "CMSC351", "lecture", (Weekday.TUE, Weekday.THU), at(0, 9, 30).time(), at(0, 10, 45).time()
    )
    with transaction(conn):
        ids = repo.sync_courses(conn, config, clock.now())
        repo.replace_meetings(conn, ids["CMSC351"], "umdio", [lecture], clock.now())
        repo.record_sync_run(
            conn, source="umdio", started_at=clock.now(), finished_at=clock.now(), status="ok"
        )

    texts: list[tuple[datetime, str]] = []
    pushes: list[datetime] = []
    twilio_down = (at(1, 7, 55), at(1, 8, 10))  # Twilio returns 503 around Tuesday's morning brief

    def twilio(request: httpx.Request) -> httpx.Response:
        if twilio_down[0] <= clock.now() < twilio_down[1]:
            return httpx.Response(503)
        texts.append((clock.now(), parse_qs(request.content.decode())["Body"][0]))
        return httpx.Response(201, json={"sid": "SM"})

    def ntfy(request: httpx.Request) -> httpx.Response:
        pushes.append(clock.now())
        return httpx.Response(200, json={"id": "x"})

    hook_app = create_webhook_app(
        config, secrets, clock, db_path=db_path, ai_transport=FakeTransport(responder=model)
    )
    replies: dict[str, str] = {}
    n_sid = iter(range(1, 1000))

    async def reply(
        hook: httpx.AsyncClient, body: str, sid: str | None = None, sign: bool = True
    ) -> str:
        params = {
            "MessageSid": sid or f"SMweek{next(n_sid)}",
            "From": STUDENT,
            "To": "+18005550100",
            "Body": body,
        }
        headers = {"X-Twilio-Signature": signature(TOKEN, HOOK, params)} if sign else {}
        r = await hook.post(PATH, data=params, headers=headers)
        if not sign:
            assert r.status_code == 403
            return ""
        assert r.status_code == 200
        m = re.search(r"<Message>(.*)</Message>", r.text)
        out = m.group(1) if m else ""
        replies[body] = out
        return out

    def number_for(listing: str, title_part: str) -> int:
        m = re.search(r"\[(\d)\] [A-Z]+\d+ " + re.escape(title_part), listing)
        assert m is not None, (listing, title_part)
        return int(m.group(1))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(FEED).mock(
            side_effect=lambda request: httpx.Response(200, content=feed(clock.now()))
        )
        mock.post(TWILIO).mock(side_effect=twilio)
        mock.post(NTFY).mock(side_effect=ntfy)
        mock.route(host="argus.example.test").pass_through()
        async with (
            httpx.AsyncClient() as client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=hook_app), base_url="https://argus.example.test"
            ) as hook,
        ):

            def runner(fetcher: Fetcher) -> SyncRunner:
                return SyncRunner(
                    conn,
                    config,
                    secrets,
                    clock,
                    fetcher,
                    collectors=[
                        CanvasIcalCollector(),
                        GradescopeCollector(FakeGS),
                        PiazzaCollector(FakePiazza),
                    ],
                )

            worker = Worker(
                conn,
                config,
                secrets,
                clock,
                client,
                runner_factory=runner,
                transport=FakeTransport(responder=model),
            )
            script = {
                at(
                    2, 12
                ): "wed",  # Wednesday noon: TODAY, then DONE Homework A (and a replay, and a forgery)
                at(3, 9): "thu-log",  # Thursday 9:00: TODAY, LOG 2h on Problem Set C
                at(3, 12): "thu-snooze",  # Thursday noon: SNOOZE Problem Set C for 3 hours
            }
            while clock.now() <= END:
                step = script.get(clock.now())
                if step == "wed":
                    listing = await reply(hook, "TODAY")
                    n = number_for(listing, "Homework A")
                    done = await reply(hook, f"DONE {n}", sid="SMdoneA")
                    assert done.startswith(f"Marked [{n}] CMSC351 Homework A done.")
                    assert await reply(hook, f"DONE {n}", sid="SMdoneA") == done  # Twilio retried
                    await reply(hook, "DONE 1", sign=False)  # forged: no signature
                elif step == "thu-log":
                    listing = await reply(hook, "TODAY")
                    n = (
                        number_for(listing, "PS C")
                        if "PS C" in listing
                        else number_for(listing, "Problem Set C")
                    )
                    assert (await reply(hook, f"LOG {n} 2h")).startswith(f"Logged 2h on [{n}]")
                elif step == "thu-snooze":
                    listing = await reply(hook, "TODAY")
                    n = (
                        number_for(listing, "PS C")
                        if "PS C" in listing
                        else number_for(listing, "Problem Set C")
                    )
                    assert "3-hour warning still comes" in await reply(hook, f"SNOOZE {n} 3h")
                await worker.tick()
                clock.advance(timedelta(minutes=15))

    items = {i.title: i for i in repo.list_items(conn)}
    notifications = repo.recent_notifications(conn, limit=500)
    by_rule_item: Counter[tuple[str, int]] = Counter(
        (n.rule, i) for n in notifications if n.status == "sent" for i in n.item_ids
    )

    # Every text is one GSM-7 segment.
    for when, body in texts:
        assert body == to_gsm7(body) and gsm_length(body) <= SEGMENT, (when, body)

    # Quiet hours: only 3-hour warnings. Daily cap: at most 6 capped texts a day.
    capped: Counter[Any] = Counter()
    for when, body in texts:
        local = to_local(when)
        warning = body.startswith(("Due in", "Due within"))
        if QUIET.contains(local.time()):
            assert warning, (local, body)
        if not warning and not body.startswith("At risk"):
            capped[local.date()] += 1
    assert max(capped.values()) <= 6

    # One morning brief a day, at or after 8:00.
    briefs = [to_local(w) for w, b in texts if b.startswith("Today: ")]
    assert len(briefs) == 6 and all(b.hour >= 8 for b in briefs)  # Tuesday's went by ntfy
    assert len({b.date() for b in briefs}) == 6

    # DONE stopped Homework A's warnings, once, even though the reply was replayed.
    hw_a = items["Homework A"]
    assert by_rule_item[("due_3h", hw_a.id)] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM inbound_messages WHERE message_sid = 'SMdoneA'"
        ).fetchone()[0]
        == 1
    )
    # LOG recorded once; SNOOZE kept Problem Set C quiet for 3 hours, but its 3-hour warning came.
    ps_c = items["Problem Set C"]
    assert conn.execute("SELECT COUNT(*) FROM time_logs WHERE source = 'sms'").fetchone()[0] == 1
    quiet_window = (at(3, 12), at(3, 15))
    for n in notifications:
        if ps_c.id in n.item_ids and n.sent_at and quiet_window[0] <= n.sent_at < quiet_window[1]:
            assert n.rule == "due_3h", n
    assert by_rule_item[("due_3h", ps_c.id)] == 1

    # Twilio's outage went to ntfy instead; nothing was lost or doubled.
    assert pushes and all(twilio_down[0] <= p < twilio_down[1] for p in pushes)
    sent = sum(1 for n in notifications if n.status == "sent")
    assert len(texts) + len(pushes) == sent
    # The forged request did nothing (no row, no action).
    assert conn.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 6
