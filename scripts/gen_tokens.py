"""Write the generated blocks of argus/web/static/src/tokens.css (D065, D066).

- dark-media: the `@media (prefers-color-scheme: dark)` copy of the `[data-theme="dark"]` block, so the
  dark values are written once.
- fallbacks: a local font per face with size-adjust and ascent/descent/line-gap overrides, matched to
  the vendored woff2's metrics, so swapping in the web font moves nothing (no layout shift).

    uv run python scripts/gen_tokens.py           # rewrite tokens.css
    uv run python scripts/gen_tokens.py --check   # exit 1 if tokens.css is out of date (CI)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "argus" / "web" / "static" / "src" / "tokens.css"
FONTS = ROOT / "argus" / "web" / "static" / "fonts"

# Arial (metric-compatible with Liberation Sans on the Pi), measured once from the macOS copy:
# units per em and the frequency-weighted advance of FREQ below.
FALLBACK_LOCAL = (
    'local("Arial"), local("Liberation Sans"), local("Helvetica Neue"), local("Helvetica")'
)
FALLBACK_UPEM = 2048
FALLBACK_AVG = 900.4

# English letter frequency (with space), for an average advance that tracks running text.
FREQ = {
    " ": 18.3, "e": 10.3, "t": 7.5, "a": 6.5, "o": 6.2, "n": 5.7, "i": 5.7, "s": 5.3, "r": 5.0,
    "h": 4.9, "l": 3.3, "d": 3.3, "u": 2.3, "c": 2.2, "m": 2.0, "f": 1.8, "w": 1.7, "g": 1.6,
    "y": 1.4, "p": 1.5, "b": 1.2, "v": 0.8, "k": 0.6, "x": 0.1, "j": 0.1, "q": 0.1, "z": 0.1,
}  # fmt: skip

# Fallback family -> the woff2 it stands in for.
FACES = {
    "Atkinson Fallback": "ahn-latin.woff2",
    "Argus Display Fallback": "display-bricolage.woff2",
}


def _metrics(path: Path) -> tuple[int, int, int, int, float]:
    from fontTools.ttLib import TTFont

    font = TTFont(path)
    hhea, cmap, hmtx = font["hhea"], font.getBestCmap(), font["hmtx"]
    avg = sum(hmtx[cmap[ord(c)]][0] * w for c, w in FREQ.items()) / sum(FREQ.values())
    return font["head"].unitsPerEm, hhea.ascent, hhea.descent, hhea.lineGap, avg


def fallbacks() -> str:
    out = []
    for family, file in FACES.items():
        upem, ascent, descent, gap, avg = _metrics(FONTS / file)
        adjust = (avg / upem) / (FALLBACK_AVG / FALLBACK_UPEM)
        out.append(
            f'@font-face {{ font-family: "{family}"; src: {FALLBACK_LOCAL}; '
            f"size-adjust: {adjust * 100:.2f}%; ascent-override: {ascent / upem / adjust * 100:.2f}%; "
            f"descent-override: {abs(descent) / upem / adjust * 100:.2f}%; "
            f"line-gap-override: {gap / upem / adjust * 100:.2f}%; }}"
        )
    return "\n".join(out) + "\n"


def dark_media(css: str) -> str:
    m = re.search(r':root\[data-theme="dark"\] \{\n(.*?)\n\}', css, re.S)
    if m is None:
        sys.exit('tokens.css: no :root[data-theme="dark"] block')
    body = "\n".join("  " + line for line in m.group(1).splitlines())
    return (
        '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {\n'
        + body
        + "\n  }\n}\n"
    )


def _fill(css: str, name: str, content: str) -> str:
    pattern = re.compile(
        rf"(/\* gen_tokens:{name}:begin[^\n]*\*/\n).*?(/\* gen_tokens:{name}:end \*/)", re.S
    )
    if not pattern.search(css):
        sys.exit(f"tokens.css: no gen_tokens:{name} markers")
    return pattern.sub(lambda m: m.group(1) + content + m.group(2), css)


def render(css: str) -> str:
    return _fill(_fill(css, "fallbacks", fallbacks()), "dark-media", dark_media(css))


def main() -> None:
    css = TOKENS.read_text()
    new = render(css)
    if "--check" in sys.argv:
        if new != css:
            sys.exit("tokens.css is out of date: run `uv run python scripts/gen_tokens.py`")
        return
    TOKENS.write_text(new)


if __name__ == "__main__":
    main()
