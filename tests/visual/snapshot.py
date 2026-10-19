"""Screenshot comparison against per-platform baselines (Pillow; no Node needed).

Baselines live in tests/visual/baselines/<darwin|linux>/. Font rasterization differs between
macOS and Linux, so each OS has its own set; Linux baselines are recorded in the pinned
Playwright container (`make visual-linux`) so CI and the Pi agree.
Set ARGUS_UPDATE_SNAPSHOTS=1 to (re)record.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import Page

HERE = Path(__file__).parent
PLATFORM = "darwin" if sys.platform == "darwin" else "linux"
BASELINES = HERE / "baselines" / PLATFORM
DIFFS = HERE / "__diff__"
# A pixel "differs" when any channel moves more than CHANNEL_TOLERANCE; the image fails
# when more than MAX_DIFF_RATIO of pixels differ (absorbs antialiasing noise only).
CHANNEL_TOLERANCE = 24
MAX_DIFF_RATIO = 0.002


def assert_snapshot(page: Page, name: str) -> None:
    BASELINES.mkdir(parents=True, exist_ok=True)
    baseline = BASELINES / f"{name}.png"
    actual_bytes = page.screenshot(full_page=True, animations="disabled", caret="hide")
    if os.environ.get("ARGUS_UPDATE_SNAPSHOTS") == "1":
        baseline.write_bytes(actual_bytes)
        return
    if not baseline.exists():
        raise AssertionError(
            f"no baseline {baseline.relative_to(HERE.parent.parent)}; "
            "record with `make visual-update` (or `make visual-linux` for Linux)"
        )
    DIFFS.mkdir(exist_ok=True)
    actual_path = DIFFS / f"{name}.actual.png"
    actual_path.write_bytes(actual_bytes)
    expected = Image.open(baseline).convert("RGB")
    actual = Image.open(actual_path).convert("RGB")
    if expected.size != actual.size:
        raise AssertionError(f"{name}: size {actual.size} != baseline {expected.size}")
    diff = ImageChops.difference(expected, actual)
    mask = diff.convert("L").point(lambda v: 255 if v > CHANNEL_TOLERANCE else 0)
    changed = expected.size[0] * expected.size[1] - mask.histogram()[0]
    ratio = changed / (expected.size[0] * expected.size[1])
    if ratio > MAX_DIFF_RATIO:
        mask.save(DIFFS / f"{name}.diff.png")
        raise AssertionError(f"{name}: {ratio:.2%} of pixels differ (see {DIFFS})")
    actual_path.unlink()
