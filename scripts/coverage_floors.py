"""Enforce per-area coverage floors from coverage.json (line + branch combined).

Floors: 95% on the safety-critical pure code, 85% overall. Exits non-zero on any miss.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OVERALL_FLOOR = 85.0
STRICT_FLOOR = 95.0
STRICT_PREFIXES = (
    "argus/domain/",
    "argus/planner/",
    "argus/reminders/",
    "argus/vault/ownership.py",
    "argus/vault/regions.py",
)


def main(path: str = "coverage.json") -> int:
    data = json.loads(Path(path).read_text())
    failures: list[str] = []
    covered = total = 0
    for raw_name, info in data["files"].items():
        name = raw_name.replace("\\", "/")
        if not name.startswith(STRICT_PREFIXES):
            continue
        s = info["summary"]
        covered += s["covered_lines"] + s.get("covered_branches", 0)
        total += s["num_statements"] + s.get("num_branches", 0)
        if s["percent_covered"] < STRICT_FLOOR:
            failures.append(f"{name}: {s['percent_covered']:.1f}% < {STRICT_FLOOR}%")
    strict_pct = 100.0 if total == 0 else 100.0 * covered / total
    overall = data["totals"]["percent_covered"]
    print(
        f"coverage: overall {overall:.1f}% (floor {OVERALL_FLOOR}%), "
        f"strict areas {strict_pct:.1f}% (floor {STRICT_FLOOR}% per file)"
    )
    if overall < OVERALL_FLOOR:
        failures.append(f"overall: {overall:.1f}% < {OVERALL_FLOOR}%")
    for f in failures:
        print(f"  FAIL {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
