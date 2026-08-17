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
JOURNAL = ROOT / ".install-journal.json"
CONFIG_BACKUP = ROOT / ".mcp-config.install-backup"

RUNTIME_FILES = [
    "bridge.py",
    "gvoice.py",
    "install-local.sh",
    "install_local.py",
    "rappter_chrome_mcp.py",
    "voice_assistant.py",
    "voice_command_center.py",
    "com.rapp.voice-assistant.plist.template",
    "rappter-voice-assistant.service.template",
]
TEST_FILES = [
    "test_bridge.py",
    "test_mcp.py",
    "test_gvoice.py",
    "test_voice_assistant.py",
    "test_voice_command_center.py",
    "test_install_local.py",
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


def fsync_directory(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root):
    root = Path(root)
    for path in root.rglob("*"):
        if path.is_file():
            try:
                with open(path, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                pass
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        fsync_directory(path)
    fsync_directory(root)


def write_bytes_atomic(path, payload, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def write_json_atomic(path, data):
    write_bytes_atomic(
        path,
        (json.dumps(data, indent=2) + "\n").encode(),
    )


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


def swap_dir(stage, destination, backup=None):
    if destination.exists():
        backup = backup or destination.parent / (
            f".{destination.name}.backup-{os.getpid()}-{secrets.token_hex(4)}"
        )
        destination.rename(backup)
        fsync_directory(destination.parent)
    try:
        stage.rename(destination)
        fsync_directory(destination.parent)
    except Exception:
        if backup and backup.exists():
            backup.rename(destination)
            fsync_directory(destination.parent)
        raise
    return destination, backup


def restore_swaps(swaps):
    for destination, backup in reversed(swaps):
        if backup:
            # A missing backup means the journal was persisted before the
            # swap began. The intact destination must not be deleted.
            if backup.exists():
                if destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
                backup.rename(destination)
        elif destination.exists():
            # No prior destination existed, so any published target belongs
            # to the interrupted generation and is safe to remove.
            shutil.rmtree(destination, ignore_errors=True)
        fsync_directory(destination.parent)


def finish_swaps(swaps):
    for _, backup in swaps:
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
            fsync_directory(backup.parent)


def cleanup_stale_stages():
    for destination in (RUNTIME, EXTENSION, SKILL_DIR):
        for stage in destination.parent.glob(f".{destination.name}.stage-*"):
            shutil.rmtree(stage, ignore_errors=True)


def recover_interrupted_install():
    cleanup_stale_stages()
    if not JOURNAL.exists():
        CONFIG_BACKUP.unlink(missing_ok=True)
        return
    journal = load_json(JOURNAL, None)
    if not isinstance(journal, dict):
        raise RuntimeError(f"invalid install journal: {JOURNAL}")

    swaps = []
    allowed = {str(path) for path in (RUNTIME, EXTENSION, SKILL_DIR)}
    for record in journal.get("swaps", []):
        destination = Path(record["destination"])
        if str(destination) not in allowed:
            raise RuntimeError(
                f"install journal contains unsafe destination: {destination}"
            )
        backup = Path(record["backup"]) if record.get("backup") else None
        swaps.append((destination, backup))

    was_loaded = bool(journal.get("service_was_loaded"))
    if journal.get("phase") != "committed":
        if was_loaded and voice_service_loaded():
            stop_voice_service()
        restore_swaps(swaps)
        if journal.get("config_existed"):
            if not CONFIG_BACKUP.exists():
                raise RuntimeError("install recovery is missing MCP config backup")
            write_bytes_atomic(MCP_CONFIG, CONFIG_BACKUP.read_bytes())
        else:
            MCP_CONFIG.unlink(missing_ok=True)
            fsync_directory(MCP_CONFIG.parent)
        if was_loaded:
            restart_voice_service()
    else:
        finish_swaps(swaps)

    CONFIG_BACKUP.unlink(missing_ok=True)
    JOURNAL.unlink(missing_ok=True)
    fsync_directory(ROOT)


def voice_service_loaded():
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return subprocess.run(
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                "rappter-voice-assistant.service",
            ],
        ).returncode == 0
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
    if sys.platform.startswith("linux"):
        subprocess.run(
            [
                "systemctl",
                "--user",
                "stop",
                "rappter-voice-assistant.service",
            ],
            check=False,
        )
        return
    target = f"gui/{os.getuid()}/com.rapp.voice-assistant"
    subprocess.run(["launchctl", "bootout", target], check=False)


def restart_voice_service():
    if sys.platform.startswith("linux"):
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True,
        )
        subprocess.run(
            [
                "systemctl",
                "--user",
                "restart",
                "rappter-voice-assistant.service",
            ],
            check=True,
        )
        return
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


def reload_extensions(bridge_module, window=6):
    """Best-effort reload of every configured Edge/Chrome profile."""
    seen = set()
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        try:
            with bridge_module.Chrome(
                wait=min(1, max(0.1, deadline - time.monotonic())),
                timeout=5,
                instance="",
            ) as chrome:
                instance_id = chrome.instance_id or "(unknown)"
                if instance_id in seen:
                    continue
                result = chrome.call("reload_extension")
                if result and result.get("reloading"):
                    seen.add(instance_id)
        except Exception:
            continue
    return sorted(seen)


def install(args):
    for name in RUNTIME_FILES + TEST_FILES:
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
        "command": str(Path(sys.executable).resolve()),
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
    journal = {
        "version": 1,
        "phase": "preparing",
        "service_was_loaded": was_loaded,
        "config_existed": original_config is not None,
        "stages": [str(path) for path in stages],
        "swaps": [],
    }

    try:
        for name in RUNTIME_FILES + TEST_FILES:
            source = SOURCE / name
            destination = runtime_stage / name
            if name.endswith((".plist.template", ".service.template")):
                destination.write_text(
                    source.read_text(encoding="utf-8")
                    .replace("__PYTHON__", str(Path(sys.executable).resolve()))
                    .replace("__HOME__", str(HOME))
                    .replace("__RUNTIME__", str(RUNTIME)),
                    encoding="utf-8",
                )
            else:
                shutil.copy2(source, destination)
            os.chmod(
                destination,
                0o700 if name.endswith((".py", ".sh")) else 0o600,
            )
        for item in (SOURCE / "extension").iterdir():
            if item.is_file():
                shutil.copy2(item, extension_stage / item.name)
                # Keep the installed runtime self-contained: install_local.py
                # and its regression test must work after a curl install, when
                # the source checkout and temporary download no longer exist.
                (runtime_stage / "extension").mkdir(exist_ok=True)
                shutil.copy2(
                    item,
                    runtime_stage / "extension" / item.name,
                )
        shutil.copy2(
            SOURCE / "local-skill" / "SKILL.md",
            skill_stage / "SKILL.md",
        )
        (runtime_stage / "local-skill").mkdir(exist_ok=True)
        shutil.copy2(
            SOURCE / "local-skill" / "SKILL.md",
            runtime_stage / "local-skill" / "SKILL.md",
        )
        for stage in stages:
            fsync_tree(stage)

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

        if original_config is not None:
            write_bytes_atomic(CONFIG_BACKUP, original_config)
        else:
            CONFIG_BACKUP.unlink(missing_ok=True)
        journal["phase"] = "installing"
        write_json_atomic(JOURNAL, journal)

        if was_loaded:
            # Stop before swapping files so a resident Python process cannot
            # keep executing the old inode after a successful upgrade.
            stop_voice_service()

        for stage, destination in (
            (runtime_stage, RUNTIME),
            (extension_stage, EXTENSION),
            (skill_stage, SKILL_DIR),
        ):
            backup = (
                destination.parent
                / f".{destination.name}.backup-{os.getpid()}-{secrets.token_hex(4)}"
                if destination.exists()
                else None
            )
            journal["swaps"].append(
                {
                    "destination": str(destination),
                    "backup": str(backup) if backup else None,
                }
            )
            write_json_atomic(JOURNAL, journal)
            swaps.append(swap_dir(stage, destination, backup))
            stages.remove(stage)
        os.replace(config_temp, MCP_CONFIG)
        fsync_directory(MCP_CONFIG.parent)
        config_temp = None
        journal["phase"] = "published"
        write_json_atomic(JOURNAL, journal)

        # Import the committed runtime, not the source tree.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "rappter_installed_bridge",
            RUNTIME / "bridge.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        shared_token = module.token()
        reloaded_extensions = reload_extensions(module)

        if was_loaded:
            restart_voice_service()

        if not args.keep_legacy:
            LEGACY_LAUNCHER.unlink(missing_ok=True)
            shutil.rmtree(LEGACY_SKILL, ignore_errors=True)
        journal["phase"] = "committed"
        write_json_atomic(JOURNAL, journal)
        finish_swaps(swaps)
        CONFIG_BACKUP.unlink(missing_ok=True)
        JOURNAL.unlink(missing_ok=True)
        fsync_directory(ROOT)
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
        CONFIG_BACKUP.unlink(missing_ok=True)
        JOURNAL.unlink(missing_ok=True)
        fsync_directory(ROOT)
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
        extensions_url = (
            "edge://extensions/" if "Edge" in browser else "chrome://extensions/"
        )
    else:
        extensions_url = "chrome://extensions/ (or edge://extensions/)"

    print("Installed vendorless Rappter browser bridge")
    print(f"  extension: {EXTENSION}")
    print(f"  runtime:   {RUNTIME}")
    print(f"  MCP:       {MCP_CONFIG} -> rappter-chrome-local")
    print(f"  skill:     {SKILL_DIR}")
    print(f"  token:     {shared_token}")
    print(
        f"  extension: reloaded {len(reloaded_extensions)} profile(s)"
        if reloaded_extensions
        else "  extension: load/reload the unpacked extension once"
    )
    if not args.keep_legacy:
        print("  legacy:    Anthropic launcher/config removed")
    print()
    print(f"Open {extensions_url}")
    print("Enable Developer mode, load the extension folder as unpacked,")
    print("paste the token in its popup, then click Save & connect.")
    print("Restart Copilot CLI after setup. No Claude binary or vendor login is used.")
    print()
    print("Installed commands:")
    print(f"  python3 {RUNTIME / 'bridge.py'} identity")
    print(f"  python3 {RUNTIME / 'bridge.py'} tabs")
    print(f"  python3 {RUNTIME / 'gvoice.py'} probe")
    print(f"  python3 {RUNTIME / 'voice_assistant.py'} --reply-latest")
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
        recover_interrupted_install()
        return install(args)


if __name__ == "__main__":
    raise SystemExit(main())
