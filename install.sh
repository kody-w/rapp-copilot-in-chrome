#!/bin/sh
# rapp-copilot-in-chrome installer.
#
#   curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-copilot-in-chrome/main/install.sh | sh
#
# or, from a clone:
#
#   ./install.sh
#
# Delegates to the deterministic agent so the shell script and the documented
# capability can never disagree about what "install" means.

set -e

REPO="https://github.com/kody-w/rapp-copilot-in-chrome"
RAW="https://raw.githubusercontent.com/kody-w/rapp-copilot-in-chrome/main"

if ! command -v python3 >/dev/null 2>&1; then
  echo "rapp-copilot-in-chrome: python3 is required (3.9+)." >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT="$SCRIPT_DIR/rapp_copilot_in_chrome_agent.py"

if [ -f "$AGENT" ]; then
  WORKDIR="$SCRIPT_DIR"
else
  # Piped from curl -- fetch the capability into a temp dir first.
  WORKDIR=$(mktemp -d)
  trap 'rm -rf "$WORKDIR"' EXIT
  echo "fetching rapp-copilot-in-chrome from $REPO ..."
  for f in rapp_copilot_in_chrome_agent.py SKILL.md; do
    if ! curl -fsSL "$RAW/$f" -o "$WORKDIR/$f"; then
      echo "rapp-copilot-in-chrome: failed to download $f" >&2
      exit 1
    fi
  done
  AGENT="$WORKDIR/rapp_copilot_in_chrome_agent.py"
fi

cd "$WORKDIR"
python3 "$AGENT" '{"action": "install"}' | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("summary", ""))
sys.exit(0 if d.get("status") == "success" else 1)
'
