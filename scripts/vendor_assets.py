"""Vendor frontend assets so nothing loads from a CDN at runtime (the Pi may be offline).

Downloads pinned npm tarballs, verifies each against the registry's sha512 integrity,
extracts the needed files into argus/web/static/, builds the Lucide icon sprite, pins the
display face's axes (fonttools; D066), and writes argus/web/static/vendor/MANIFEST.json
(versions + sha256 of every output).

    uv run python scripts/vendor_assets.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "argus" / "web" / "static"

PACKAGES = {
    "htmx.org": "2.0.11",
    "alpinejs": "3.17.4",
    "lucide-static": "1.49.0",
    "@fontsource-variable/atkinson-hyperlegible-next": "5.3.0",
    "@fontsource-variable/bricolage-grotesque": "5.3.0",
}
AHN = "atkinson-hyperlegible-next"
FILES = {
    "htmx.org": {"package/dist/htmx.min.js": "vendor/htmx.min.js"},
    "alpinejs": {"package/dist/cdn.min.js": "vendor/alpine.min.js"},
    "@fontsource-variable/atkinson-hyperlegible-next": {
        f"package/files/{AHN}-latin-wght-normal.woff2": "fonts/ahn-latin.woff2",
        f"package/files/{AHN}-latin-ext-wght-normal.woff2": "fonts/ahn-latin-ext.woff2",
        "package/LICENSE": "fonts/LICENSE-atkinson-hyperlegible-next.txt",
    },
    "lucide-static": {"package/LICENSE": "icons/LICENSE-lucide.txt"},
    "@fontsource-variable/bricolage-grotesque": {
        "package/LICENSE": "fonts/LICENSE-bricolage-grotesque.txt"
    },
}
# The display face: the latin file with every axis but weight pinned, and weight cut to the range
# titles use, so it stays under 45 KB (D066). package -> (source file, axes, output).
DISPLAY = {
    "@fontsource-variable/bricolage-grotesque": (
        "package/files/bricolage-grotesque-latin-standard-normal.woff2",
        {"opsz": 48, "wdth": 100, "wght": (500, 800)},
        "fonts/display-bricolage.woff2",
    ),
}
ICONS = [
    "activity",
    "alarm-clock",
    "arrow-down",
    "arrow-up",
    "bell",
    "bell-off",
    "book-open",
    "calendar-clock",
    "calendar-days",
    "chart-column",
    "check",
    "chevron-down",
    "chevron-right",
    "circle-alert",
    "circle-check",
    "circle-dot",
    "circle-help",
    "clock",
    "command",
    "ellipsis",
    "external-link",
    "eye",
    "file-diff",
    "gauge",
    "git-compare",
    "graduation-cap",
    "hourglass",
    "house",
    "inbox",
    "info",
    "keyboard",
    "layers",
    "link",
    "list-checks",
    "loader-circle",
    "map-pin",
    "megaphone",
    "monitor",
    "moon",
    "notebook-pen",
    "octagon-alert",
    "pause",
    "pin",
    "pin-off",
    "play",
    "refresh-cw",
    "search",
    "square",
    "sun",
    "timer",
    "triangle-alert",
    "wifi-off",
    "x",
]


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:
        return bytes(r.read())


def tarball(name: str, version: str) -> tarfile.TarFile:
    meta = json.loads(fetch(f"https://registry.npmjs.org/{name}/{version}"))
    data = fetch(meta["dist"]["tarball"])
    algo, _, expected = meta["dist"]["integrity"].partition("-")
    if algo != "sha512" or base64.b64encode(hashlib.sha512(data).digest()).decode() != expected:
        sys.exit(f"integrity mismatch for {name}@{version}")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def sprite(tf: tarfile.TarFile) -> str:
    symbols = []
    for icon in ICONS:
        member = tf.extractfile(f"package/icons/{icon}.svg")
        if member is None:
            sys.exit(f"lucide icon not found: {icon}")
        svg = member.read().decode()
        inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S)
        if inner is None:
            sys.exit(f"unexpected svg for {icon}")
        body = re.sub(r"<!--.*?-->", "", inner.group(1), flags=re.S)
        body = " ".join(body.split())
        symbols.append(f'<symbol id="i-{icon}" viewBox="0 0 24 24">{body}</symbol>')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + "".join(symbols)
        + "</svg>\n"
    )


def display_face(
    tf: tarfile.TarFile, src: str, axes: dict[str, float | tuple[float, float]]
) -> bytes:
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    member = tf.extractfile(src)
    if member is None:
        sys.exit(f"{src} missing")
    font = TTFont(io.BytesIO(member.read()))
    instantiateVariableFont(font, axes, inplace=True)
    font.flavor = "woff2"
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def main() -> None:
    manifest: dict[str, object] = {"packages": PACKAGES, "files": {}}
    files: dict[str, str] = {}
    for name, version in PACKAGES.items():
        tf = tarball(name, version)
        for src, dest in FILES.get(name, {}).items():
            member = tf.extractfile(src)
            if member is None:
                sys.exit(f"{src} missing from {name}@{version}")
            out = STATIC / dest
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(member.read())
            files[dest] = hashlib.sha256(out.read_bytes()).hexdigest()
        if name in DISPLAY:
            src, axes, dest = DISPLAY[name]
            out = STATIC / dest
            out.write_bytes(display_face(tf, src, axes))
            files[dest] = hashlib.sha256(out.read_bytes()).hexdigest()
        if name == "lucide-static":
            out = STATIC / "icons" / "sprite.svg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(sprite(tf))
            files["icons/sprite.svg"] = hashlib.sha256(out.read_bytes()).hexdigest()
    manifest["files"] = files
    (STATIC / "vendor").mkdir(parents=True, exist_ok=True)
    (STATIC / "vendor" / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"vendored {len(files)} files")


if __name__ == "__main__":
    main()
