"""WCAG AA for every token pair used as text (4.5:1) or as a UI edge (3:1), in both themes; the status
hues stay apart (D065); the generated blocks of tokens.css are current."""

import importlib.util
import itertools
import math
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
    ("watch", "paper"),
    ("at-risk", "paper"),
    ("overdue", "paper"),
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


def test_generated_blocks_are_current() -> None:
    spec = importlib.util.spec_from_file_location("gen_tokens", REPO_ROOT / "scripts/gen_tokens.py")
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.render(TOKENS) == TOKENS, "run `uv run python scripts/gen_tokens.py`"


# ---------------------------------------------------------------- status separation (D065)
# A port of the dataviz skill's validate_palette.js checks: OKLab distance x100 between neighbouring
# status hues (creeping, at risk, overdue), under normal vision and Machado (2009) protan/deutan.
MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
}  # fmt: skip


def _linear(hex_color: str) -> tuple[float, ...]:
    def ch(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    return tuple(ch(int(hex_color[i : i + 2], 16)) for i in (1, 3, 5))


def _oklab(rgb: tuple[float, ...]) -> tuple[float, float, float]:
    r, g, b = rgb
    l_ = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m_ = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s_ = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def delta_e(a: str, b: str, kind: str | None = None) -> float:
    def lab(h: str) -> tuple[float, float, float]:
        rgb = _linear(h)
        if kind:
            rgb = tuple(
                max(0.0, min(1.0, sum(m * c for m, c in zip(row, rgb, strict=True))))
                for row in MACHADO[kind]
            )
        return _oklab(rgb)

    return 100 * math.dist(lab(a), lab(b))


STATUS = [
    "watch",
    "at-risk",
    "overdue",
]  # severity order: neighbours are the pairs that must differ


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_status_hues_stay_apart(theme: str) -> None:
    t = LIGHT if theme == "light" else DARK
    for a, b in itertools.pairwise(STATUS):
        assert delta_e(t[a], t[b]) >= 15, f"{theme}: {a} vs {b} (normal vision)"
        for kind in MACHADO:
            assert delta_e(t[a], t[b], kind) >= 8, f"{theme}: {a} vs {b} ({kind})"


def test_every_face_has_a_metric_matched_fallback() -> None:
    for family in ("Atkinson Fallback", "Argus Display Fallback"):
        assert re.search(rf'font-family: "{family}";[^}}]*size-adjust: [\d.]+%', TOKENS), family
