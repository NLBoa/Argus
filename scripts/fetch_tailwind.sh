#!/usr/bin/env bash
# Download the pinned Tailwind standalone CLI into bin/ and verify its checksum.
# No Node is needed at build time or at runtime.
set -euo pipefail
VERSION="v4.3.3"
cd "$(dirname "$0")/.."
mkdir -p bin
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) ASSET="tailwindcss-macos-arm64" ;;
  Darwin-x86_64) ASSET="tailwindcss-macos-x64" ;;
  Linux-aarch64) ASSET="tailwindcss-linux-arm64" ;;
  Linux-x86_64) ASSET="tailwindcss-linux-x64" ;;
  *) echo "unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
if [[ -x bin/tailwindcss ]] && [[ "$(cat bin/.tailwind-version 2>/dev/null)" == "$VERSION-$ASSET" ]]; then
  exit 0
fi
BASE="https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION"
curl -fsSL -o "bin/$ASSET" "$BASE/$ASSET"
curl -fsSL -o bin/sha256sums.txt "$BASE/sha256sums.txt"
EXPECTED="$(grep " \*\?\(\./\)\?$ASSET\$" bin/sha256sums.txt | awk '{print $1}')"
if command -v sha256sum >/dev/null; then ACTUAL="$(sha256sum "bin/$ASSET" | awk '{print $1}')";
else ACTUAL="$(shasum -a 256 "bin/$ASSET" | awk '{print $1}')"; fi
if [[ -z "$EXPECTED" || "$EXPECTED" != "$ACTUAL" ]]; then
  echo "checksum mismatch for $ASSET (expected '$EXPECTED', got '$ACTUAL')" >&2
  rm -f "bin/$ASSET"; exit 1
fi
mv "bin/$ASSET" bin/tailwindcss
chmod +x bin/tailwindcss
echo "$VERSION-$ASSET" > bin/.tailwind-version
rm -f bin/sha256sums.txt
