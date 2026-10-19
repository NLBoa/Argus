from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus.config import AppConfig, CourseConfig, TimeWindow, load_config, load_secrets
from tests.conftest import REPO_ROOT


def test_example_config_loads() -> None:
    cfg = load_config(REPO_ROOT / "config.example.yaml")
    assert [c.code for c in cfg.courses] == ["CMSC351", "MATH240"]
    assert cfg.schedule.quiet_hours.wraps_midnight
    assert cfg.notifications.daily_text_cap == 6
    assert cfg.ai.models.fast == "claude-haiku-5-5"
    assert cfg.course("CMSC351") is not None
    assert cfg.course("ENGL101") is None
    assert cfg.paths.db_path == Path("var/argus.db")


def test_empty_yaml_gives_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("")
    assert load_config(p) == AppConfig()


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_config(p)


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"coursez": []})


@pytest.mark.parametrize("code", ["cmsc 351", "CMSC351", "BMGT110H"])
def test_course_code_normalized(code: str) -> None:
    assert CourseConfig(code=code, section="0101").code == code.upper().replace(" ", "")


@pytest.mark.parametrize(("field", "value"), [("code", "CS351"), ("section", "101")])
def test_course_validation(field: str, value: str) -> None:
    data = {"code": "CMSC351", "section": "0101", field: value}
    with pytest.raises(ValidationError):
        CourseConfig.model_validate(data)


def test_duplicate_courses_rejected() -> None:
    c = {"code": "CMSC351", "section": "0101"}
    with pytest.raises(ValidationError, match="duplicate"):
        AppConfig.model_validate({"courses": [c, c]})


def test_time_window_contains() -> None:
    day = TimeWindow(start=time(8), end=time(23))
    night = TimeWindow(start=time(23), end=time(8))
    assert day.contains(time(8))
    assert not day.contains(time(23))
    assert night.contains(time(23, 30))
    assert night.contains(time(7, 59))
    assert not night.contains(time(8))
    assert not night.contains(time(12))


def test_secrets_from_env_only_argus_prefix() -> None:
    s = load_secrets({"ARGUS_CANVAS_TOKEN": "tok-1234", "CANVAS_TOKEN": "nope", "ARGUS_BOGUS": "x"})
    assert s.canvas_token is not None
    assert s.canvas_token.get_secret_value() == "tok-1234"
    assert "tok-1234" not in repr(s)


def test_secrets_from_credentials_directory_env_wins(tmp_path: Path) -> None:
    (tmp_path / "argus_twilio_auth_token").write_text("from-file\n")
    (tmp_path / "argus_anthropic_api_key").write_text("file-key")
    s = load_secrets({"CREDENTIALS_DIRECTORY": str(tmp_path), "ARGUS_ANTHROPIC_API_KEY": "env-key"})
    assert s.twilio_auth_token is not None
    assert s.twilio_auth_token.get_secret_value() == "from-file"
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "env-key"


def test_secret_values_lists_set_secrets_and_emails() -> None:
    s = load_secrets(
        {
            "ARGUS_PIAZZA_PASSWORD": "hunter22",
            "ARGUS_PIAZZA_EMAIL": "student@terpmail.umd.edu",
            "ARGUS_GRADESCOPE_EMAIL": "student@umd.edu",
            "ARGUS_NTFY_TOPIC": "",
        }
    )
    assert set(s.secret_values()) == {"hunter22", "student@terpmail.umd.edu", "student@umd.edu"}


def test_load_secrets_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_NTFY_TOPIC", "topic-abc")
    s = load_secrets()
    assert s.ntfy_topic is not None
