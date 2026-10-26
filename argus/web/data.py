"""Page data loaders: read the store, build view models. One function per view."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from argus.capabilities import Capabilities, Feature
from argus.clock import local_datetime, to_local
from argus.config import AppConfig
from argus.domain.models import Item, ItemKind, ItemStatus
from argus.estimates import (
    counts,
    effective_estimates,
    estimate_sources,
    estimate_state,
    finished_rows,
)
from argus.planner.calibration import Effective, Finished, factor
from argus.planner.freetime import busy_on
from argus.planner.risk import RiskLevel
from argus.planner.windows import hours_between, overlapping_windows, window_spans
from argus.planning import (
    Planning,
    compute,
    explanation,
    notice_minutes,
    risk_views,
    workload_curve,
)
from argus.reminders.engine import SOURCES
from argus.reminders.rules import Rule
from argus.store import repo
from argus.vault.service import health as vault_health
from argus.vault.service import item_note_path
from argus.web.format import clock_time, due_text, hours, long_date, relative
from argus.web.settings_form import WindowRow, describe, rows_from_windows
from argus.web.study_data import obsidian_link
from argus.web.viewmodels import (
    UNCONFIRMED_ASK_FOR,
    WEEK_DAYS,
    CourseView,
    DayGroup,
    FindingView,
    ItemView,
    SourceStatusView,
    StaleNotice,
    TimelineView,
    WorkDayGroup,
    WorkloadChart,
    WorkloadDay,
    course_views,
    group_by_day,
    item_view,
    source_status,
    stale_notices,
    timeline,
    today_summary,
    week_groups,
    workload_chart,
    workload_days,
)

PAST_DUE_WINDOW = timedelta(days=3)
CHANGED = "The list changed since you loaded it, so nothing moved. Try again."


def source_statuses(
    conn: sqlite3.Connection, configured: Mapping[str, bool], now: datetime
) -> list[SourceStatusView]:
    health = repo.source_health(conn, SOURCES)
    return [source_status(s, health.get(s), configured.get(s, False), now) for s in SOURCES]


def notices(
    conn: sqlite3.Connection, configured: Mapping[str, bool], now: datetime
) -> list[StaleNotice]:
    return stale_notices(source_statuses(conn, configured, now))


def _views(
    conn: sqlite3.Connection,
    config: AppConfig,
    now: datetime,
    *,
    caps: Capabilities,
    plan: Planning | None = None,
    **filters: object,
) -> list[ItemView]:
    courses = course_views(config)
    timer = repo.running_timer(conn)
    running = timer.item_id if timer else None
    plan = plan or compute(conn, config, now, tracking_sources=caps.tracking_sources)
    risk = risk_views(plan)
    return [
        item_view(
            i,
            now,
            courses,
            tracking_sources=caps.tracking_sources,
            running_item_id=running,
            risk=risk[i.id].level if i.id in risk else None,
            left=plan.items[i.id].remaining if i.id in plan.items else None,
        )
        for i in repo.list_items(conn, **filters)  # type: ignore[arg-type]
    ]


@dataclass(frozen=True)
class TodayData:
    date_text: str
    summary: str
    last_text: str  # "Monday, October 26": the week's last work day, for the empty state
    groups: list[WorkDayGroup]  # working on, past due, then each work day of the week (D072)


def _past_due(views: Sequence[ItemView], now: datetime) -> tuple[list[ItemView], list[ItemView]]:
    """Recent work past its deadline: (to confirm, overdue)."""
    recent = now - PAST_DUE_WINDOW
    # D056: past-due work no source can report a submission for asks, it doesn't accuse.
    unconfirmed = [
        v
        for v in views
        if v.risk is RiskLevel.UNCONFIRMED
        and v.due_at
        and now - UNCONFIRMED_ASK_FOR <= v.due_at < now
    ]
    overdue = [
        v
        for v in views
        if v.due_at
        and recent <= v.due_at < now
        and v.risk is not RiskLevel.UNCONFIRMED
        and (v.status.is_open or v.status == ItemStatus.MISSED)
        and v.kind.takes_submission
    ]
    return unconfirmed, overdue


def _widget_summary(views: Sequence[ItemView], plan: Planning, now: datetime) -> str:
    """The widget feed's line (D071): "2 due today, 1 tomorrow, 1 past due"."""
    today = to_local(now).date()
    tomorrow = local_datetime(today + timedelta(days=1), 0)
    after = local_datetime(today + timedelta(days=2), 0)
    queued = {e.item_id for e in plan.queue if e.kind == "work"}
    unconfirmed, overdue = _past_due(views, now)
    overdue = [v for v in overdue if v.id not in queued]
    open_today = sum(
        1 for v in views if v.due_at and now <= v.due_at < tomorrow and v.status.is_open
    )
    soon = sum(
        1
        for v in views
        if v.due_at and tomorrow <= v.due_at < after and v.kind.takes_submission and not v.done
    )
    parts = [f"{open_today} due today" if open_today else "Nothing left due today"]
    if soon:
        parts.append(f"{soon} tomorrow")
    if overdue:
        parts.append(f"{len(overdue)} past due")
    if unconfirmed:
        parts.append(f"{len(unconfirmed)} to confirm")
    return ", ".join(parts)


def load_today(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> TodayData:
    """Today (D072): what's due, in order, filed by the day it's worked on, through the same
    weekday next week. No ranking: the student picks when to do it."""
    today = to_local(now).date()
    views = _views(conn, config, now, caps=caps)
    unconfirmed, overdue = _past_due(views, now)
    past = sorted(unconfirmed + overdue, key=lambda v: (v.due_at, v.title))
    week = week_groups(views, now)
    summary = today_summary(week, today, past_due=len(overdue), to_confirm=len(unconfirmed))
    timer = repo.running_timer(conn)
    running = next((v for v in views if timer and v.id == timer.item_id), None)
    if running is not None:
        week = [replace(g, items=tuple(v for v in g.items if v.id != running.id)) for g in week]
        past = [v for v in past if v.id != running.id]
    lead = [
        WorkDayGroup(None, label, tuple(vs))
        for label, vs in (("Working on", [running] if running else []), ("Past due", past))
        if vs
    ]
    return TodayData(
        date_text=long_date(today),
        summary=summary,
        last_text=long_date(today + timedelta(days=WEEK_DAYS)),
        groups=lead + [g for g in week if g.items],
    )


def load_upcoming(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    now: datetime,
    *,
    days: int,
    course: str | None,
) -> list[DayGroup]:
    views = _views(
        conn, config, now, caps=caps, due_after=now, due_before=now + timedelta(days=days)
    )
    if course:
        views = [v for v in views if v.course.code == course]
    return group_by_day(views, to_local(now).date())


@dataclass(frozen=True)
class RuleRow:
    rule: str
    label: str
    muted: bool
    note: str | None  # why the rule cannot fire with the current setup (Health only, D055)


def rule_note(rule: Rule, caps: Capabilities, *, windows_on: bool) -> str | None:
    if rule is Rule.ANNOUNCED and not (caps.has(Feature.STAFF_POSTS) and caps.has(Feature.AI)):
        return "needs staff posts (ELMS API or Piazza) and AI"
    if rule is Rule.WINDOW_PLAN and not windows_on:
        return "needs work windows: set them in Settings"
    return None


@dataclass(frozen=True)
class NotificationLine:
    when: str
    title: str
    body: str
    channel: str | None
    status: str


def _when(dt: datetime) -> str:
    return f"{to_local(dt):%a} {clock_time(dt)}"


@dataclass(frozen=True)
class HealthData:
    sources: list[SourceStatusView]
    texts_today: int
    text_cap: int
    channel: str | None
    ai_spend: str
    last_sync: str | None
    rules: list[RuleRow]
    notifications: list[NotificationLine]
    vault: VaultStatus | None = None


@dataclass(frozen=True)
class VaultStatus:
    state: str  # ok | failing | never (status-dot states)
    state_label: str
    last_sync: str | None
    error: str | None
    proposals: int


def vault_status(conn: sqlite3.Connection, now: datetime) -> VaultStatus:
    h = vault_health(conn)
    if h.last_status is None:
        state, label = "never", "Not synced yet"
    elif h.failures:
        state, label = "failing", f"Failing ({h.failures} in a row)"
    elif h.last_status == "conflict":
        state, label = "ok", "Kept your version after a conflict"
    else:
        state, label = "ok", "OK"
    return VaultStatus(
        state,
        label,
        relative(h.last_ok, now) if h.last_ok else None,
        h.last_error if h.failures else None,
        h.open_proposals,
    )


def load_health(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    channels: list[str],
    now: datetime,
) -> HealthData:
    today = to_local(now).date()
    sent = repo.notifications_sent_between(
        conn, local_datetime(today, 0), local_datetime(today + timedelta(days=1), 0)
    )
    statuses = source_statuses(conn, caps.sources, now)
    health = repo.source_health(conn, SOURCES)
    successes = [h.last_success for h in health.values() if h.last_success]
    muted = repo.muted_rules(conn, now)
    windows_on = bool(repo.work_windows(conn))
    return HealthData(
        sources=statuses,
        texts_today=sum(1 for n in sent if n.channel == "sms"),
        text_cap=config.notifications.daily_text_cap,
        channel=channels[0] if channels else None,
        ai_spend=ai_spend(conn, config, now),
        last_sync=relative(max(successes), now) if successes else None,
        rules=[
            RuleRow(r.value, r.label, r.value in muted, rule_note(r, caps, windows_on=windows_on))
            for r in Rule
            if r is not Rule.START_NUDGE  # retired (D061)
        ],
        vault=vault_status(conn, now),
        notifications=[
            NotificationLine(
                when=_when(n.sent_at or n.created_at),
                title=n.title,
                body=n.body,
                channel=n.channel,
                status=n.status,
            )
            for n in repo.recent_notifications(conn, 15)
        ],
    )


@dataclass(frozen=True)
class NextUp:
    label: str  # "Now" or "Next"
    title: str
    detail: str


@dataclass(frozen=True)
class KioskData:
    time_text: str
    date_text: str
    timeline: TimelineView
    deadlines: list[ItemView]
    notices: list[StaleNotice]
    next_up: NextUp | None = None
    offline: str | None = None


def load_kiosk(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> KioskData:
    today = to_local(now).date()
    views = _views(
        conn,
        config,
        now,
        caps=caps,
        due_after=now,
        due_before=now + timedelta(days=7),
        open_only=True,
        include_undated=False,
    )
    busy = busy_on(today, repo.list_meetings(conn), config.schedule.commitments)
    local = to_local(now)
    upcoming = [b for b in busy if b.end > now]
    next_up = None
    if upcoming:
        b = upcoming[0]
        when = f"until {clock_time(b.end)}" if b.start <= now else f"at {clock_time(b.start)}"
        where = f", {b.where}" if b.where else ""
        next_up = NextUp("Now" if b.start <= now else "Next", b.title, f"{when}{where}")
    return KioskData(
        next_up=next_up,
        time_text=f"{local.hour % 12 or 12}:{local.minute:02d}",
        date_text=long_date(today),
        timeline=timeline(
            today,
            busy,
            now,
            config=config,
            courses=course_views(config),
            due_items=[v for v in views if v.due_at and to_local(v.due_at).date() == today],
        ),
        deadlines=views[:6],
        notices=notices(conn, caps.sources, now),
    )


WIDGET_DAYS = 7
WIDGET_ITEMS = 8


@dataclass(frozen=True)
class WidgetData:
    summary: str
    notes: list[str]
    items: list[ItemView]


def load_widget(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> WidgetData:
    """The desktop widget's feed (D071): Today's summary line and the next open work due this week."""
    plan = compute(conn, config, now, tracking_sources=caps.tracking_sources)
    views = _views(conn, config, now, caps=caps, plan=plan)
    week = now + timedelta(days=WIDGET_DAYS)
    items = sorted(
        (v for v in views if v.due_at and now <= v.due_at < week and v.actionable),
        key=lambda v: (v.due_at, v.title),
    )
    return WidgetData(
        summary=_widget_summary(views, plan, now),
        notes=[n.text for n in notices(conn, caps.sources, now)],
        items=items[:WIDGET_ITEMS],
    )


def palette_items(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> list[dict[str, str]]:
    views = _views(
        conn,
        config,
        now,
        caps=caps,
        due_after=now - timedelta(days=1),
        due_before=now + timedelta(days=30),
        open_only=True,
    )
    return [
        {
            "id": f"item-{v.id}",
            "label": v.title,
            "hint": f"{v.course.code}, {v.due_text or 'no due date'}",
            "href": f"/items/{v.id}",
        }
        for v in views
    ] + [
        {
            "id": f"course-{c.code}",
            "label": f"Open {c.code}",
            "hint": "course",
            "href": f"/courses/{c.code}",
        }
        for c in course_views(config).values()
    ]


# ---------------------------------------------------------------- course & search


@dataclass(frozen=True)
class MeetingLine:
    kind: str
    days: str
    time: str
    where: str | None


@dataclass(frozen=True)
class ModuleItemView:
    title: str
    type: str
    icon: str
    url: str | None
    indent: int
    is_header: bool


@dataclass(frozen=True)
class ModuleView:
    id: str
    name: str
    state: str | None
    items: list[ModuleItemView]


@dataclass(frozen=True)
class CourseLink:
    label: str
    url: str


@dataclass(frozen=True)
class CourseData:
    course: CourseView
    title: str | None
    section: str
    instructors: tuple[str, ...]
    meetings: list[MeetingLine]
    show_modules: bool  # the modules sync is set up for this course (D025, D055)
    show_grade: bool
    show_posts: bool  # some staff-post source is set up for this course
    post_sources: str  # "ELMS and Piazza", for the empty state
    grade: str | None
    grade_updated: str | None
    modules: list[ModuleView]
    upcoming: list[ItemView]
    recent: list[ItemView]
    posts: list[PostView]
    links: list[CourseLink]


@dataclass(frozen=True)
class PostView:
    title: str
    body: str
    kind_label: str
    when: str
    url: str | None
    staff: bool


_MODULE_ICONS = {
    "File": "book-open",
    "Page": "notebook-pen",
    "Assignment": "list-checks",
    "Quiz": "circle-help",
    "Discussion": "megaphone",
    "ExternalUrl": "external-link",
    "ExternalTool": "external-link",
}
_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_POST_KINDS = {
    "announcement": "Canvas announcement",
    "syllabus": "Syllabus",
    "note": "Piazza note",
    "question": "Piazza question",
    "instructor_answer": "Instructor answer on Piazza",
}


def load_course(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    now: datetime,
    code: str,
) -> CourseData | None:
    row = repo.get_course(conn, code)
    cfg = config.course(code)
    if row is None or cfg is None:
        return None
    courses = course_views(config)
    meetings = [
        MeetingLine(
            kind=m.kind.capitalize(),
            days=" ".join(_DAY_ABBR[d] for d in m.days),
            time=f"{_wall(m.start.hour, m.start.minute)} to {_wall(m.end.hour, m.end.minute)}",
            where=" ".join(x for x in (m.building, m.room) if x) or None,
        )
        for m in repo.list_meetings(conn)
        if m.course_code == code
    ]
    course_caps = caps.course(code)
    modules = []
    if course_caps.modules:
        for sm in repo.list_modules(conn, code):
            modules.append(
                ModuleView(
                    id=f"module-{_module_row_id(conn, code, sm.record.canvas_id)}",
                    name=sm.record.name,
                    state=sm.record.state,
                    items=[
                        ModuleItemView(
                            title=i.title,
                            type=i.type,
                            icon=_MODULE_ICONS.get(i.type, "link"),
                            url=i.url,
                            indent=min(i.indent, 3),
                            is_header=i.type == "SubHeader",
                        )
                        for i in sm.record.items
                    ],
                )
            )
    views = [
        v
        for v in _views(
            conn,
            config,
            now,
            caps=caps,
            due_after=now - timedelta(days=14),
            due_before=now + timedelta(days=60),
        )
        if v.course.code == code
    ]
    upcoming = [v for v in views if v.due_at is None or v.due_at >= now]
    recent = sorted(
        (v for v in views if v.due_at is not None and v.due_at < now),
        key=lambda v: v.due_at or now,
        reverse=True,
    )
    posts = [
        PostView(
            title=p.title,
            body=p.body[:400] + ("..." if len(p.body) > 400 else ""),
            kind_label=_POST_KINDS.get(p.kind, p.kind),
            when=relative(p.posted_at, now),
            url=p.url,
            staff=p.author_role.is_staff,
        )
        for p in repo.list_posts(conn, code)
        if p.author_role.is_staff
        and p.kind != "syllabus"
        and caps.shows_post(platform=p.platform, kind=p.kind, course=code)
    ][:8]
    base = config.canvas.base_url.rstrip("/")
    links = []
    if cfg.canvas_id:
        links.append(CourseLink("ELMS", f"{base}/courses/{cfg.canvas_id}"))
    if cfg.gradescope_id:
        links.append(
            CourseLink("Gradescope", f"https://www.gradescope.com/courses/{cfg.gradescope_id}")
        )
    if cfg.piazza_network_id:
        links.append(CourseLink("Piazza", f"https://piazza.com/class/{cfg.piazza_network_id}"))
    grade = None
    if course_caps.modules and row.grade_score is not None:
        grade = f"{row.grade_score:g}%" + (f" ({row.grade_letter})" if row.grade_letter else "")
    return CourseData(
        course=courses[code],
        title=row.title,
        section=row.section,
        instructors=row.instructors,
        meetings=meetings,
        show_modules=course_caps.modules,
        show_grade=course_caps.modules,
        show_posts=course_caps.canvas_api or course_caps.piazza,
        post_sources=caps.post_sources(),
        grade=grade,
        grade_updated=relative(row.grade_updated_at, now) if row.grade_updated_at else None,
        modules=modules,
        upcoming=upcoming,
        recent=recent[:6],
        posts=posts,
        links=links,
    )


def _wall(hour: int, minute: int) -> str:
    h12 = hour % 12 or 12
    suffix = "am" if hour < 12 else "pm"
    return f"{h12}:{minute:02d} {suffix}" if minute else f"{h12} {suffix}"


def _module_row_id(conn: sqlite3.Connection, code: str, canvas_id: int) -> int:
    r = conn.execute(
        "SELECT m.id FROM modules m JOIN courses c ON c.id = m.course_id"
        " WHERE c.code = ? AND m.canvas_id = ?",
        (code, canvas_id),
    ).fetchone()
    return int(r[0]) if r else canvas_id


@dataclass(frozen=True)
class SearchGroup:
    label: str
    hits: list[tuple[repo.SearchHit, CourseView]]


SEARCH_GROUPS = (
    ("item", "Assignments and exams"),
    ("module", "Modules"),
    ("post", "Announcements and posts"),
)


def load_search(
    conn: sqlite3.Connection,
    config: AppConfig,
    caps: Capabilities,
    text: str,
    course: str | None,
) -> list[SearchGroup]:
    courses = course_views(config)
    hits = [h for h in repo.search(conn, text, course=course) if _searchable(conn, caps, h)]
    groups = []
    for kind, label in SEARCH_GROUPS:
        matched = [
            (h, courses.get(h.course) or CourseView(h.course, "slate"))
            for h in hits
            if h.kind == kind
        ]
        if matched:
            groups.append(SearchGroup(label, matched))
    return groups


def _searchable(conn: sqlite3.Connection, caps: Capabilities, hit: repo.SearchHit) -> bool:
    """Hits from a source that is no longer set up are not shown (D055)."""
    if hit.kind == "module":
        return caps.course(hit.course).modules
    if hit.kind == "post":
        row = conn.execute(
            "SELECT platform, kind FROM posts WHERE id = ?", (int(hit.ref_id),)
        ).fetchone()
        return row is not None and caps.shows_post(
            platform=row["platform"], kind=row["kind"], course=hit.course
        )
    return True


def search_scope(caps: Capabilities) -> tuple[str, str]:
    """(placeholder, empty-state text) naming only what search can find with this setup."""
    things = ["assignments"]
    covers = ["Assignment instructions"]
    if caps.has(Feature.MODULES):
        things.append("modules")
        covers.append("module titles")
    if caps.has(Feature.CANVAS_ANNOUNCEMENTS):
        covers.append("ELMS announcements")
    if caps.has(Feature.PIAZZA):
        covers.append("Piazza posts")
    if caps.has(Feature.STAFF_POSTS):
        things.append("posts")
    placeholder = "Search " + (
        ", ".join(things[:-1]) + ", and " + things[-1] if len(things) > 2 else " and ".join(things)
    )
    text = (
        (", ".join(covers[:-1]) + ", and " + covers[-1])
        if len(covers) > 2
        else " and ".join(covers)
    )
    return placeholder, text + "."


# ---------------------------------------------------------------- estimate detail


@dataclass(frozen=True)
class LogLine:
    when: str
    hours: str
    source: str
    running: bool


@dataclass(frozen=True)
class PastLine:
    title: str
    actual: str
    estimate: str | None
    ratio: str | None


@dataclass(frozen=True)
class ItemDetail:
    view: ItemView
    item: Item
    estimate: Effective | None
    estimate_source: str | None
    estimate_state: str  # estimates.estimate_state: user | ai | ask | no_description | ...
    miss_reason: str | None  # what the AI said is missing from the description
    latest: repo.EstimateRow | None
    history: list[repo.EstimateRow]
    factor_text: str | None
    logged: str
    logs: list[LogLine]
    past: list[PastLine]
    remaining: str | None  # E
    capacity: str | None  # C: work-window time before it's due
    demand: str | None  # D: work due by then, this item included
    findings: list[repo.FindingRow]
    scope: str | None
    logistics: str | None
    obsidian: str | None = None  # obsidian:// link to the item's vault note, when it exists
    exam_prep: bool = False


def load_item(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime, item_id: int
) -> ItemDetail | None:
    item = repo.get_item(conn, item_id)
    if item is None:
        return None
    plan = compute(conn, config, now, tracking_sources=caps.tracking_sources)
    views = {v.id: v for v in _views(conn, config, now, caps=caps, plan=plan)}
    view = views.get(item.id) or item_view(
        item, now, course_views(config), tracking_sources=caps.tracking_sources
    )
    eff = effective_estimates(conn, now).get(item.id)
    history = repo.estimates_for(conn, item.id)
    base = next((e for e in reversed(history) if e.source != "user"), None)
    user = repo.latest_estimates(conn).get(item.id, (None, None))[1]
    miss = repo.estimate_misses(conn).get(item.id)
    state = estimate_state(item, base, user, miss, ai=caps.has(Feature.AI))
    finished = finished_rows(conn, now)
    same = [f for f in finished if f.course_code == item.course_code and f.kind == item.kind]
    factor_text = None
    if eff is not None and not eff.overridden:
        f = factor(
            [Finished(r.course_code, r.kind, r.p50, r.actual) for r in finished],
            item.course_code,
            item.kind,
        )
        if f.n:
            factor_text = (
                f"Your past {item.kind.value}s in {item.course_code} took {f.r_bar:.2f}x their estimates "
                f"({f.n} finished). Argus applies {f.m:.2f}x and moves closer to your average as history grows."
            )
        else:
            factor_text = "No finished work with logged time yet, so the estimate is used as is."
    logs = []
    total = 0.0
    for log in repo.time_logs_for(conn, item.id):
        h = log.hours(now)
        total += h
        logs.append(
            LogLine(
                f"{to_local(log.started_at):%a %b} {to_local(log.started_at).day}, {clock_time(log.started_at)}",
                hours(h),
                log.source,
                log.ended_at is None,
            )
        )
    risk = risk_views(plan).get(item.id)
    row = conn.execute("SELECT scope, logistics FROM items WHERE id = ?", (item.id,)).fetchone()
    return ItemDetail(
        obsidian=obsidian_link(config, item_note_path(item)),
        exam_prep=item.kind is ItemKind.EXAM,
        view=view,
        item=item,
        estimate=eff,
        estimate_source=estimate_sources(conn).get(item.id),
        estimate_state=state,
        miss_reason=miss.reason if state == "incomplete" and miss else None,
        latest=base if counts(base) else None,
        history=list(reversed(history)),
        factor_text=factor_text,
        logged=hours(total) if total else "None yet",
        logs=list(reversed(logs)),
        past=[
            PastLine(
                f.title,
                hours(f.actual),
                hours(f.p50) if f.p50 else None,
                f"{f.actual / f.p50:.1f}x" if f.p50 else None,
            )
            for f in same
        ][-8:],
        remaining=hours(risk.remaining) if risk and risk.remaining is not None else None,
        capacity=hours(risk.capacity) if risk and risk.capacity is not None else None,
        demand=hours(risk.demand) if risk and risk.demand is not None else None,
        findings=[f for f in repo.list_findings(conn) if f.item_id == item.id],
        scope=row["scope"] if row else None,
        logistics=row["logistics"] if row else None,
    )


# ---------------------------------------------------------------- workload


@dataclass(frozen=True)
class WorkloadData:
    windows: list[str]  # "Every day, 7 pm to 11 pm"
    chart: WorkloadChart
    summary: str  # "Next 7 days: 14 h of work, 16 h of window time."
    explanation: str | None
    at_risk: list[ItemView]


def workload_summary(days: Sequence[WorkloadDay], *, windows_on: bool) -> str:
    week = days[min(6, len(days) - 1)]
    work = f"Next 7 days: {hours(week.due_total) if week.due_total else 'no'} work due"
    if not windows_on:
        return work + "."
    return f"{work}, {hours(week.window_total) if week.window_total else 'no'} window time."


def load_workload(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> WorkloadData:
    plan = compute(conn, config, now, tracking_sources=caps.tracking_sources)
    days = workload_days(workload_curve(plan), to_local(now).date())
    risky = [
        v
        for v in _views(conn, config, now, caps=caps, plan=plan, open_only=True)
        if v.risk.slug in ("at-risk", "creeping", "overdue")
    ]
    risky.sort(key=lambda v: (-v.risk.severity, v.due_at or now))
    return WorkloadData(
        windows=[
            describe(w)
            for w in sorted(repo.work_windows(conn), key=lambda w: (min(w.days), w.start))
        ],
        chart=workload_chart(days, windows_on=plan.windows_on),
        summary=workload_summary(days, windows_on=plan.windows_on),
        explanation=explanation(conn),
        at_risk=risky,
    )


# ---------------------------------------------------------------- announcements


@dataclass(frozen=True)
class CourseAnnouncements:
    course: CourseView
    waiting: list[FindingView]
    recent: list[FindingView]
    unanalyzed: list[PostView]


KIND_TITLES = {
    "new_assessment": "New assessment",
    "date_change": "Date change",
    "cancellation": "Cancellation",
    "scope": "Scope",
    "logistics": "Logistics",
    "other": "Other",
}


def finding_view(
    conn: sqlite3.Connection, f: repo.FindingRow, courses: dict[str, CourseView], now: datetime
) -> FindingView:
    stored = repo.get_post(conn, f.post_id)
    post = stored.record if stored else None
    item = repo.get_item(conn, f.item_id) if f.item_id else None
    official_name = None
    official_text = None
    if f.status == "conflict" and item and item.due_at:
        official_name = (
            "ELMS" if any(link.source.family == "canvas" for link in item.links) else "Gradescope"
        )
        official_text = due_text(item.due_at, now)
    when = None
    if f.date:
        when = (
            f"{to_local(f.date):%a, %b} {to_local(f.date).day}"
            if f.date_only
            else due_text(f.date, now)
        )
    source = "Piazza" if post and post.platform == "piazza" else "a Canvas announcement"
    role = (
        {"instructor": " by an instructor", "ta": " by a TA", "student": " by a student"}.get(
            post.author_role.value, ""
        )
        if post
        else ""
    )
    confirm_label = {
        "conflict": "Use announced date",
        "cancellation": "Mark cancelled",
    }.get("conflict" if f.status == "conflict" else f.kind, "Confirm")
    return FindingView(
        id=f.id,
        course=courses.get(f.course_code) or CourseView(f.course_code, "slate"),
        kind_label=KIND_TITLES[f.kind],
        title=f.title,
        quote=f.quote,
        source_name=f"{source}{role}",
        source_url=(post.url if post and post.url else "#"),
        posted_text=f"posted {relative(post.posted_at, now)}" if post else "",
        state=f.status
        if f.status in ("pending", "conflict", "applied")
        else "applied"
        if f.status in ("confirmed", "linked")
        else f.status,
        date_text=when,
        official_name=official_name,
        official_text=official_text,
        confirm_label=confirm_label,
    )


def load_announcements(
    conn: sqlite3.Connection, config: AppConfig, caps: Capabilities, now: datetime
) -> list[CourseAnnouncements]:
    from argus.announcements import post_visible, worth_analyzing

    courses = course_views(config)

    findings = [f for f in repo.list_findings(conn) if post_visible(conn, caps, f.post_id)]
    pending_posts = [
        p
        for p in repo.posts_to_scan(conn, limit=200)
        if worth_analyzing(p)
        and caps.shows_post(
            platform=p.record.platform, kind=p.record.kind, course=p.record.course_code
        )
    ]
    out = []
    for code, course in courses.items():
        mine = [f for f in findings if f.course_code == code]
        waiting = [
            finding_view(conn, f, courses, now) for f in mine if f.status in ("pending", "conflict")
        ]
        recent = [
            finding_view(conn, f, courses, now)
            for f in mine
            if f.status in ("applied", "confirmed", "linked")
            and now - f.created_at < timedelta(days=21)
        ][:8]
        unanalyzed = [
            PostView(
                title=p.record.title,
                body=p.record.body[:400] + ("..." if len(p.record.body) > 400 else ""),
                kind_label=_POST_KINDS.get(p.record.kind, p.record.kind),
                when=relative(p.record.posted_at, now),
                url=p.record.url,
                staff=p.record.author_role.is_staff,
            )
            for p in pending_posts
            if p.record.course_code == code
        ][:10]
        out.append(CourseAnnouncements(course, waiting, recent, unanalyzed))
    return out


def ai_spend(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> str:
    from argus.ai.budget import spent_this_month

    return f"${spent_this_month(conn, now):.2f} of ${config.ai.monthly_budget_usd:.2f}"


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class SettingsData:
    rows: list[WindowRow]  # one per window (plus blank rows the student added)
    summaries: list[str]  # "Every day, 7 pm to 11 pm"
    week_hours: float | None  # window time in the next 7 days, after classes and commitments
    lead_minutes: int
    error: str | None = None
    overlaps: list[str] = field(default_factory=list)


def load_settings(
    conn: sqlite3.Connection,
    config: AppConfig,
    now: datetime,
    *,
    rows: list[WindowRow] | None = None,
    error: str | None = None,
) -> SettingsData:
    """The work-window settings: the saved windows, or the rows as the student is editing them."""
    saved = repo.work_windows(conn)
    shown = rows if rows is not None else rows_from_windows(saved)
    s = config.schedule
    spans = window_spans(
        now,
        now + timedelta(days=7),
        saved,
        meetings=repo.list_meetings(conn),
        commitments=s.commitments,
        buffer_minutes=s.walking_buffer_minutes,
    )
    return SettingsData(
        rows=shown or [WindowRow()],
        summaries=[describe(w) for w in sorted(saved, key=lambda w: (min(w.days), w.start))],
        week_hours=hours_between(spans, now, now + timedelta(days=7)) if saved else None,
        lead_minutes=notice_minutes(conn, config),
        error=error,
        overlaps=overlapping_windows(saved),
    )
