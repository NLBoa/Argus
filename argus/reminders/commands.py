"""SMS command parser (pure, rule-based). The LLM is only a fallback for what this refuses.

Commands (case-insensitive, extra spaces ignored):
  DONE n          mark item n done
  LOG n h         log time on item n: "2", "2h", "1.5 h", "90m", "1h30m"
  SNOOZE n [d]    no reminders about item n for d (default 1h; up to 3 days), except the 3-hour warning
  EST n h         set my estimate for item n
  OK n / Y n      yes to the question Argus asked about item n (YES / Y / OK)
  NO n / N n      no to it
  SKIP n          not tonight: item n waits until after the current or next work window
  FIRST n         pin item n to the top of the queue
  LEFT n h        about h hours of work left on item n (the same amounts as EST)
  TODAY           what's due and planned today
  ? / COMMANDS    the command list
The number is optional for DONE, OK, and NO; the service fills it in when only one item fits.
SKIP, FIRST, and LEFT always need one, so chatter like "skip it tonight" goes to the AI instead.
Opt-out and help keywords (STOP, START, HELP, ...) belong to Twilio and are never commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

MAX_LOG = timedelta(hours=24)
MAX_EST_HOURS = 200.0
MAX_SNOOZE = timedelta(days=3)
DEFAULT_SNOOZE = timedelta(hours=1)
RESERVED = frozenset(
    {
        "stop",
        "stopall",
        "unsubscribe",
        "cancel",
        "end",
        "quit",
        "start",
        "unstop",
        "yes start",
        "help",
        "info",
    }
)


class Verb(StrEnum):
    DONE = "DONE"
    LOG = "LOG"
    SNOOZE = "SNOOZE"
    EST = "EST"
    YES = "OK"
    NO = "NO"
    SKIP = "SKIP"
    FIRST = "FIRST"
    LEFT = "LEFT"
    TODAY = "TODAY"
    HELP = "HELP"


@dataclass(frozen=True, slots=True)
class Command:
    verb: Verb
    number: int | None = None
    hours: float | None = None
    duration: timedelta | None = None

    def describe(self) -> str:
        parts = [self.verb.value]
        if self.number is not None:
            parts.append(str(self.number))
        if self.hours is not None:
            parts.append(f"{self.hours:g}h")
        if self.duration is not None:
            parts.append(f"{int(self.duration.total_seconds() // 60)}m")
        return " ".join(parts)


_VERBS = {
    "done": Verb.DONE,
    "log": Verb.LOG,
    "snooze": Verb.SNOOZE,
    "est": Verb.EST,
    "estimate": Verb.EST,
    "ok": Verb.YES,
    "okay": Verb.YES,
    "y": Verb.YES,
    "yes": Verb.YES,
    "no": Verb.NO,
    "n": Verb.NO,
    "skip": Verb.SKIP,
    "first": Verb.FIRST,
    "left": Verb.LEFT,
    "today": Verb.TODAY,
    "?": Verb.HELP,
    "commands": Verb.HELP,
}
_NUMBER = re.compile(r"^#?(\d{1,4})[.,]?$")
_HOURS_ONLY = re.compile(r"^(\d+(?:\.\d+)?)$")
_DURATION = re.compile(
    r"^(?:(?P<d>\d+(?:\.\d+)?)\s*(?:d|days?))?\s*"
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:h|hrs?|hours?))?\s*"
    r"(?:(?P<m>\d+)\s*(?:m|mins?|minutes?))?$"
)


def is_reserved(text: str) -> bool:
    return " ".join(text.casefold().split()) in RESERVED


def _duration(text: str) -> timedelta | None:
    text = text.strip().casefold()
    if not text:
        return None
    if (m := _HOURS_ONLY.match(text)) is not None:
        return timedelta(hours=float(m.group(1)))
    m = _DURATION.match(text)
    if m is None or not any(m.group(k) for k in ("d", "h", "m")):
        return None
    return timedelta(
        days=float(m.group("d") or 0),
        hours=float(m.group("h") or 0),
        minutes=int(m.group("m") or 0),
    )


def _with_amount(verb: Verb, number: int, rest: list[str]) -> Command | None:
    """SNOOZE, LOG, EST, and LEFT: the amount after the number."""
    amount = _duration(" ".join(rest)) if rest else None
    if verb is Verb.SNOOZE:
        if not rest:
            return Command(verb, number, duration=DEFAULT_SNOOZE)
        ok = amount is not None and timedelta(0) < amount <= MAX_SNOOZE
        return Command(verb, number, duration=amount) if ok else None
    if amount is None or amount <= timedelta(0):
        return None
    hours = amount.total_seconds() / 3600
    too_much = amount > MAX_LOG if verb is Verb.LOG else hours > MAX_EST_HOURS
    return None if too_much else Command(verb, number, hours=hours)


def parse(text: str) -> Command | None:
    words = text.strip().rstrip("!").split()
    if not words or is_reserved(text):
        return None
    head = "?" if words[0] == "?" else words[0].casefold().rstrip("?.!")
    verb = _VERBS.get(head)
    if verb is None:
        return None
    rest = words[1:]
    if verb in (Verb.TODAY, Verb.HELP):
        return None if rest else Command(verb)
    number: int | None = None
    if rest:
        m = _NUMBER.match(rest[0])
        if m is None or int(m.group(1)) < 1:
            return None
        number, rest = int(m.group(1)), rest[1:]
    if verb in (Verb.DONE, Verb.YES, Verb.NO):
        return None if rest else Command(verb, number)
    if verb in (Verb.SKIP, Verb.FIRST):
        return None if rest or number is None else Command(verb, number)
    return None if number is None else _with_amount(verb, number, rest)
