"""The Twilio webhook: signatures, sender check, every command, replays, the LLM fallback,
and the separation between the public webhook app and the tailnet-only dashboard."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from argus.clock import FakeClock
from argus.config import AppConfig
from argus.demo import DEMO_CONFIG, DEMO_SECRETS, seed_demo
from argus.store import repo
from argus.store.db import open_db, transaction
from argus.web.app import create_app
from argus.web.webhook import EMPTY, PATH, create_webhook_app, signature, valid_signature
from tests.ai_fakes import FakeTransport, ok

TOKEN = "test-auth-token"
URL = "https://argus.example.test" + PATH
STUDENT = "+13015550123"  # 555 numbers are fictional
SECRETS = DEMO_SECRETS.model_copy(
    update={
        "twilio_auth_token": SecretStr(TOKEN),
        "user_phone_number": SecretStr(STUDENT),
    }
)
HW3, PS6, MIDTERM = 2, 6, 5


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    notifications = DEMO_CONFIG.notifications.model_copy(update={"webhook_url": URL})
    paths = DEMO_CONFIG.paths.model_copy(
        update={"data_dir": tmp_path / "var", "vault_dir": tmp_path / "vault"}
    )
    return DEMO_CONFIG.model_copy(update={"notifications": notifications, "paths": paths})


@pytest.fixture
def db_path(tmp_path: Path, clock: FakeClock) -> Path:
    path = tmp_path / "w.db"
    conn = open_db(path, clock)
    seed_demo(conn, clock.now())
    with transaction(conn):  # the last text Argus sent numbered HW3 [1] and Problem Set 6 [2]
        nid = repo.claim_notification(
            conn,
            keys=["test:numbered"],
            rule="due_24h",
            title="Due today",
            body="[1] HW3 [2] PS6",
            item_ids=[HW3, PS6],
            priority=4,
            counts_toward_cap=True,
            now=clock.now(),
        )
        assert nid is not None
        repo.finish_notification(conn, nid, ok=True, channel="sms", error=None, now=clock.now())
    conn.close()
    return path


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport(
        responder=lambda req: ok(
            {
                "command": "UNCLEAR",
                "number": None,
                "hours": None,
                "minutes": None,
                "confidence": 0.2,
            }
        )
    )


@pytest.fixture
def client(
    config: AppConfig, db_path: Path, clock: FakeClock, transport: FakeTransport
) -> Iterator[TestClient]:
    app = create_webhook_app(config, SECRETS, clock, db_path=db_path, ai_transport=transport)
    with TestClient(app) as c:
        yield c


def db(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


_sid = iter(range(1000, 10_000))


def text(
    client: TestClient,
    body: str,
    *,
    sid: str | None = None,
    sender: str = STUDENT,
    sign: Any = True,
) -> Any:
    params = {
        "MessageSid": sid or f"SM{next(_sid)}",
        "From": sender,
        "To": "+18005550100",
        "Body": body,
    }
    headers = {}
    if sign is True:
        headers["X-Twilio-Signature"] = signature(TOKEN, URL, params)
    elif isinstance(sign, str):
        headers["X-Twilio-Signature"] = sign
    return client.post(PATH, data=params, headers=headers)


def reply(r: Any) -> str:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    body: str = r.text
    if body == EMPTY:
        return ""
    return body.split("<Message>")[1].split("</Message>", maxsplit=1)[0]


# ---------------------------------------------------------------- signatures and senders


def test_signature_matches_twilios_documented_example() -> None:
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    assert (
        signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params)
        == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    )
    assert not valid_signature(
        "12345", "https://mycompany.com/myapp.php", params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    )
    assert not valid_signature("12345", "https://x", params, None)


def test_unsigned_and_badly_signed_requests_are_rejected(client: TestClient, db_path: Path) -> None:
    assert text(client, "DONE 1", sign=None).status_code == 403
    assert text(client, "DONE 1", sign="bm90IGEgc2lnbmF0dXJl").status_code == 403
    tampered = {"MessageSid": "SMx", "From": STUDENT, "To": "+18005550100", "Body": "DONE 1"}
    sig = signature(TOKEN, URL, tampered)
    tampered["Body"] = "DONE 2"
    assert client.post(PATH, data=tampered, headers={"X-Twilio-Signature": sig}).status_code == 403
    assert db(db_path).execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 0
    assert repo.get_item(db(db_path), HW3).status.is_open  # type: ignore[union-attr]


def test_unknown_sender_is_ignored(client: TestClient, db_path: Path) -> None:
    assert reply(text(client, "DONE 1", sender="+12025550199")) == ""
    assert db(db_path).execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 0


def test_not_configured_and_missing_sid(
    config: AppConfig, db_path: Path, clock: FakeClock, client: TestClient
) -> None:
    bare = create_webhook_app(config, DEMO_SECRETS, clock, db_path=db_path)
    with TestClient(bare) as c:
        assert c.post(PATH, data={"Body": "x"}).status_code == 503
    params = {"From": STUDENT, "Body": "DONE 1"}
    r = client.post(
        PATH, data=params, headers={"X-Twilio-Signature": signature(TOKEN, URL, params)}
    )
    assert r.status_code == 400


def test_twilio_keywords_get_no_reply_from_argus(client: TestClient) -> None:
    assert reply(text(client, "STOP")) == ""
    assert reply(text(client, "help")) == ""


# ---------------------------------------------------------------- commands


def test_done(client: TestClient, db_path: Path) -> None:
    out = reply(text(client, "DONE 1"))
    assert out.startswith("Marked [1] CMSC351 Homework 3 done.")
    assert (
        db(db_path).execute("SELECT user_done_at FROM items WHERE id = ?", (HW3,)).fetchone()[0]
        is not None
    )


def test_log_once_even_when_replayed(client: TestClient, db_path: Path) -> None:
    first = reply(text(client, "log 2 1.5h", sid="SMreplay"))
    assert first.startswith("Logged 1.5h on [2] MATH240 Problem Set 6")
    again = reply(text(client, "log 2 1.5h", sid="SMreplay"))
    assert again == first
    rows = (
        db(db_path)
        .execute("SELECT COUNT(*) FROM time_logs WHERE item_id = ? AND source = 'sms'", (PS6,))
        .fetchone()[0]
    )
    assert rows == 1


def test_snooze_and_estimate(client: TestClient, db_path: Path) -> None:
    out = reply(text(client, "SNOOZE 1 2h"))
    assert (
        out.startswith("Snoozed [1] CMSC351 Homework 3 until")
        and "3-hour warning still comes" in out
    )
    assert (
        db(db_path).execute("SELECT until FROM snoozes WHERE item_id = ?", (HW3,)).fetchone()
        is not None
    )
    assert reply(text(client, "EST 2 4")).startswith("Set [2] MATH240 Problem Set 6 to 4h.")
    row = (
        db(db_path)
        .execute(
            "SELECT p50_hours FROM estimates WHERE item_id = ? AND source = 'user' ORDER BY id DESC",
            (PS6,),
        )
        .fetchone()
    )
    assert row[0] == 4.0


def test_skip_first_and_left_change_the_queue_once_even_when_replayed(
    client: TestClient, db_path: Path
) -> None:
    out = reply(text(client, "SKIP 1", sid="SMskip"))
    assert out.startswith("Skipped [1] CMSC351 Homework 3 for tonight 7-11pm.")
    assert reply(text(client, "SKIP 1", sid="SMskip")) == out  # Twilio's retry: same answer
    rows = db(db_path).execute("SELECT entry, skip_window FROM queue_prefs").fetchall()
    assert [(r["entry"], r["skip_window"]) for r in rows] == [(f"item:{HW3}", "2026-10-19T19:00")]

    assert reply(text(client, "FIRST 2")).startswith("Pinned [2] ")
    pin = (
        db(db_path)
        .execute("SELECT pin_rank, pin_kind FROM queue_prefs WHERE entry = ?", (f"item:{PS6}",))
        .fetchone()
    )
    assert (pin["pin_rank"], pin["pin_kind"]) == (0, "pin")

    assert reply(text(client, "LEFT 1 2h")).startswith(
        "Set [1] CMSC351 Homework 3 to about 2h left."
    )
    marks = db(db_path).execute("SELECT item_id, hours, source FROM remaining_marks").fetchall()
    assert [tuple(m) for m in marks] == [(HW3, 2.0, "sms")]


def test_queue_verbs_without_a_number_go_to_the_ai_and_ask_back(
    client: TestClient, db_path: Path
) -> None:
    assert reply(text(client, "skip it tonight")).startswith("Not sure what you meant.")
    assert db(db_path).execute("SELECT COUNT(*) FROM queue_prefs").fetchone()[0] == 0


def test_today_renumbers_and_ok_no_answer_the_waiting_question(
    client: TestClient, db_path: Path
) -> None:
    today = reply(text(client, "TODAY"))
    assert today.startswith("Due today: [1]")
    ids = json.loads(
        db(db_path).execute("SELECT item_ids FROM inbound_messages ORDER BY id DESC").fetchone()[0]
    )
    assert ids and ids[0] in (HW3, PS6)
    # Point [1] at the midterm, as the "Dates disagree" text would.
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE inbound_messages SET item_ids = ?", (json.dumps([MIDTERM]),))
        c.execute("UPDATE notifications SET item_ids = ?", (json.dumps([MIDTERM]),))
    assert "waiting" not in reply(text(client, "NO 1"))
    assert (
        db(db_path).execute("SELECT status FROM findings WHERE id = 1").fetchone()[0] == "dismissed"
    )
    assert "Nothing about [1]" in reply(text(client, "OK 1"))


def test_ok_confirms_the_announced_date(client: TestClient, db_path: Path) -> None:
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE notifications SET item_ids = ?", (json.dumps([MIDTERM]),))
    reply(text(client, "Y"))  # one item in context: the number is implied
    assert (
        db(db_path).execute("SELECT status FROM findings WHERE id = 1").fetchone()[0] == "confirmed"
    )


def test_bad_numbers_and_help(client: TestClient) -> None:
    assert "has no [5]" in reply(text(client, "DONE 5"))
    assert reply(text(client, "DONE")).startswith("Which one?")
    assert reply(text(client, "?")).startswith("Reply DONE 1")


# ---------------------------------------------------------------- the fallback and asking back


def test_free_text_goes_to_the_llm_and_the_reply_says_how_it_was_read(
    client: TestClient, db_path: Path, transport: FakeTransport
) -> None:
    transport.responder = lambda req: ok(
        {"command": "DONE", "number": 1, "hours": None, "minutes": None, "confidence": 0.95}
    )
    # Review R16: an AI reading never marks work done by itself; it asks for the exact command.
    out = reply(text(client, "finished the hw lol"))
    assert out == "Did you mean DONE 1 ([1] CMSC351 Homework 3)? Reply DONE 1 to do it."
    [call] = transport.calls
    assert "[1] Homework 3" in call["user"] and "finished the hw lol" in call["user"]
    assert (
        db(db_path).execute("SELECT user_done_at FROM items WHERE id = ?", (HW3,)).fetchone()[0]
        is None
    )
    assert reply(text(client, "DONE 1")).startswith("Marked [1] CMSC351 Homework 3 done.")
    transport.responder = lambda req: ok(
        {"command": "LOG", "number": 2, "hours": 0.75, "minutes": None, "confidence": 0.9}
    )
    assert reply(text(client, "spent 45 min on the problem set")).startswith(
        "Read as LOG 2 0.75h. Logged"
    )


def test_llm_output_is_validated_like_typed_commands(
    client: TestClient, db_path: Path, transport: FakeTransport
) -> None:
    transport.responder = lambda req: ok(
        {"command": "LOG", "number": 1, "hours": 30, "minutes": None, "confidence": 0.99}
    )
    assert reply(text(client, "worked on it all weekend")).startswith("Not sure what you meant.")
    assert (
        db(db_path).execute("SELECT COUNT(*) FROM time_logs WHERE source = 'sms'").fetchone()[0]
        == 0
    )


def test_unclear_asks_back_once(client: TestClient, clock: FakeClock) -> None:
    assert reply(text(client, "hmm")).startswith("Not sure what you meant.")
    assert reply(text(client, "what")).startswith("Still not sure")
    clock.advance(__import__("datetime").timedelta(hours=1))
    assert reply(text(client, "hmm")).startswith("Not sure what you meant.")


def test_without_ai_unknown_text_asks_back(
    config: AppConfig, db_path: Path, clock: FakeClock
) -> None:
    app = create_webhook_app(
        config, SECRETS.model_copy(update={"anthropic_api_key": None}), clock, db_path=db_path
    )
    with TestClient(app) as c:
        assert reply(text(c, "i'm done with homework 3")).startswith("Not sure what you meant.")


# ---------------------------------------------------------------- separation


def test_only_the_webhook_is_public(config: AppConfig, db_path: Path, clock: FakeClock) -> None:
    hook = create_webhook_app(config, SECRETS, clock, db_path=db_path)
    assert {getattr(r, "path", None) for r in hook.routes} == {PATH, "/healthz"}
    dashboard = create_app(config, SECRETS, clock, db_path=db_path)
    assert PATH not in {getattr(r, "path", None) for r in dashboard.routes}
    with TestClient(dashboard) as c:
        assert c.post(PATH, data={"Body": "DONE 1"}).status_code == 403  # cross-origin guard


def test_numbers_come_from_texts_and_ambiguity_is_refused(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    from datetime import timedelta

    conn = db(db_path)
    with transaction(conn):
        for key, ids, channel in (("test:push", [MIDTERM], "ntfy"), ("test:sms2", [PS6], "sms")):
            nid = repo.claim_notification(
                conn,
                keys=[key],
                rule="new_item",
                title="t",
                body="b",
                item_ids=ids,
                priority=3,
                counts_toward_cap=True,
                now=clock.now() + timedelta(minutes=5),
            )
            assert nid is not None
            repo.finish_notification(
                conn,
                nid,
                ok=True,
                channel=channel,
                error=None,
                now=clock.now() + timedelta(minutes=5),
            )
    clock.advance(timedelta(minutes=6))
    # Review R7: the ntfy push is ignored; the two texts 5 minutes apart give [1] different meanings.
    out = reply(text(client, "DONE 1"))
    assert (
        out.startswith("My last two texts both have a [1]")
        and "Problem Set 6" in out
        and "Homework 3" in out
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM items WHERE user_done_at IS NOT NULL").fetchone()[0] == 0
    )
    listing = reply(text(client, "TODAY"))
    assert listing.startswith("Due today: [1]")
    assert reply(text(client, "DONE 1")).startswith("Marked [1]")  # the fresh list is unambiguous


def test_ok_answers_the_finding_that_was_texted(
    client: TestClient, db_path: Path, clock: FakeClock
) -> None:
    conn = db(db_path)
    with transaction(conn):  # a second change to the midterm is waiting, newer than finding 1
        conn.execute(
            """INSERT INTO findings (post_id, course_id, kind, title, date, date_only, scope, quote, confidence,
                                     item_id, status, reason, created_at)
               SELECT post_id, course_id, kind, title, '2026-11-10T14:30:00Z', date_only, scope, quote, confidence,
                      item_id, status, reason, created_at FROM findings WHERE id = 1"""
        )
        conn.execute("UPDATE notifications SET item_ids = ?", (json.dumps([MIDTERM]),))
    # Review R2: with two waiting and none texted, Argus refuses to guess.
    assert "More than one change" in reply(text(client, "OK 1"))
    with transaction(conn):
        nid = repo.claim_notification(
            conn,
            keys=["finding:1:conflict"],
            rule="announced",
            title="Dates disagree",
            body="b",
            item_ids=[MIDTERM],
            priority=4,
            counts_toward_cap=True,
            now=clock.now(),
        )
        assert nid is not None
        repo.finish_notification(conn, nid, ok=True, channel="sms", error=None, now=clock.now())
    out = reply(text(client, "OK 1"))
    assert out.startswith("Using the announced date for Midterm 1: ")
    statuses = dict(conn.execute("SELECT id, status FROM findings").fetchall())
    assert statuses[1] == "confirmed" and statuses[max(statuses)] == "conflict"


def test_oversized_or_non_form_bodies_are_refused_before_parsing(
    client: TestClient, db_path: Path
) -> None:
    """Review R14: the one public route never parses a large or multipart body."""
    big = {"MessageSid": "SMbig", "From": STUDENT, "Body": "x" * 20_000}
    assert client.post(PATH, data=big).status_code == 413
    files = client.post(PATH, files={"f": ("a.bin", b"\0" * 1000)}, data={"Body": "DONE 1"})
    assert files.status_code == 415
    assert (
        client.post(PATH, content=b"{}", headers={"content-type": "application/json"}).status_code
        == 415
    )
    assert db(db_path).execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 0


def test_a_crash_is_answered_and_recorded_so_retries_do_not_rerun(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argus.sms

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(argus.sms, "replan", boom)
    out = reply(text(client, "EST 1 3h", sid="SMcrash"))
    assert out.startswith("Something went wrong")
    assert (
        reply(text(client, "EST 1 3h", sid="SMcrash")) == out
    )  # Twilio's retry gets the same answer
    assert (
        db(db_path)
        .execute("SELECT status FROM inbound_messages WHERE message_sid = 'SMcrash'")
        .fetchone()[0]
        == "failed"
    )
