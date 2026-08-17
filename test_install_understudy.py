#!/usr/bin/env python3
"""Transactional configuration and LaunchAgent tests for the understudy."""

import json
import pathlib
import subprocess
import sys
import tempfile

import digital_understudy
import install_understudy

tmp = pathlib.Path(tempfile.mkdtemp(prefix="understudy-install-test-"))
home = tmp / "home"
root = home / ".rappter-chrome"
runtime = tmp / "runtime"
runtime.mkdir(parents=True)

for name in ("digital_understudy.py", "com.rapp.digital-understudy.plist.template"):
    source = pathlib.Path(__file__).resolve().parent / name
    (runtime / name).write_bytes(source.read_bytes())

config = {
    "google_voice_account": "expected@example.com",
    "google_voice_peer": "5558675309",
    "rapp_owner": "example-owner",
}
root.mkdir(parents=True)
(root / "config.json").write_text(json.dumps(config), encoding="utf-8")

install_understudy.HOME = home
install_understudy.ROOT = root
install_understudy.CONFIG_FILE = root / "config.json"
install_understudy.PLIST = (
    home / "Library" / "LaunchAgents" / "com.rapp.digital-understudy.plist"
)
install_understudy.HERE = runtime
install_understudy.TEMPLATE = (
    runtime / "com.rapp.digital-understudy.plist.template"
)
install_understudy.JOURNAL = root / ".understudy-install-journal.json"
install_understudy.CONFIG_BACKUP = root / ".understudy-config.backup"
install_understudy.PLIST_BACKUP = root / ".understudy-plist.backup"
install_understudy.BARRIER = root / ".understudy-start-barrier"

digital_understudy.ROOT = root
digital_understudy.STUDY_ROOT = root / "understudy"
digital_understudy.STATE_FILE = digital_understudy.STUDY_ROOT / "state.json"
digital_understudy.CONFIG_FILE = root / "config.json"
digital_understudy.STATE_BACKUP = (
    digital_understudy.STUDY_ROOT / "state.json.bak"
)
digital_understudy.voice_twin.google_voice_conversation_binding = (
    lambda cfg: {
        "schema": "rapp-messaging-bound-conversation/1.0",
        "conversation_id": "conversation:" + ("a" * 64),
        "audience_id": "audience:" + ("b" * 64),
    }
)

calls = []
service = {"loaded": False}


def fake_run(command, **kwargs):
    calls.append((command, kwargs))
    if command[:2] == ["launchctl", "print"]:
        return subprocess.CompletedProcess(
            command,
            0 if service["loaded"] else 1,
            stdout=(
                str(pathlib.Path(sys.executable).resolve())
                + " "
                + str(runtime / "digital_understudy.py")
            ),
            stderr="",
        )
    if command[:2] == ["launchctl", "bootout"]:
        service["loaded"] = False
    if command[:2] == ["launchctl", "bootstrap"]:
        service["loaded"] = True
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


original_run = install_understudy.subprocess.run
install_understudy.subprocess.run = fake_run
try:
    result = install_understudy.install()
finally:
    install_understudy.subprocess.run = original_run

assert result["status"] == "installed"
enabled = json.loads((root / "config.json").read_text())
assert enabled["understudy_enabled"] is True
assert enabled["understudy_duration_days"] == 30
assert enabled["understudy_include_conversation_excerpts"] is True
assert result["study"]["status"] == "not-initialized"
assert not digital_understudy.STATE_FILE.exists()
assert install_understudy.BARRIER.read_text() == "committed\n"
plist = install_understudy.PLIST.read_text()
assert "__PYTHON__" not in plist
assert "__HOME__" not in plist
assert "__RUNTIME__" not in plist
assert str(runtime / "digital_understudy.py") in plist
assert any(command[:2] == ["launchctl", "bootstrap"] for command, _ in calls)
assert any(command[:2] == ["launchctl", "kickstart"] for command, _ in calls)
assert install_understudy.PLIST.stat().st_mode & 0o777 == 0o600
assert (root / "config.json").stat().st_mode & 0o777 == 0o600

# Simulate the resident process starting only after the committed barrier.
state = digital_understudy.initialize(cfg=enabled)
assert state["duration_days"] == 30
assert state["completed"] is False

# A failed replacement restores the exact prior config/plist and loaded job.
original_config = install_understudy.CONFIG_FILE.read_bytes()
original_plist = install_understudy.PLIST.read_bytes()
bootstrap_calls = {"count": 0}
service["loaded"] = True


def fail_first_bootstrap(command, **kwargs):
    if command[:2] == ["launchctl", "print"]:
        return subprocess.CompletedProcess(
            command,
            0 if service["loaded"] else 1,
            stdout=(
                str(pathlib.Path(sys.executable).resolve())
                + " "
                + str(runtime / "digital_understudy.py")
            ),
            stderr="",
        )
    if command[:2] == ["launchctl", "bootout"]:
        service["loaded"] = False
    if command[:2] == ["launchctl", "bootstrap"]:
        bootstrap_calls["count"] += 1
        if bootstrap_calls["count"] == 1:
            raise subprocess.CalledProcessError(5, command)
        service["loaded"] = True
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


install_understudy.subprocess.run = fail_first_bootstrap
try:
    try:
        install_understudy.install()
        raise AssertionError("failed bootstrap was reported as success")
    except subprocess.CalledProcessError:
        pass
finally:
    install_understudy.subprocess.run = original_run
assert bootstrap_calls["count"] == 2
assert install_understudy.CONFIG_FILE.read_bytes() == original_config
assert install_understudy.PLIST.read_bytes() == original_plist
assert install_understudy.BARRIER.read_text() == "committed\n"
assert not install_understudy.JOURNAL.exists()
assert not install_understudy.CONFIG_BACKUP.exists()
assert not install_understudy.PLIST_BACKUP.exists()

# A stopped prior installation retains its committed barrier after rollback.
service["loaded"] = False
install_understudy.BARRIER.write_text("committed\n", encoding="utf-8")


def fail_stopped_reinstall(command, **kwargs):
    if command[:2] == ["launchctl", "print"]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
    if command[:2] == ["launchctl", "bootstrap"]:
        raise subprocess.CalledProcessError(5, command)
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


install_understudy.subprocess.run = fail_stopped_reinstall
try:
    try:
        install_understudy.install()
        raise AssertionError("stopped reinstall failure was reported as success")
    except subprocess.CalledProcessError:
        pass
finally:
    install_understudy.subprocess.run = original_run
assert install_understudy.BARRIER.read_text() == "committed\n"

# A first-install bootstrap failure restores disabled config and removes the
# newly initialized study instead of starting its 30-day clock.
fresh = tmp / "fresh"
fresh_home = fresh / "home"
fresh_root = fresh_home / ".rappter-chrome"
fresh_root.mkdir(parents=True)
fresh_config = fresh_root / "config.json"
fresh_original = json.dumps(config).encode("utf-8")
fresh_config.write_bytes(fresh_original)
install_understudy.HOME = fresh_home
install_understudy.ROOT = fresh_root
install_understudy.CONFIG_FILE = fresh_config
install_understudy.PLIST = (
    fresh_home
    / "Library"
    / "LaunchAgents"
    / "com.rapp.digital-understudy.plist"
)
install_understudy.JOURNAL = fresh_root / ".understudy-install-journal.json"
install_understudy.CONFIG_BACKUP = fresh_root / ".understudy-config.backup"
install_understudy.PLIST_BACKUP = fresh_root / ".understudy-plist.backup"
install_understudy.BARRIER = fresh_root / ".understudy-start-barrier"
digital_understudy.ROOT = fresh_root
digital_understudy.STUDY_ROOT = fresh_root / "understudy"
digital_understudy.STATE_FILE = digital_understudy.STUDY_ROOT / "state.json"
digital_understudy.STATE_BACKUP = (
    digital_understudy.STUDY_ROOT / "state.json.bak"
)
digital_understudy.CONFIG_FILE = fresh_config


def fail_fresh_bootstrap(command, **kwargs):
    if command[:2] == ["launchctl", "print"]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
    if command[:2] == ["launchctl", "bootstrap"]:
        raise subprocess.CalledProcessError(5, command)
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


install_understudy.subprocess.run = fail_fresh_bootstrap
try:
    try:
        install_understudy.install()
        raise AssertionError("fresh failed bootstrap was reported as success")
    except subprocess.CalledProcessError:
        pass
finally:
    install_understudy.subprocess.run = original_run
assert fresh_config.read_bytes() == fresh_original
assert not install_understudy.PLIST.exists()
assert not digital_understudy.STUDY_ROOT.exists()
assert not install_understudy.JOURNAL.exists()
assert not install_understudy.BARRIER.exists()

print("Understudy installer: config and LaunchAgent checks passed")
