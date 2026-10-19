"""SMS-friendly text: GSM-7 only, one segment (160 chars) where possible."""

from __future__ import annotations

from datetime import datetime

from argus.clock import to_local

SEGMENT = 160
# GSM 03.38 basic set. The extension table (_GSM7_EXT) is allowed too, at 2 septets per
# character: the "[1]" item numbers in reminders use it.
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXT = set("^{}\\[~]|" + chr(0x20AC))  # ^ { } \ [ ~ ] | and the euro sign
_REPLACE = {
    chr(0x2018): "'",  # left single quote
    chr(0x2019): "'",  # right single quote
    chr(0x201C): '"',  # left double quote
    chr(0x201D): '"',  # right double quote
    chr(0x2013): "-",  # en dash
    chr(0x2014): "-",  # em dash
    chr(0x2026): "...",  # ellipsis
    chr(0x00A0): " ",  # no-break space
    "\t": " ",
}


def to_gsm7(text: str) -> str:
    out = []
    for raw in text:
        ch = _REPLACE.get(raw, raw)
        out.append(ch if all(c in _GSM7 or c in _GSM7_EXT for c in ch) else "?")
    return "".join(out)


def gsm_length(text: str) -> int:
    """Septets used: extension-table characters count twice."""
    return len(text) + sum(1 for c in text if c in _GSM7_EXT)


def fit(text: str, limit: int = SEGMENT) -> str:
    """GSM-7 text, cut at a word boundary with '...' if longer than `limit`."""
    text = " ".join(to_gsm7(text).split(" "))
    if gsm_length(text) <= limit:
        return text
    cut = text
    while gsm_length(cut) > limit - 3:
        cut = cut[:-1]
    if " " in cut[len(cut) // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "..."


def short(title: str, limit: int) -> str:
    return fit(title, limit)


def when(dt: datetime) -> str:
    """'Thu 11:30pm' in local time."""
    local = to_local(dt)
    hour = local.hour % 12 or 12
    minute = f":{local.minute:02d}" if local.minute else ""
    return f"{local:%a} {hour}{minute}{'am' if local.hour < 12 else 'pm'}"


def clock(dt: datetime) -> str:
    """'7pm' or '7:30pm' in local time."""
    return when(dt).split(" ", 1)[1]


def window_phrase(start: datetime, end: datetime, now: datetime) -> str:
    """'Tonight 7-11pm', 'This afternoon 1-6pm', or 'Sun 1-6pm' for a later day."""
    s, n = to_local(start), to_local(now)
    if s.date() == n.date():
        word = "Tonight" if s.hour >= 17 else "This afternoon" if s.hour >= 12 else "This morning"
    else:
        word = f"{s:%a}"
    a, b = clock(start), clock(end)
    if a[-2:] == b[-2:]:
        a = a[:-2]  # '7-11pm', not '7pm-11pm'
    return f"{word} {a}-{b}"


def hours_short(hours: float) -> str:
    """'4h', '1.5h', '45m'."""
    if hours < 1:
        return f"{max(5, round(hours * 60 / 5) * 5)}m"
    rounded = round(hours * 2) / 2
    return f"{rounded:g}h"


def due_phrase(due: datetime, now: datetime) -> str:
    """'due today', 'due tomorrow', or 'due Thu' by local calendar day."""
    days = (to_local(due).date() - to_local(now).date()).days
    if days <= 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    return f"due {to_local(due):%a}"
