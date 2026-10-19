"""Replies by text: parse, act, and answer in one SMS segment.

Order: the rule-based parser (reminders/commands.py) → the LLM, acted on only when its reading
is complete and confident (it is turned back into a command string and parsed again, so it
can never do anything the parser would not) → ask back once. Every answer says exactly what
changed, so a wrong reading is visible right away. Twilio's own keywords (STOP, HELP, ...)
get no reply from Argus.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus import announcements, estimates
from argus.ai import prompts
from argus.ai.client import AIClient, Tier
from argus.ai.schemas import CommandOut
from argus.capabilities import Capabilities, Feature
from argus.clock import Clock, local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import Item
from argus.logging import get_logger
from argus.planning import compute, replan
from argus.reminders.commands import Command, Verb, is_reserved, parse
from argus.reminders.text import fit, short, when, window_phrase
from argus.store import repo
from argus.store.db import iso, parse_iso, transaction

log = get_logger(__name__)
AI_CONFIDENCE = 0.8
ASK_BACK_WINDOW = timedelta(minutes=30)
AMBIGUOUS = timedelta(minutes=10)  # two numbered texts this close: [n] could mean either
# An AI reading never silences reminders or changes dates by itself (review R16).
CONFIRM_FIRST = (Verb.DONE, Verb.YES, Verb.NO, Verb.SNOOZE)
HELP = "Reply DONE 1, LOG 1 2h, SNOOZE 1 2h, EST 1 3h, OK 1, NO 1, or TODAY. Numbers are the [n] in my last text."
# Without a source of staff posts there are no announcement questions to answer OK/NO to (D055).
HELP_NO_POSTS = (
    "Reply DONE 1, LOG 1 2h, SNOOZE 1 2h, EST 1 3h, or TODAY. Numbers are the [n] in my last text."
)


def help_text(caps: Capabilities) -> str:
    return HELP if caps.has(Feature.STAFF_POSTS) else HELP_NO_POSTS


@dataclass(frozen=True)
class Outcome:
    reply: str  # empty: send no reply
    status: str  # done | unclear | failed
    via: str  # rules | ai | none
    command: Command | None = None
    item_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Context:
    """What [1], [2], ... mean, plus the text before it when that came close behind (review R7)."""

    ids: tuple[int, ...] = ()
    previous: tuple[int, ...] = ()  # only when sent within AMBIGUOUS of the latest


def numbered(conn: sqlite3.Connection) -> Context:
    """The item ids of the latest numbered text: an SMS Argus sent (replies only ever answer
    texts, so ntfy pushes don't count) or a TODAY reply (which wins ties)."""
    rows = [
        (parse_iso(r["at"]), 0, tuple(json.loads(r["item_ids"])))
        for r in conn.execute(
            "SELECT item_ids, COALESCE(sent_at, created_at) AS at FROM notifications"
            " WHERE status = 'sent' AND channel = 'sms' AND item_ids != '[]' ORDER BY at DESC, id DESC LIMIT 2"
        )
    ] + [
        (parse_iso(r["at"]), 1, tuple(json.loads(r["item_ids"])))
        for r in conn.execute(
            "SELECT item_ids, created_at AS at FROM inbound_messages WHERE item_ids != '[]'"
            " ORDER BY created_at DESC, id DESC LIMIT 2"
        )
    ]
    if not rows:
        return Context()
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    latest = rows[0]
    # A TODAY reply is the list the student just asked for: it is never ambiguous.
    unsolicited = latest[1] == 0
    prev = (
        rows[1] if unsolicited and len(rows) > 1 and latest[0] - rows[1][0] <= AMBIGUOUS else None
    )
    return Context(latest[2], prev[2] if prev else ())


class Inbox:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: AppConfig,
        clock: Clock,
        ai: AIClient,
        *,
        caps: Capabilities,
    ) -> None:
        self.conn = conn
        self.config = config
        self.caps = caps
        self.clock = clock
        self.ai = ai

    async def handle(self, sid: str, body: str) -> str:
        """Answer one inbound text. A replayed MessageSid gets the stored answer and does nothing."""
        seen = self.conn.execute(
            "SELECT reply FROM inbound_messages WHERE message_sid = ?", (sid,)
        ).fetchone()
        if seen is not None:
            return str(seen["reply"])
        outcome = await self._outcome(body)
        with transaction(self.conn):
            self.conn.execute(
                """INSERT OR IGNORE INTO inbound_messages
                   (message_sid, body, command, via, status, reply, item_ids, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid,
                    body[:1600],
                    outcome.command.describe() if outcome.command else None,
                    outcome.via,
                    outcome.status,
                    outcome.reply,
                    json.dumps(list(outcome.item_ids)),
                    iso(self.clock.now()),
                ),
            )
        return outcome.reply

    async def _outcome(self, body: str) -> Outcome:
        if is_reserved(body):
            return Outcome("", "done", "none")
        context = numbered(self.conn)
        cmd, via = parse(body), "rules"
        if cmd is None:
            cmd, via = await self._interpret(body, context), "ai"
        if cmd is None:
            return self._ask_back()
        if via == "ai" and cmd.verb in CONFIRM_FIRST:
            return self._confirm_first(cmd, context)
        try:
            reply, ids = self._run(cmd, context)
        except LookupError as e:
            return Outcome(fit(str(e)), "failed", via, cmd)
        except Exception:
            # Recorded like any answer, so Twilio's retry of this message gets it instead of a rerun.
            log.exception("sms command failed", extra={"command": cmd.describe()})
            return Outcome(
                fit("Something went wrong, so that may not have happened. Check the dashboard."),
                "failed",
                via,
                cmd,
            )
        prefix = f"Read as {cmd.describe()}. " if via == "ai" else ""
        return Outcome(fit(prefix + reply), "done", via, cmd, ids)

    def _confirm_first(self, cmd: Command, context: Context) -> Outcome:
        try:
            n, item = self._item(cmd, context)
        except LookupError as e:
            return Outcome(fit(str(e)), "unclear", "ai", cmd)
        exact = Command(cmd.verb, n, cmd.hours, cmd.duration).describe()
        reply = f"Did you mean {exact} ([{n}] {item.course_code} {short(item.title, 40)})? Reply {exact} to do it."
        return Outcome(fit(reply), "unclear", "ai", cmd)

    async def _interpret(self, body: str, context: Context) -> Command | None:
        if not self.ai.available:
            return None
        items = {i.id: i for i in repo.list_items(self.conn)}
        listed = [(n, items[i].title) for n, i in enumerate(context.ids, start=1) if i in items]
        result = await self.ai.structured(
            task="sms_command",
            tier=Tier.FAST,
            prompt=prompts.load("sms").render(items=listed, text=body[:500]),
            schema=CommandOut,
            max_tokens=300,
            effort="low",
        )
        out = result.value
        if out is None or out.command == "UNCLEAR" or out.confidence < AI_CONFIDENCE:
            return None
        canonical: str = out.command
        if out.number is not None:
            canonical += f" {out.number}"
        if out.command in ("LOG", "EST") and out.hours is not None:
            canonical += f" {out.hours:g}h"
        if out.command == "SNOOZE" and out.minutes is not None:
            canonical += f" {out.minutes}m"
        return parse(canonical)  # the same validation as a typed command

    def _ask_back(self) -> Outcome:
        last = self.conn.execute(
            "SELECT status, created_at FROM inbound_messages ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        recent = (
            last is not None and self.clock.now() - parse_iso(last["created_at"]) < ASK_BACK_WINDOW
        )
        if recent and last["status"] == "unclear":
            reply = "Still not sure what you meant, so nothing changed. You can make changes on the dashboard."
            return Outcome(fit(reply), "failed", "none")
        return Outcome(fit("Not sure what you meant. " + help_text(self.caps)), "unclear", "none")

    # ---------------------------------------------------------------- commands

    def _item(self, cmd: Command, ctx: Context) -> tuple[int, Item]:
        context = ctx.ids
        n = cmd.number
        if n is None:
            if len(context) != 1:
                raise LookupError(f"Which one? Reply {cmd.verb.value} 1 (the [n] in my last text).")
            n = 1
        if not 1 <= n <= len(context):
            raise LookupError(f"My last text has no [{n}]. Reply TODAY for a fresh list.")
        if ctx.previous and len(ctx.previous) >= n and ctx.previous[n - 1] != context[n - 1]:
            titles = [repo.get_item(self.conn, i) for i in (context[n - 1], ctx.previous[n - 1])]
            names = " and ".join(short(t.title, 30) for t in titles if t is not None)
            raise LookupError(
                f"My last two texts both have a [{n}] ({names}). Reply TODAY for one list, then use its numbers."
            )
        item = repo.get_item(self.conn, context[n - 1])
        if item is None:
            raise LookupError(f"[{n}] no longer exists. Reply TODAY for a fresh list.")
        return n, item

    def _run(self, cmd: Command, context: Context) -> tuple[str, tuple[int, ...]]:
        if cmd.verb is Verb.HELP:
            return help_text(self.caps), ()
        if cmd.verb is Verb.TODAY:
            return today_text(
                self.conn,
                self.config,
                self.clock.now(),
                tracking_sources=self.caps.tracking_sources,
            )
        n, item = self._item(cmd, context)
        label = f"[{n}] {item.course_code} {short(item.title, 50)}"
        now = self.clock.now()
        if cmd.verb is Verb.DONE:
            with transaction(self.conn):
                repo.set_user_done(self.conn, item.id, True, now)
                timer = repo.running_timer(self.conn)
                if timer and timer.item_id == item.id:
                    repo.stop_timer(self.conn, now)
                repo.refresh_statuses(self.conn, now, tracking_sources=self.caps.tracking_sources)
            replan(
                self.conn,
                self.config,
                self.clock,
                tracking_sources=self.caps.tracking_sources,
                force=True,
            )
            return f"Marked {label} done. No more reminders for it.", ()
        if cmd.verb is Verb.LOG:
            assert cmd.hours is not None
            with transaction(self.conn):
                repo.log_time(self.conn, item.id, cmd.hours, now, source="sms")
                repo.refresh_statuses(self.conn, now, tracking_sources=self.caps.tracking_sources)
            total = sum(log.hours(now) for log in repo.time_logs_for(self.conn, item.id))
            replan(
                self.conn,
                self.config,
                self.clock,
                tracking_sources=self.caps.tracking_sources,
                force=True,
            )
            return f"Logged {cmd.hours:g}h on {label} ({total:g}h so far). Plan updated.", ()
        if cmd.verb is Verb.SNOOZE:
            assert cmd.duration is not None
            until = now + cmd.duration
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO snoozes (item_id, until, created_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (item_id) DO UPDATE SET until = excluded.until, created_at = excluded.created_at",
                    (item.id, iso(until), iso(now)),
                )
            return f"Snoozed {label} until {when(until)}. The 3-hour warning still comes.", ()
        if cmd.verb is Verb.EST:
            assert cmd.hours is not None
            try:
                estimates.set_override(self.conn, item, cmd.hours, now)
            except ValueError as e:
                raise LookupError(str(e)) from e
            replan(
                self.conn,
                self.config,
                self.clock,
                tracking_sources=self.caps.tracking_sources,
                force=True,
            )
            return f"Set {label} to {cmd.hours:g}h. Plan updated.", ()
        return self._answer(cmd, item, label), ()

    def _answer(self, cmd: Command, item: Item, label: str) -> str:
        """OK/NO: the open question about this item (an announced change waiting for a decision)."""
        waiting = [
            f
            for f in repo.list_findings(self.conn, statuses=("conflict", "pending"))
            if f.item_id == item.id
        ]
        if not waiting:
            raise LookupError(f"Nothing about {label} is waiting for a yes or no.")
        asked = self._asked_about(item.id)
        if asked is not None:
            chosen = [f for f in waiting if f.id == asked]
        else:
            chosen = waiting if len(waiting) == 1 else []
        if not chosen:
            # Review R2: never guess which announcement the student means.
            raise LookupError(
                f"More than one change to {label} is waiting. Decide on the Announcements page."
            )
        f = chosen[0]
        now = self.clock.now()
        if cmd.verb is Verb.YES:
            message = announcements.confirm(
                self.conn, f.id, now, tracking_sources=self.caps.tracking_sources
            )
        else:
            message = announcements.dismiss(self.conn, f.id, now)
        replan(
            self.conn,
            self.config,
            self.clock,
            tracking_sources=self.caps.tracking_sources,
            force=True,
        )
        return message

    def _asked_about(self, item_id: int) -> int | None:
        """The finding of the latest announced-change text about this item, if any."""
        for r in self.conn.execute(
            """SELECT k.key, n.item_ids FROM notifications n JOIN notification_keys k ON k.notification_id = n.id
               WHERE n.rule = 'announced' AND n.status = 'sent' AND k.key LIKE 'finding:%'
               ORDER BY COALESCE(n.sent_at, n.created_at) DESC, n.id DESC"""
        ):
            if item_id in json.loads(r["item_ids"]):
                return int(r["key"].split(":")[1])
        return None


def today_text(
    conn: sqlite3.Connection,
    config: AppConfig,
    now: datetime,
    *,
    tracking_sources: frozenset[str],
) -> tuple[str, tuple[int, ...]]:
    """Today's open items (numbered) and what the next work window starts with. Also the
    morning brief's body."""
    day = to_local(now).date()
    end = local_datetime(day + timedelta(days=1), 0)
    due = [
        i
        for i in repo.list_items(
            conn, due_after=now - timedelta(hours=1), due_before=end, open_only=True
        )
        if i.due_at is not None and i.kind.takes_submission
    ][:4]
    parts = [
        f"[{n}] {i.course_code} {short(i.title, 28)} {when(i.due_at).split(' ', 1)[1]}"
        for n, i in enumerate(due, 1)
        if i.due_at
    ]
    head = "Due today: " + "; ".join(parts) + "." if parts else "Nothing due today."
    p = compute(conn, config, now, tracking_sources=tracking_sources)
    tail = ""
    if p.window is not None:
        alloc = p.allocation(p.window.window_id)
        first = alloc[0][0] if alloc else None
        what = "nothing planned yet"
        if first and first.startswith("item:"):
            ip = p.items.get(int(first.split(":", 1)[1]))
            what = f"{short(ip.item.title, 28)} first" if ip else what
        elif first:
            ip = p.items.get(int(first.split(":", 1)[1]))
            what = f"study for {short(ip.item.title, 24)} first" if ip else what
        tail = f" {window_phrase(p.window.start, p.window.end, now)}: {what}."
    return head + tail, tuple(i.id for i in due)
