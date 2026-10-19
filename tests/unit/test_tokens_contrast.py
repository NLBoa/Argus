"""WCAG AA for every token pair used as text (4.5:1) or as a UI edge (3:1), in both themes."""

import re

import pytest

from tests.conftest import REPO_ROOT

TOKENS = (REPO_ROOT / "argus/web/static/src/tokens.css").read_text()
ACCENTS = ["indigo", "teal", "plum", "ochre", "rose", "sky", "olive", "slate"]


def _block(selector_regex: str) -> dict[str, str]:
    m = re.search(selector_regex + r"\s*\{(.*?)\n\s*\}", TOKENS, re.S)
    assert m, selector_regex
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1)))


LIGHT = _block(r"(?m)^:root")
DARK = _block(r':root\[data-theme="dark"\]')
DARK_MEDIA = _block(r':root:not\(\[data-theme="light"\]\)')


def luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [
    ("ink", "paper"),
    ("ink", "surface"),
    ("ink", "surface-2"),
    ("muted", "paper"),
    ("muted", "surface"),
    ("muted", "surface-2"),
    ("brand", "surface"),
    ("brand", "paper"),
    ("brand-ink", "brand"),
    ("ink", "brand-soft"),
    ("ok", "ok-bg"),
    ("overdue-ink", "overdue"),
    ("watch", "watch-bg"),
    ("at-risk", "at-risk-bg"),
    ("overdue", "overdue-bg"),
    ("ok", "surface"),
    ("watch", "surface"),
    ("at-risk", "surface"),
    ("overdue", "surface"),
    *[(f"c-{a}-ink", f"c-{a}-bg") for a in ACCENTS],
    *[(f"c-{a}-ink", "surface") for a in ACCENTS],
]
UI_PAIRS = [
    ("control-line", "surface"),
    ("control-line", "paper"),
    ("needle", "surface"),
    ("needle", "paper"),
    ("focus", "surface"),
    ("focus", "paper"),
    *[(f"c-{a}", "surface") for a in ACCENTS],
    *[(f"c-{a}", "paper") for a in ACCENTS],
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_pairs_meet_aa(theme: str, fg: str, bg: str) -> None:
    t = LIGHT if theme == "light" else DARK
    assert contrast(t[fg], t[bg]) >= 4.5, f"{theme}: {fg} on {bg} = {contrast(t[fg], t[bg]):.2f}"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), UI_PAIRS)
def test_ui_pairs_meet_3_to_1(theme: str, fg: str, bg: str) -> None:
    t = LIGHT if theme == "light" else DARK
    assert contrast(t[fg], t[bg]) >= 3.0, f"{theme}: {fg} on {bg} = {contrast(t[fg], t[bg]):.2f}"


def test_dark_media_query_matches_manual_dark() -> None:
    assert DARK_MEDIA == DARK


def test_every_light_token_has_a_dark_value() -> None:
    assert set(LIGHT) == set(DARK)
