"""Security: logs must never contain secrets or phone numbers."""

import io
import json
import logging

import pytest

from argus.logging import REDACTED, JsonFormatter, RedactingFilter, configure_logging, redactor


@pytest.fixture
def capture() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("test.redaction")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, buf


def lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_registered_secrets_redacted_in_message_args_and_extra(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    log, buf = capture
    redactor.register(["s3cret-token-value", "abc"])  # "abc" is too short to register
    log.info("token is %s", "s3cret-token-value", extra={"detail": {"t": "s3cret-token-value"}})
    out = buf.getvalue()
    assert "s3cret-token-value" not in out
    rec = lines(buf)[0]
    assert rec["msg"] == f"token is {REDACTED}"
    assert rec["detail"] == {"t": REDACTED}


def test_longer_secret_wins_over_contained_secret(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    log, buf = capture
    redactor.register(["pass1234", "pass1234-extended"])
    log.info("pass1234-extended")
    assert lines(buf)[0]["msg"] == REDACTED


@pytest.mark.parametrize(
    "text",
    [
        "texting +12025550123 now",
        "call (202) 555-0123",
        "call 202-555-0123",
        "call 202.555.0123",
    ],
)
def test_phone_numbers_redacted(capture: tuple[logging.Logger, io.StringIO], text: str) -> None:
    log, buf = capture
    log.warning(text)
    msg = str(lines(buf)[0]["msg"])
    assert "555" not in msg
    assert "[PHONE]" in msg


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("GET https://umd.instructure.com/feeds/calendars/user_AbC123xyz.ics", "AbC123xyz"),
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ("key sk-ant-api03-XYZ_123", "XYZ_123"),
        ("https://x.test/cb?access_token=zzz999&page=2", "zzz999"),
        ("login password=hunter2", "hunter2"),
    ],
)
def test_token_patterns_redacted(
    capture: tuple[logging.Logger, io.StringIO], text: str, leak: str
) -> None:
    log, buf = capture
    log.error(text)
    assert leak not in buf.getvalue()


def test_exception_text_redacted(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, buf = capture
    redactor.register(["very-secret-pw"])
    try:
        raise RuntimeError("login failed for very-secret-pw")
    except RuntimeError:
        log.exception("boom")
    rec = lines(buf)[0]
    assert "very-secret-pw" not in buf.getvalue()
    assert "RuntimeError" in str(rec["exc"])


def test_ordinary_numbers_survive(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, buf = capture
    log.info("synced 1234 items in 2026-10-19 run 42")
    assert lines(buf)[0]["msg"] == "synced 1234 items in 2026-10-19 run 42"


def test_configure_logging_installs_redacting_json_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", secrets=["configured-secret"])
    logging.getLogger("argus.x").info("using configured-secret")
    out = capsys.readouterr().out
    assert "configured-secret" not in out
    assert json.loads(out.splitlines()[-1])["msg"] == f"using {REDACTED}"
