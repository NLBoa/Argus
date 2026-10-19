"""SMS-friendly text: GSM-7 only, one segment (160 chars) where possible."""

from __future__ import annotations

from datetime import datetime

from argus.clock import to_local

SEGMENT = 160
# GSM 03.38 basic set (the extension table is avoided on purpose: it costs 2 chars each).
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
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
        out.append(ch if all(c in _GSM7 for c in ch) else "?")
    return "".join(out)


def fit(text: str, limit: int = SEGMENT) -> str:
    """GSM-7 text, cut at a word boundary with '...' if longer than `limit`."""
    text = " ".join(to_gsm7(text).split(" "))
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut[limit // 2 :]:
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
