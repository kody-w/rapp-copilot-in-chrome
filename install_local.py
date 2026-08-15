#!/usr/bin/env python3
"""Install the vendorless local bridge and register it with Copilot CLI."""

import argparse
import fcntl
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
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


@contextmanager
def install_lock(timeout=30):
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / ".install.lock"
    handle = open(path, "a+", encoding="utf-8")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise RuntimeError("another rappter-chrome install is still running")
            time.sleep(0.1)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def stage_dir(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.stage-",
            dir=destination.parent,
        )
    )


def swap_dir(stage, destination):
    backup = None
    if destination.exists():
        backup = destination.parent / (
            f".{destination.name}.backup-{os.getpid()}-{secrets.token_hex(4)}"
        )
        destination.rename(backup)
    try:
        stage.rename(destination)
    except Exception:
        if backup and backup.exists():
            backup.rename(destination)
        raise
    return destination, backup


def restore_swaps(swaps):
    for destination, backup in reversed(swaps):
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        if backup and backup.exists():
            backup.rename(destination)


def finish_swaps(swaps):
    for _, backup in swaps:
        if backup:
            shutil.rmtree(backup, ignore_errors=True)


def voice_service_loaded():
    if sys.platform != "darwin":
        return False
    plist = HOME / "Library" / "LaunchAgents" / "com.rapp.voice-assistant.plist"
    if not plist.exists():
        return False
    target = f"gui/{os.getuid()}/com.rapp.voice-assistant"
    result = subprocess.run(
        ["launchctl", "print", target],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and str(RUNTIME) in result.stdout


def stop_voice_service():
    target = f"gui/{os.getuid()}/com.rapp.voice-assistant"
    subprocess.run(["launchctl", "bootout", target], check=False)


def restart_voice_service():
    target = f"gui/{os.getuid()}/com.rapp.voice-assistant"
    plist = HOME / "Library" / "LaunchAgents" / "com.rapp.voice-assistant.plist"
    if not plist.exists():
        raise RuntimeError(
            f"Voice service was loaded but its plist is missing: {plist}"
        )
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
        check=True,
    )
    subprocess.run(["launchctl", "enable", target], check=False)
    subprocess.run(["launchctl", "kickstart", "-p", target], check=True)


def install(args):
    for name in RUNTIME_FILES:
        if not (SOURCE / name).is_file():
            raise RuntimeError(f"missing runtime source: {SOURCE / name}")
    if not (SOURCE / "extension").is_dir():
        raise RuntimeError(f"missing extension source: {SOURCE / 'extension'}")
    if not (SOURCE / "local-skill" / "SKILL.md").is_file():
        raise RuntimeError("missing local skill")

    config = load_json(MCP_CONFIG, {"mcpServers": {}})
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(
            f"refusing to overwrite {MCP_CONFIG}: mcpServers is not an object"
        )
    if not args.keep_legacy:
        servers.pop("rapp-copilot-in-chrome", None)
    servers["rappter-chrome-local"] = {
        "type": "local",
        "command": "/usr/bin/python3",
        "args": [str(RUNTIME / "rappter_chrome_mcp.py")],
        "tools": ["*"],
    }

    runtime_stage = stage_dir(RUNTIME)
    extension_stage = stage_dir(EXTENSION)
    skill_stage = stage_dir(SKILL_DIR)
    stages = [runtime_stage, extension_stage, skill_stage]
    config_temp = None
    swaps = []
    original_config = MCP_CONFIG.read_bytes() if MCP_CONFIG.exists() else None
    was_loaded = voice_service_loaded()

    try:
        for name in RUNTIME_FILES:
            shutil.copy2(SOURCE / name, runtime_stage / name)
            os.chmod(
                runtime_stage / name,
                0o700 if name.endswith(".py") else 0o600,
            )
        for item in (SOURCE / "extension").iterdir():
            if item.is_file():
                shutil.copy2(item, extension_stage / item.name)
        shutil.copy2(
            SOURCE / "local-skill" / "SKILL.md",
            skill_stage / "SKILL.md",
        )

        MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".mcp-config.",
            suffix=".json",
            dir=MCP_CONFIG.parent,
        )
        config_temp = Path(temp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        if was_loaded:
            # Stop before swapping files so a resident Python process cannot
            # keep executing the old inode after a successful upgrade.
            stop_voice_service()

        swaps.append(swap_dir(runtime_stage, RUNTIME))
        stages.remove(runtime_stage)
        swaps.append(swap_dir(extension_stage, EXTENSION))
        stages.remove(extension_stage)
        swaps.append(swap_dir(skill_stage, SKILL_DIR))
        stages.remove(skill_stage)
        os.replace(config_temp, MCP_CONFIG)
        config_temp = None

        # Import the committed runtime, not the source tree.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "rappter_installed_bridge",
            RUNTIME / "bridge.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        shared_token = module.token()

        if was_loaded:
            restart_voice_service()

        if not args.keep_legacy:
            LEGACY_LAUNCHER.unlink(missing_ok=True)
            shutil.rmtree(LEGACY_SKILL, ignore_errors=True)
        finish_swaps(swaps)
    except Exception:
        restore_swaps(swaps)
        if original_config is None:
            MCP_CONFIG.unlink(missing_ok=True)
        else:
            MCP_CONFIG.write_bytes(original_config)
            os.chmod(MCP_CONFIG, 0o600)
        if was_loaded:
            try:
                restart_voice_service()
            except Exception:
                pass
        raise
    finally:
        for stage in stages:
            shutil.rmtree(stage, ignore_errors=True)
        if config_temp:
            config_temp.unlink(missing_ok=True)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--keep-legacy",
        action="store_true",
        help="keep the Anthropic-backed MCP entry alongside the local bridge",
    )
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    with install_lock():
        return install(args)


if __name__ == "__main__":
    raise SystemExit(main())
