#!/bin/sh
# Install the vendorless local Edge/Chrome bridge.
set -eu

if ! command -v python3 >/dev/null 2>&1; then
  echo "rappter-chrome: python3 is required (3.9+)." >&2
  exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$HERE/install_local.py" ]; then
  exec python3 "$HERE/install_local.py" "$@"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL \
  https://github.com/kody-w/rapp-copilot-in-chrome/archive/refs/heads/main.tar.gz \
  -o "$TMP/repo.tar.gz"
tar -xzf "$TMP/repo.tar.gz" -C "$TMP"
python3 "$TMP/rapp-copilot-in-chrome-main/install_local.py" "$@"
