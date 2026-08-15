#!/usr/bin/env python3
"""Install the vendorless local bridge and register it with Copilot CLI."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
HOME = Path.home()
ROOT = HOME / ".rappter-chrome"
RUNTIME = ROOT / "runtime"
EXTENSION = ROOT / "extension"
MCP_CONFIG = HOME / ".copilot" / "mcp-config.json"
SKILL_DIR = HOME / ".copilot" / "skills" / "rappter-chrome-local"
LEGACY_LAUNCHER = HOME / ".copilot" / "bin" / "rapp-copilot-in-chrome"
LEGACY_SKILL = HOME / ".copilot" / "skills" / "rapp-copilot-in-chrome"

RUNTIME_FILES = [
    "bridge.py",
    "gvoice.py",
    "rappter_chrome_mcp.py",
    "voice_assistant.py",
    "com.rapp.voice-assistant.plist.template",
]


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"refusing to overwrite unreadable JSON at {path}: {exc}"
        ) from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--keep-legacy",
        action="store_true",
        help="keep the Anthropic-backed MCP entry alongside the local bridge",
    )
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    RUNTIME.mkdir(parents=True, exist_ok=True)
    EXTENSION.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        shutil.copy2(SOURCE / name, RUNTIME / name)
        os.chmod(RUNTIME / name, 0o700 if name.endswith(".py") else 0o600)
    for item in (SOURCE / "extension").iterdir():
        if item.is_file():
            shutil.copy2(item, EXTENSION / item.name)
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / "local-skill" / "SKILL.md", SKILL_DIR / "SKILL.md")

    # Import from the installed runtime, so token creation tests the exact copy
    # Copilot will call rather than the source tree we happened to run from.
    import sys

    sys.path.insert(0, str(RUNTIME))
    from bridge import token

    shared_token = token()

    config = load_json(MCP_CONFIG, {"mcpServers": {}})
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(
            f"refusing to overwrite {MCP_CONFIG}: mcpServers is not an object"
        )
    if not args.keep_legacy:
        servers.pop("rapp-copilot-in-chrome", None)
        LEGACY_LAUNCHER.unlink(missing_ok=True)
        shutil.rmtree(LEGACY_SKILL, ignore_errors=True)
    servers["rappter-chrome-local"] = {
        "type": "local",
        "command": "/usr/bin/python3",
        "args": [str(RUNTIME / "rappter_chrome_mcp.py")],
        "tools": ["*"],
    }
    MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config_tmp = MCP_CONFIG.with_suffix(".json.tmp")
    config_tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    config_tmp.replace(MCP_CONFIG)
    os.chmod(MCP_CONFIG, 0o600)

    if not args.no_open and sys.platform == "darwin":
        browser = "Microsoft Edge" if Path("/Applications/Microsoft Edge.app").exists() else "Google Chrome"
        subprocess.run(
            ["open", "-a", browser, "edge://extensions/" if "Edge" in browser else "chrome://extensions/"],
            check=False,
        )

    print("Installed vendorless Rappter browser bridge")
    print(f"  extension: {EXTENSION}")
    print(f"  runtime:   {RUNTIME}")
    print(f"  MCP:       {MCP_CONFIG} -> rappter-chrome-local")
    print(f"  skill:     {SKILL_DIR}")
    print(f"  token:     {shared_token}")
    if not args.keep_legacy:
        print("  legacy:    Anthropic launcher/config removed")
    print()
    print("Load the extension folder as unpacked, paste the token in its popup,")
    print("then restart Copilot CLI. No Claude binary or vendor login is used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
