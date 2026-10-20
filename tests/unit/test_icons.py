"""The app icons are rendered from the mark macro (D068)."""

from __future__ import annotations

import importlib.util
import re

from tests.conftest import REPO_ROOT


def test_icons_are_current() -> None:
    spec = importlib.util.spec_from_file_location("make_icons", REPO_ROOT / "scripts/make_icons.py")
    assert spec and spec.loader
    icons = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(icons)
    assert icons.check() == [], "run `uv run python scripts/make_icons.py`"


def test_theme_color_is_the_header_surface() -> None:
    tokens = (REPO_ROOT / "argus/web/static/src/tokens.css").read_text()
    light = re.search(r":root \{.*?--surface: (#\w+);", tokens, re.S)
    dark = re.search(r':root\[data-theme="dark"\] \{.*?--surface: (#\w+);', tokens, re.S)
    assert light and dark
    base = (REPO_ROOT / "argus/web/templates/base.html").read_text()
    pwa = (REPO_ROOT / "argus/web/routes/pwa.py").read_text()
    assert f'content="{light[1]}" media="(prefers-color-scheme: light)"' in base
    assert f'content="{dark[1]}" media="(prefers-color-scheme: dark)"' in base
    assert f"'{dark[1]}' if theme == 'dark' else '{light[1]}'" in base
    assert f'"theme_color": "{light[1]}"' in pwa
