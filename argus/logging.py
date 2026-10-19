"""Structured logging with secret and phone-number redaction.

Every record passes through `RedactingFilter` before it is formatted. Redaction covers:
registered secret values (from `Secrets.secret_values()`), phone numbers, Canvas
calendar-feed tokens, bearer tokens, Anthropic keys, and token-like query parameters.
Logs go to stdout as one JSON object per line (journald captures them on the Pi).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from typing import Any, TextIO

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(/feeds/calendars/)[A-Za-z0-9_\-]+(\.ics)?"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/\-]+=*"), r"\1 " + REDACTED),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), REDACTED),
    (
        re.compile(r"(?i)\b(token|access_token|api_key|apikey|password|secret|auth)=[^&\s\"']+"),
        r"\1=" + REDACTED,
    ),
    # Phone numbers: E.164 (+12025550123) and US formats with separators (202-555-0123).
    (re.compile(r"\+\d{10,15}\b"), "[PHONE]"),
    (re.compile(r"(?<!\d)\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)"), "[PHONE]"),
]

_STD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)


class Redactor:
    def __init__(self) -> None:
        self._secrets: list[str] = []

    def register(self, values: Iterable[str]) -> None:
        # Longest first, so a secret that contains another is fully replaced.
        merged = {v for v in [*self._secrets, *values] if v and len(v) >= 4}
        self._secrets = sorted(merged, key=len, reverse=True)

    def clear(self) -> None:
        self._secrets = []

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text


redactor = Redactor()


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redactor(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redactor(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        for key, value in list(vars(record).items()):
            if key not in _STD_ATTRS:
                setattr(record, key, _redact_value(value))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STD_ATTRS:
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


def configure_logging(
    level: str = "INFO", secrets: Iterable[str] = (), *, stream: TextIO | None = None
) -> None:
    """JSON logs to stdout (or `stream`: commands whose stdout is data log to stderr)."""
    redactor.register(secrets)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
