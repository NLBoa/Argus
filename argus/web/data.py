"""Page data loaders: read the store, build view models. One function per view."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.clock import Clock, local_datetime, to_local
from argus.collectors.base import CollectContext
from argus.collectors.http import Fetcher
from argus.collectors.runner import default_collectors
from argus.config import AppConfig, Secrets
from argus.domain.models import ItemStatus
from argus.planner.freetime import busy_on
from argus.reminders.engine import SOURCES
from argus.reminders.rules import Rule
from argus.store import repo
from argus.web.format import clock_time, long_date, relative
from argus.web.viewmodels import (
    CourseView,
    DayGroup,
    ItemView,
    SourceStatusView,
    StaleNotice,
    TimelineView,
    TimerView,
    course_views,
    group_by_day,
    item_view,
    source_status,
    stale_notices,
    timeline,
    timer_view,
)

PAST_DUE_WINDOW = timedelta(days=3)
ACTIVE_RULES = {Rule.DUE_24H, Rule.DUE_3H, Rule.NEW_ITEM, Rule.SYSTEM_ALERT}


def configured_sources(
    config: AppConfig, secrets: Secrets, fetcher: Fetcher, clock: Clock
) -> dict[str, bool]:
    ctx = CollectContext(config=config, secrets=secrets, clock=clock, fetcher=fetcher)
    return {c.source.value: c.enabled(ctx) for c in default_collectors()}


def source_statuses(
    conn: sqlite3.Connection, configured: dict[str, bool], now: datetime
) -> list[SourceStatusView]:
    health = repo.source_health(conn, SOURCES)
    return [source_status(s, health.get(s), configured.get(s, False), now) for s in SOURCES]


def notices(
    conn: sqlite3.Connection, configured: dict[str, bool], now: datetime
) -> list[StaleNotice]:
    return stale_notices(source_statuses(conn, configured, now))


def _views(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, **filters: object
) -> list[ItemView]:
    courses = course_views(config)
    timer = repo.running_timer(conn)
    running = timer.item_id if timer else None
    return [
        item_view(i, now, courses, running_item_id=running)
        for i in repo.list_items(conn, **filters)  # type: ignore[arg-type]
    ]


@dataclass(frozen=True)
class TodayGroup(DayGroup):
    empty_icon: str = "inbox"
    empty_title: str = ""
    empty_text: str = ""


@dataclass(frozen=True)
class TodayData:
    date_text: str
    summary: str
    timeline: TimelineView
    groups: list[TodayGroup]
    timer: TimerView | None


def load_today(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> TodayData:
    today = to_local(now).date()
    start = local_datetime(today, 0)
    tomorrow = local_datetime(today + timedelta(days=1), 0)
    after = local_datetime(today + timedelta(days=2), 0)
    views = _views(
        conn,
        config,
        now,
        due_after=now - timedelta(days=14),
        due_before=after,
        include_undated=False,
    )
    recent = now - PAST_DUE_WINDOW
    overdue = [
        v
        for v in views
        if v.due_at
        and recent <= v.due_at < now
        and (v.status.is_open or v.status == ItemStatus.MISSED)
        and v.kind.takes_submission
    ]
    overdue_ids = {v.id for v in overdue}
    due_today = [
        v for v in views if v.due_at and start <= v.due_at < tomorrow and v.id not in overdue_ids
    ]
    due_tomorrow = [v for v in views if v.due_at and tomorrow <= v.due_at < after]
    groups: list[TodayGroup] = []
    if overdue:
        groups.append(TodayGroup(None, "Past due", tuple(overdue)))
    groups.append(
        TodayGroup(
            today,
            "Due today",
            tuple(due_today),
            "circle-check",
            "Nothing due today",
            "Get ahead on something from Upcoming, or take the win.",
        )
    )
    groups.append(
        TodayGroup(
            today + timedelta(days=1),
            "Due tomorrow",
            tuple(due_tomorrow),
            "calendar-days",
            "Nothing due tomorrow",
            "Tomorrow is clear so far.",
        )
    )
    open_today = sum(1 for v in due_today if v.status.is_open)
    parts = [f"{open_today} due today" if open_today else "Nothing left due today"]
    if due_tomorrow:
        parts.append(f"{len(due_tomorrow)} tomorrow")
    if overdue:
        parts.append(f"{len(overdue)} past due")
    busy = busy_on(today, repo.list_meetings(conn), config.schedule.commitments)
    by_id = {v.id: v for v in views}
    return TodayData(
        date_text=long_date(today),
        summary=", ".join(parts),
        timeline=timeline(
            today, busy, now, config=config, courses=course_views(config), due_items=due_today
        ),
        groups=groups,
        timer=timer_view(
            repo.running_timer(conn), by_id | _running_extra(conn, config, now, by_id), now
        ),
    )


def _running_extra(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, known: dict[int, ItemView]
) -> dict[int, ItemView]:
    """The running item may be due later than tomorrow; fetch it so the timer bar can show it."""
    timer = repo.running_timer(conn)
    if timer is None or timer.item_id in known:
        return {}
    item = repo.get_item(conn, timer.item_id)
    if item is None:
        return {}
    return {item.id: item_view(item, now, course_views(config), running_item_id=item.id)}


def load_upcoming(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, days: int, course: str | None
) -> list[DayGroup]:
    views = _views(conn, config, now, due_after=now, due_before=now + timedelta(days=days))
    if course:
        views = [v for v in views if v.course.code == course]
    return group_by_day(views, to_local(now).date())


@dataclass(frozen=True)
class RuleRow:
    rule: str
    label: str
    muted: bool
    active: bool


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


def load_health(
    conn: sqlite3.Connection,
    config: AppConfig,
    configured: dict[str, bool],
    channels: list[str],
    now: datetime,
) -> HealthData:
    today = to_local(now).date()
    sent = repo.notifications_sent_between(
        conn, local_datetime(today, 0), local_datetime(today + timedelta(days=1), 0)
    )
    statuses = source_statuses(conn, configured, now)
    health = repo.source_health(conn, SOURCES)
    successes = [h.last_success for h in health.values() if h.last_success]
    muted = repo.muted_rules(conn, now)
    return HealthData(
        sources=statuses,
        texts_today=sum(1 for n in sent if n.channel == "sms"),
        text_cap=config.notifications.daily_text_cap,
        channel=channels[0] if channels else None,
        ai_spend="Not set up",
        last_sync=relative(max(successes), now) if successes else None,
        rules=[RuleRow(r.value, r.label, r.value in muted, r in ACTIVE_RULES) for r in Rule],
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
    conn: sqlite3.Connection, config: AppConfig, configured: dict[str, bool], now: datetime
) -> KioskData:
    today = to_local(now).date()
    views = _views(
        conn,
        config,
        now,
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
        timeline=timeline(today, busy, now, config=config, courses=course_views(config)),
        deadlines=views[:6],
        notices=notices(conn, configured, now),
    )


def palette_items(
    conn: sqlite3.Connection, config: AppConfig, now: datetime
) -> list[dict[str, str]]:
    views = _views(
        conn,
        config,
        now,
        due_after=now - timedelta(days=1),
        due_before=now + timedelta(days=30),
        open_only=True,
    )
    return [
        {
            "id": f"item-{v.id}",
            "label": v.title,
            "hint": f"{v.course.code}, {v.due_text or 'no due date'}",
            "href": f"/upcoming#item-{v.id}",
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
    digest: str | None


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
    tier2: bool  # Canvas token configured: grade and modules exist as concepts
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
    configured: dict[str, bool],
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
    tier2 = configured.get("canvas_modules", False)
    modules = []
    if tier2:
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
                    digest=sm.digest,
                )
            )
    views = [
        v
        for v in _views(
            conn,
            config,
            now,
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
        if p.author_role.is_staff and p.kind != "syllabus"
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
    if row.grade_score is not None:
        grade = f"{row.grade_score:g}%" + (f" ({row.grade_letter})" if row.grade_letter else "")
    return CourseData(
        course=courses[code],
        title=row.title,
        section=row.section,
        instructors=row.instructors,
        meetings=meetings,
        tier2=tier2,
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
    conn: sqlite3.Connection, config: AppConfig, text: str, course: str | None
) -> list[SearchGroup]:
    courses = course_views(config)
    hits = repo.search(conn, text, course=course)
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
