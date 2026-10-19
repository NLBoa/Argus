"""Class schedule from umd.io v1 (no auth).

Quirks: meeting times are strings like "9:30am". `seats` and other counts are strings.
`days` packs tokens ("MWF", "TuTh"). Lecture rows have an empty `classtype`. Online or TBA
meetings have empty times and are skipped.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Any

from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.domain.models import CourseInfo, MeetingRecord, Source, Weekday
from argus.store.repo import SourceState, sha256

API = "https://api.umd.io/v1"
_DAY_TOKENS = re.compile(r"Su|Sa|Tu|Th|M|W|F")
_DAYS = {
    "M": Weekday.MON,
    "Tu": Weekday.TUE,
    "W": Weekday.WED,
    "Th": Weekday.THU,
    "F": Weekday.FRI,
    "Sa": Weekday.SAT,
    "Su": Weekday.SUN,
}
_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\s*$", re.IGNORECASE)


def parse_time(value: str) -> time:
    """'9:30am' -> 09:30, '12:00pm' -> 12:00, '12:15am' -> 00:15."""
    m = _TIME.match(value or "")
    if not m:
        raise ValueError(f"unrecognized umd.io time {value!r}")
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if not (1 <= hour <= 12 and 0 <= minute < 60):
        raise ValueError(f"out-of-range umd.io time {value!r}")
    hour = hour % 12 + (12 if ampm == "p" else 0)
    return time(hour, minute)


def parse_days(value: str) -> tuple[Weekday, ...]:
    tokens = _DAY_TOKENS.findall(value or "")
    if "".join(tokens) != (value or "").replace(" ", ""):
        return ()
    return tuple(sorted({_DAYS[t] for t in tokens}))


def _kind(classtype: str) -> str:
    c = (classtype or "").strip().lower()
    if not c:
        return "lecture"
    if "disc" in c:
        return "discussion"
    if "lab" in c:
        return "lab"
    return c


def parse_meetings(section: dict[str, Any], course_code: str) -> list[MeetingRecord]:
    out: list[MeetingRecord] = []
    for m in section.get("meetings") or []:
        days = parse_days(str(m.get("days", "")))
        if not days or not m.get("start_time") or not m.get("end_time"):
            continue  # online asynchronous / TBA
        out.append(
            MeetingRecord(
                course_code=course_code,
                kind=_kind(str(m.get("classtype", ""))),
                days=days,
                start=parse_time(str(m["start_time"])),
                end=parse_time(str(m["end_time"])),
                building=(str(m.get("building")) or None) if m.get("building") else None,
                room=(str(m.get("room")) or None) if m.get("room") else None,
            )
        )
    return out


class UmdioCollector:
    source = Source.UMDIO

    def enabled(self, ctx: CollectContext) -> bool:
        return bool(ctx.config.courses)

    async def collect(self, ctx: CollectContext) -> CollectResult:
        params = {"semester": ctx.config.term} if ctx.config.term else None
        meetings: dict[str, tuple[MeetingRecord, ...]] = {}
        infos: list[CourseInfo] = []
        updates: dict[str, SourceState] = {}
        for course in ctx.config.courses:
            sec_resp = await ctx.fetcher.get(
                f"{API}/courses/{course.code}/sections/{course.section}", params=params
            )
            course_resp = await ctx.fetcher.get(f"{API}/courses/{course.code}", params=params)
            key = f"{course.code}-{course.section}"
            digest = sha256(sec_resp.body + b"\0" + course_resp.body)
            if digest == ctx.state(self.source, key).payload_hash:
                continue
            sections = sec_resp.json()
            if not isinstance(sections, list):
                raise ValueError(f"umd.io returned a non-list for {key}")
            section = next((s for s in sections if str(s.get("number")) == course.section), None)
            if section is None:
                raise ValueError(f"umd.io has no section {key}")
            meetings[course.code] = tuple(parse_meetings(section, course.code))
            course_data = course_resp.json()
            title = None
            if isinstance(course_data, list) and course_data:
                title = course_data[0].get("name")
            infos.append(
                CourseInfo(
                    course_code=course.code,
                    title=title,
                    instructors=tuple(str(i) for i in section.get("instructors") or []),
                )
            )
            updates[key] = SourceState(payload_hash=digest)
        status = RunStatus.OK if meetings else RunStatus.UNCHANGED
        return CollectResult(
            self.source, status, meetings=meetings, courses=tuple(infos), state_updates=updates
        )


def describe(meeting: MeetingRecord) -> str:
    """'TuTh 09:30-10:45 IRB 1101' (for the CLI and logs)."""
    names = {v: k for k, v in _DAYS.items()}
    days = "".join(names[d] for d in meeting.days)
    where = " ".join(x for x in (meeting.building, meeting.room) if x)
    return f"{days} {meeting.start:%H:%M}-{meeting.end:%H:%M} {where}".strip()
