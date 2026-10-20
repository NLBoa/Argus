"""Render the Argus mark (D068) to the favicon SVG and the PNG app icons. Run after changing components/brand.html.

The mark comes from the `mark_shapes` macro, in the dark theme's colors on a dark tile. The PNGs are rendered by
Playwright's Chromium, each tagged with a hash of its source SVG. `--check` exits 1 when argus.svg or a PNG is out
of date (tests run it).
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "argus" / "web"
OUT = WEB / "static" / "icons"
TOKENS = WEB / "static" / "src" / "tokens.css"
SIZES = (192, 512)
SOURCE_KEY = "argus-source"


def dark_tokens() -> dict[str, str]:
    block = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\n\}', TOKENS.read_text(), re.S)
    assert block, "no dark block in tokens.css"
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block.group(1)))


def shapes(size: int) -> str:
    t = dark_tokens()
    module = Environment(loader=FileSystemLoader(WEB)).get_template("components/brand.html").module
    return str(module.mark_shapes(size, t["ink"], t["needle"]))  # type: ignore[attr-defined]


def favicon() -> str:
    """The browser-tab icon: a rounded tile, the mark drawn in its 16 px form."""
    paper = dark_tokens()["paper"]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">\n'
        f'<rect width="32" height="32" rx="7" fill="{paper}"/>\n'
        f'<g transform="translate(16 16) scale(0.92) translate(-16 -16)">{shapes(16)}</g>\n'
        "</svg>\n"
    )


def app_icon(size: int) -> str:
    """A full-bleed tile (maskable): the mark stays inside the central 80% circle."""
    paper = dark_tokens()["paper"]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="{size}" height="{size}">'
        f'<rect width="32" height="32" fill="{paper}"/>'
        f'<g transform="translate(16 16) scale(0.62) translate(-16 -16)">{shapes(size)}</g></svg>'
    )


def source_hash(svg: str) -> str:
    return hashlib.sha256(svg.encode()).hexdigest()


def png_is_current(path: Path, size: int) -> bool:
    if not path.exists():
        return False
    with Image.open(path) as img:
        text = getattr(img, "text", {})
        return img.size == (size, size) and text.get(SOURCE_KEY) == source_hash(app_icon(size))


def check() -> list[str]:
    stale = []
    svg = OUT / "argus.svg"
    if not svg.exists() or svg.read_text() != favicon():
        stale.append("argus.svg")
    for size in SIZES:
        path = OUT / f"argus-{size}.png"
        if not png_is_current(path, size):
            stale.append(path.name)
    return stale


def main() -> None:
    if "--check" in sys.argv:
        stale = check()
        if stale:
            sys.exit(f"out of date: {', '.join(stale)}; run `uv run python scripts/make_icons.py`")
        return
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "argus.svg").write_text(favicon())
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for size in SIZES:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(
                f"<style>html,body{{margin:0}}svg{{display:block}}</style>{app_icon(size)}"
            )
            path = OUT / f"argus-{size}.png"
            page.screenshot(path=path)
            page.close()
            info = PngImagePlugin.PngInfo()
            info.add_text(SOURCE_KEY, source_hash(app_icon(size)))
            with Image.open(path) as img:
                img.load()
                img.save(path, pnginfo=info, optimize=True)
        browser.close()


if __name__ == "__main__":
    main()
