#!/usr/bin/env python3
"""Enable and install the 30-day digital understudy LaunchAgent."""

import argparse
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import digital_understudy

HERE = Path(__file__).resolve().parent
HOME = Path.home()
ROOT = HOME / ".rappter-chrome"
CONFIG_FILE = ROOT / "config.json"
PLIST = HOME / "Library" / "LaunchAgents" / "com.rapp.digital-understudy.plist"
TEMPLATE = HERE / "com.rapp.digital-understudy.plist.template"
LABEL = "com.rapp.digital-understudy"
JOURNAL = ROOT / ".understudy-install-journal.json"
CONFIG_BACKUP = ROOT / ".understudy-config.backup"
PLIST_BACKUP = ROOT / ".understudy-plist.backup"
BARRIER = ROOT / ".understudy-start-barrier"


def write_bytes_atomic(path, payload, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def install_lock():
    ROOT.mkdir(parents=True, exist_ok=True)
    handle = open(ROOT / ".install.lock", "a+", encoding="utf-8")
    os.chmod(handle.name, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def enabled_config_bytes():
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot enable understudy with invalid config") from exc
    if not isinstance(value, dict):
        raise RuntimeError("understudy config must be an object")
    value.update({
        "understudy_enabled": True,
        "understudy_duration_days": 30,
        "understudy_include_conversation_excerpts": True,
        "understudy_max_conversation_rows": 12,
        "understudy_analysis_timeout_seconds": 240,
    })
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def rendered_plist():
    text = TEMPLATE.read_text(encoding="utf-8")
    return (
        text.replace("__PYTHON__", str(Path(sys.executable).resolve()))
        .replace("__HOME__", str(HOME))
        .replace("__RUNTIME__", str(HERE))
        .encode("utf-8")
    )


def launch_target():
    return f"gui/{os.getuid()}/{LABEL}"


def service_loaded():
    result = subprocess.run(
        ["launchctl", "print", launch_target()],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def validate_plist(payload):
    try:
        value = plistlib.loads(payload)
    except Exception as exc:
        raise RuntimeError("understudy plist is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("Label") != LABEL
        or not isinstance(value.get("ProgramArguments"), list)
        or len(value["ProgramArguments"]) < 2
        or value["ProgramArguments"][0]
        != str(Path(sys.executable).resolve())
        or value["ProgramArguments"][1] != str(HERE / "digital_understudy.py")
        or not isinstance(value.get("EnvironmentVariables"), dict)
        or value["EnvironmentVariables"].get("UNDERSTUDY_START_BARRIER")
        != str(BARRIER)
    ):
        raise RuntimeError("understudy plist targets the wrong runtime")


def verify_loaded():
    result = subprocess.run(
        ["launchctl", "print", launch_target()],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        result.returncode != 0
        or str(HERE / "digital_understudy.py") not in result.stdout
        or str(Path(sys.executable).resolve()) not in result.stdout
    ):
        raise RuntimeError("understudy LaunchAgent did not load exact runtime")


def _restore(journal):
    BARRIER.unlink(missing_ok=True)
    if service_loaded():
        subprocess.run(
            ["launchctl", "bootout", launch_target()],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if service_loaded():
            raise RuntimeError("new understudy service did not stop")
    if journal["config_existed"]:
        if not CONFIG_BACKUP.exists():
            raise RuntimeError("understudy config backup is missing")
        write_bytes_atomic(CONFIG_FILE, CONFIG_BACKUP.read_bytes())
    else:
        CONFIG_FILE.unlink(missing_ok=True)
    if journal["plist_existed"]:
        if not PLIST_BACKUP.exists():
            raise RuntimeError("understudy plist backup is missing")
        write_bytes_atomic(PLIST, PLIST_BACKUP.read_bytes())
    else:
        PLIST.unlink(missing_ok=True)
    if not journal["state_existed"]:
        study_root = digital_understudy.STUDY_ROOT.resolve()
        allowed_root = ROOT.resolve()
        if os.path.commonpath([str(study_root), str(allowed_root)]) != str(
            allowed_root
        ):
            raise RuntimeError("understudy state path is unsafe")
        if not journal["study_existed"]:
            shutil.rmtree(study_root, ignore_errors=True)
        else:
            digital_understudy.STATE_FILE.unlink(missing_ok=True)
            digital_understudy.STATE_BACKUP.unlink(missing_ok=True)
    if journal.get("barrier_existed"):
        write_bytes_atomic(BARRIER, b"committed\n")
    if journal["service_was_loaded"]:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)],
            check=True,
        )
        subprocess.run(["launchctl", "enable", launch_target()], check=False)
        subprocess.run(
            ["launchctl", "kickstart", "-p", launch_target()],
            check=True,
            capture_output=True,
            text=True,
        )
        if not service_loaded():
            raise RuntimeError("previous understudy service did not recover")


def recover_install():
    if not JOURNAL.exists():
        return
    try:
        journal = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("understudy install journal is invalid") from exc
    if not isinstance(journal, dict) or journal.get("version") != 1:
        raise RuntimeError("understudy install journal is invalid")
    if journal.get("phase") != "committed":
        _restore(journal)
    elif not BARRIER.exists():
        write_bytes_atomic(BARRIER, b"committed\n")
    CONFIG_BACKUP.unlink(missing_ok=True)
    PLIST_BACKUP.unlink(missing_ok=True)
    JOURNAL.unlink(missing_ok=True)


def install():
    if sys.platform != "darwin":
        raise RuntimeError("the digital understudy service currently requires macOS")
    if not TEMPLATE.is_file() or not (HERE / "digital_understudy.py").is_file():
        raise RuntimeError("understudy runtime is incomplete")
    with install_lock():
        recover_install()
        plist_payload = rendered_plist()
        validate_plist(plist_payload)
        config_payload = enabled_config_bytes()
        was_loaded = service_loaded()
        journal = {
            "version": 1,
            "phase": "prepared",
            "config_existed": CONFIG_FILE.exists(),
            "plist_existed": PLIST.exists(),
            "study_existed": digital_understudy.STUDY_ROOT.exists(),
            "state_existed": digital_understudy.STATE_FILE.exists(),
            "barrier_existed": BARRIER.exists(),
            "service_was_loaded": was_loaded,
        }
        if CONFIG_FILE.exists():
            write_bytes_atomic(CONFIG_BACKUP, CONFIG_FILE.read_bytes())
        if PLIST.exists():
            write_bytes_atomic(PLIST_BACKUP, PLIST.read_bytes())
        write_bytes_atomic(
            JOURNAL,
            (json.dumps(journal, separators=(",", ":")) + "\n").encode(),
        )
        try:
            if was_loaded:
                subprocess.run(
                    ["launchctl", "bootout", launch_target()],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if service_loaded():
                    raise RuntimeError("previous understudy service did not stop")
            BARRIER.unlink(missing_ok=True)
            journal["phase"] = "stopped"
            write_bytes_atomic(
                JOURNAL,
                (json.dumps(journal, separators=(",", ":")) + "\n").encode(),
            )
            write_bytes_atomic(CONFIG_FILE, config_payload)
            write_bytes_atomic(PLIST, plist_payload)
            journal["phase"] = "published"
            write_bytes_atomic(
                JOURNAL,
                (json.dumps(journal, separators=(",", ":")) + "\n").encode(),
            )
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)],
                check=True,
            )
            subprocess.run(["launchctl", "enable", launch_target()], check=False)
            subprocess.run(
                ["launchctl", "kickstart", "-p", launch_target()],
                check=True,
                capture_output=True,
                text=True,
            )
            verify_loaded()
            journal["phase"] = "committed"
            write_bytes_atomic(
                JOURNAL,
                (json.dumps(journal, separators=(",", ":")) + "\n").encode(),
            )
            write_bytes_atomic(BARRIER, b"committed\n")
        except Exception:
            try:
                _restore(journal)
            except Exception as rollback:
                raise RuntimeError(
                    "understudy install and rollback both failed"
                ) from rollback
            CONFIG_BACKUP.unlink(missing_ok=True)
            PLIST_BACKUP.unlink(missing_ok=True)
            JOURNAL.unlink(missing_ok=True)
            raise
        CONFIG_BACKUP.unlink(missing_ok=True)
        PLIST_BACKUP.unlink(missing_ok=True)
        JOURNAL.unlink(missing_ok=True)
    return {
        "status": "installed",
        "study": digital_understudy.status(),
    }


def status():
    result = subprocess.run(
        ["launchctl", "print", launch_target()],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "loaded": result.returncode == 0,
        "study": digital_understudy.status(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "status"))
    args = parser.parse_args()
    value = install() if args.action == "install" else status()
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
