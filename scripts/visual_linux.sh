#!/usr/bin/env bash
# Run the visual suite inside the pinned Playwright Linux image, the same image CI uses.
# Usage: scripts/visual_linux.sh           verify Linux baselines
#        scripts/visual_linux.sh --update  re-record them
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="mcr.microsoft.com/playwright/python:v1.63.0-noble"
UPDATE=0; [[ "${1:-}" == "--update" ]] && UPDATE=1
make css >/dev/null
docker run --rm --ipc=host -v "$PWD":/work -w /work \
  -e ARGUS_UPDATE_SNAPSHOTS="$UPDATE" -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_LINK_MODE=copy \
  "$IMAGE" bash -c 'pip install -q uv==0.13.1 && uv sync --frozen -q && uv run --no-sync pytest tests/visual -q'
