"""Render the Argus mark (a peacock eye) to SVG and PNG app icons. Run after changing colors."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "argus" / "web" / "static" / "icons"
BG, BRAND, NEEDLE, INK, GLINT = "#0b1220", "#56c6cc", "#e2b153", "#0b1220", "#e6ebf3"

SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="{BG}"/>
<ellipse cx="16" cy="16" rx="12.5" ry="9.5" fill="{BRAND}"/>
<ellipse cx="16" cy="16" rx="7.8" ry="6.5" fill="{NEEDLE}"/>
<ellipse cx="16" cy="16" rx="4.8" ry="4.3" fill="{INK}"/>
<circle cx="14.3" cy="14.4" r="1.4" fill="{GLINT}"/>
</svg>
"""


def png(size: int) -> Image.Image:
    s = size / 32
    img = Image.new("RGBA", (size, size), BG)
    d = ImageDraw.Draw(img)

    def ellipse(cx: float, cy: float, rx: float, ry: float, fill: str) -> None:
        d.ellipse([(cx - rx) * s, (cy - ry) * s, (cx + rx) * s, (cy + ry) * s], fill=fill)

    # Maskable safe zone: keep the mark within the central 80%.
    ellipse(16, 16, 11, 8.4, BRAND)
    ellipse(16, 16, 6.9, 5.8, NEEDLE)
    ellipse(16, 16, 4.2, 3.8, INK)
    ellipse(14.5, 14.6, 1.25, 1.25, GLINT)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "argus.svg").write_text(SVG)
    for size in (192, 512):
        png(size).save(OUT / f"argus-{size}.png", optimize=True)


if __name__ == "__main__":
    main()
