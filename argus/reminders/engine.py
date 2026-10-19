"""Reminder engine: snapshot the store, ask the rules, apply the guards, claim, send, record.

Idempotency is enforced twice: rules skip keys already recorded, and `claim_notification`
refuses any key that exists (UNIQUE), so a crash between claim and send can never double-send.
A claimed but undelivered message stays `pending` or `failed` and is retried (at most 3
attempts) while it has not expired.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from argus.ai.budget import budget_hit_this_month, month_key
from argus.clock import Clock, local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import ItemKind
from argus.exams import review_concepts
from argus.logging import get_logger
from argus.notify.channels import OutboundMessage
from argus.notify.router import Router
from argus.planner.freetime import meetings_on
from argus.planner.review import review_order
from argus.planner.risk import RiskLevel
from argus.planning import blocks_between, risk_map
from argus.reminders.guards import GuardContext, Verdict, check
from argus.reminders.rules import (
    Announced,
    Brief,
    Countdown,
    Reminder,
    Rule,
    SessionRef,
    Snapshot,
    SourceAlertState,
    candidates,
)
from argus.sms import today_text
from argus.store import repo
from argus.store.db import iso, parse_iso, transaction
from argus.vault.service import health as vault_health

log = get_logger(__name__)
BOOTSTRAP_GRACE = timedelta(minutes=10)
COUNTDOWN_DAYS = (7, 3, 1)
ANNOUNCED_KINDS = ("new_assessment", "date_change", "scope", "cancellation")
ANNOUNCED_FOR = timedelta(days=2)  # older findings are not texted (the Announcements page has them)
SOURCES = (
    "canvas_ical",
    "canvas_api",
    "canvas_modules",
    "gradescope",
    "piazza",
    "piazza_email",
    "umdio",
)
_TAGS = {
    Rule.AT_RISK: ("warning",),
    Rule.START_NUDGE: ("hourglass_flowing_sand",),
    Rule.DUE_3H: ("rotating_light",),
    Rule.DUE_24H: ("alarm_clock",),
    Rule.NEW_ITEM: ("inbox_tray",),
    Rule.SYSTEM_ALERT: ("warning",),
}


@dataclass(frozen=True, slots=True)
class TickOutcome:
    rule: Rule
    keys: tuple[str, ...]
    verdict: Verdict
    reason: str
    channel: str | None = None


class ReminderEngine:
    def __init__(self, conn: sqlite3.Connection, config: AppConfig, clock: Clock, router: Router):
        self.conn = conn
        self.config = config
        self.clock = clock
        self.router = router

    def snapshot(self, now: datetime) -> Snapshot:
        health = repo.source_health(self.conn, SOURCES)
        first = repo.first_success_at(self.conn)
        risk = risk_map(self.conn, self.config, now)
        sessions = [
            SessionRef(
                b.item_id, b.start, b.end, (b.label or "Review") if b.kind == "review" else None
            )
            for b in blocks_between(
                self.conn, now - timedelta(minutes=15), now + timedelta(minutes=1)
            )
            if b.kind in ("work", "review") and b.item_id is not None
        ]
        vault = vault_health(self.conn)
        sources = {
            s: SourceAlertState(s, h.failures, h.failing_since, h.last_error)
            for s, h in health.items()
        }
        sources["vault"] = SourceAlertState(
            "vault", vault.failures, vault.failing_since, vault.last_error
        )
        return Snapshot(
            at_risk=frozenset(i for i, r in risk.items() if r.level is RiskLevel.AT_RISK),
            sessions=sessions,
            ai_budget_month=month_key(now) if budget_hit_this_month(self.conn, now) else None,
            now=now,
            items=repo.list_items(self.conn),
            sent_keys=frozenset(repo.notification_keys(self.conn)),
            sources=sources,
            brief=self._brief(now),
            countdowns=self._countdowns(now),
            announced=self._announced(now),
            bootstrap_until=first + BOOTSTRAP_GRACE if first else None,
        )

    def _end_of_day(self, now: datetime) -> datetime:
        return local_datetime(to_local(now).date() + timedelta(days=1), 0)

    def _brief(self, now: datetime) -> Brief | None:
        local = to_local(now)
        if local.time() < self.config.schedule.morning_brief:
            return None
        body, ids = today_text(self.conn, now)
        return Brief(local.date(), body, ids, self._end_of_day(now))

    def _countdowns(self, now: datetime) -> list[Countdown]:
        today = to_local(now).date()
        out = []
        for item in repo.list_items(
            self.conn, due_after=now, due_before=now + timedelta(days=8), open_only=True
        ):
            if item.kind is not ItemKind.EXAM or item.due_at is None:
                continue
            days = (to_local(item.due_at).date() - today).days
            if days not in COUNTDOWN_DAYS:
                continue
            weakest = tuple(c.name for c in review_order(review_concepts(self.conn, item.id))[:2])
            out.append(Countdown(item, days, weakest, self._end_of_day(now)))
        return out

    def _announced(self, now: datetime) -> list[Announced]:
        items = {i.id: i for i in repo.list_items(self.conn)}
        out = []
        for f in repo.list_findings(self.conn, ("applied", "conflict", "pending")):
            if f.kind not in ANNOUNCED_KINDS or now - f.created_at > ANNOUNCED_FOR:
                continue
            item = items.get(f.item_id) if f.item_id is not None else None
            if f.status == "pending" and f.kind != "cancellation":
                continue  # held for review (student posts): the Announcements page shows them
            if item is not None and item.kind is ItemKind.ASSIGNMENT and f.kind != "cancellation":
                continue  # this rule is for quizzes and exams
            out.append(
                Announced(
                    f.id,
                    f.course_code,
                    f.kind,
                    item.title if item else f.title,
                    f.status,
                    f.date,
                    f.item_id,
                    item.due_at if item and f.status == "conflict" else None,
                    f.scope,
                    f.created_at + ANNOUNCED_FOR,
                )
            )
        return out

    def guard_context(self, now: datetime) -> GuardContext:
        today = to_local(now).date()
        start = local_datetime(today, 0)
        end = local_datetime(today + timedelta(days=1), 0)
        sent = repo.notifications_sent_between(self.conn, start, end)
        return GuardContext(
            quiet_hours=self.config.schedule.quiet_hours,
            busy=meetings_on(today, repo.list_meetings(self.conn)),
            sent_today_capped=sum(1 for n in sent if n.counts_toward_cap),
            daily_cap=self.config.notifications.daily_text_cap,
            muted=frozenset(repo.muted_rules(self.conn, now)),
            snoozed={
                r["item_id"]: parse_iso(r["until"])
                for r in self.conn.execute(
                    "SELECT item_id, until FROM snoozes WHERE until > ?", (iso(now),)
                )
            },
        )

    def _message(
        self, title: str, body: str, sms: str, priority: int, rule: Rule
    ) -> OutboundMessage:
        return OutboundMessage(
            title=title,
            body=body,
            sms_text=sms,
            priority=priority,
            tags=_TAGS.get(rule, ()),
            click_url=self.config.notifications.dashboard_url,
        )

    async def _deliver(self, notification_id: int, message: OutboundMessage) -> str | None:
        delivery = await self.router.send(message)
        with transaction(self.conn):
            repo.finish_notification(
                self.conn,
                notification_id,
                ok=delivery.ok,
                channel=delivery.channel,
                error=delivery.error,
                now=self.clock.now(),
            )
        return delivery.channel if delivery.ok else None

    async def tick(self) -> list[TickOutcome]:
        now = self.clock.now()
        outcomes: list[TickOutcome] = []
        ctx = self.guard_context(now)
        await self._retry_failed(now, ctx)
        ctx = self.guard_context(now)
        for r in candidates(self.snapshot(now)):
            verdict, reason = check(r, ctx, now)
            if verdict is not Verdict.SEND:
                outcomes.append(TickOutcome(r.rule, r.keys, verdict, reason))
                continue
            reason, channel = await self._send(r, now)
            outcomes.append(TickOutcome(r.rule, r.keys, verdict, reason, channel))
            if channel is not None and r.counts_toward_cap:
                ctx = replace(ctx, sent_today_capped=ctx.sent_today_capped + 1)
        for o in outcomes:
            if o.verdict is Verdict.SEND:
                log.info(
                    "reminder",
                    extra={"rule": o.rule.value, "channel": o.channel, "reason": o.reason},
                )
        return outcomes

    async def _send(self, r: Reminder, now: datetime) -> tuple[str, str | None]:
        """Claim then deliver. Returns (reason, channel); channel is None unless delivered."""
        nid = repo.claim_notification(
            self.conn,
            keys=r.keys,
            rule=r.rule.value,
            title=r.title,
            body=r.body,
            item_ids=r.item_ids,
            priority=r.priority,
            counts_toward_cap=r.counts_toward_cap,
            now=now,
            expires_at=r.expires_at,
        )
        if nid is None:
            return "already sent", None
        channel = await self._deliver(
            nid, self._message(r.title, r.body, r.sms, r.priority, r.rule)
        )
        return ("sent", channel) if channel else ("delivery failed", None)

    async def _retry_failed(self, now: datetime, ctx: GuardContext) -> None:
        for n in repo.retryable_notifications(self.conn, now):
            if n.created_at > now - timedelta(seconds=1) and n.attempts == 0:
                continue  # claimed this very instant by another tick
            rule = Rule(n.rule)
            again = Reminder(
                rule,
                (n.key,),
                n.title,
                n.body,
                item_ids=n.item_ids,
                priority=n.priority,
                bypass_quiet=rule is Rule.DUE_3H,
                counts_toward_cap=n.counts_toward_cap,
                expires_at=n.expires_at,
            )
            if check(again, ctx, now)[0] is not Verdict.SEND:
                continue  # quiet hours, class, mute, snooze, or the cap hold a retry back too
            await self._deliver(n.id, self._message(n.title, n.body, again.sms, n.priority, rule))
