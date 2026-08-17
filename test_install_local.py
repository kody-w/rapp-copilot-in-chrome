#!/usr/bin/env python3
"""Installer must never clobber an existing malformed MCP config."""

import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap

root = pathlib.Path(__file__).resolve().parent
home = pathlib.Path(tempfile.mkdtemp(prefix="rappter-install-test-"))
config = home / ".copilot" / "mcp-config.json"
config.parent.mkdir(parents=True)
original = '{"mcpServers": BROKEN'
config.write_text(original)

env = {**os.environ, "HOME": str(home)}
result = subprocess.run(
    [sys.executable, str(root / "install_local.py"), "--no-open"],
    capture_output=True,
    text=True,
    env=env,
)
assert result.returncode != 0
assert config.read_text() == original
assert "refusing to overwrite unreadable JSON" in (result.stderr + result.stdout)
print("installer malformed-config refusal passed")

# Two installers sharing one HOME must serialize and publish one token.
parallel_home = pathlib.Path(tempfile.mkdtemp(prefix="rappter-install-parallel-"))
parallel_env = {**os.environ, "HOME": str(parallel_home)}
commands = [
    subprocess.Popen(
        [sys.executable, str(root / "install_local.py"), "--no-open"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=parallel_env,
    )
    for _ in range(2)
]
outputs = [process.communicate(timeout=60) for process in commands]
assert all(process.returncode == 0 for process in commands), outputs
published = (parallel_home / ".rappter-chrome" / "token").read_text().strip()
reported = [
    next(line.split("token:", 1)[1].strip() for line in stdout.splitlines()
         if "token:" in line)
    for stdout, _ in outputs
]
assert reported == [published, published]
understudy_template = (
    parallel_home
    / ".rappter-chrome"
    / "runtime"
    / "com.rapp.digital-understudy.plist.template"
).read_text()
assert "__PYTHON__" in understudy_template
assert "__RUNTIME__" in understudy_template
voice_template = (
    parallel_home
    / ".rappter-chrome"
    / "runtime"
    / "com.rapp.voice-assistant.plist.template"
).read_text()
assert "__PYTHON__" not in voice_template
assert "__RUNTIME__" not in voice_template
print("installer concurrency serialization passed")

# Failure after the first directory swap must restore every old directory.
rollback_home = pathlib.Path(tempfile.mkdtemp(prefix="rappter-install-rollback-"))
script = textwrap.dedent(
    f"""
    import argparse, importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "installer", {str(root / "install_local.py")!r}
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = pathlib.Path.home()
    for target in (module.RUNTIME, module.EXTENSION, module.SKILL_DIR):
        target.mkdir(parents=True, exist_ok=True)
        (target / "old-marker").write_text("old")
    original = module.swap_dir
    calls = {{"count": 0}}
    def fail_second(stage, destination, backup=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected swap failure")
        return original(stage, destination, backup)
    module.swap_dir = fail_second
    try:
        module.install(argparse.Namespace(keep_legacy=False, no_open=True))
    except RuntimeError as exc:
        assert "injected" in str(exc)
    else:
        raise AssertionError("expected injected failure")
    for target in (module.RUNTIME, module.EXTENSION, module.SKILL_DIR):
        assert (target / "old-marker").read_text() == "old"
    print("installer rollback passed")
    """
)
rollback = subprocess.run(
    [sys.executable, "-c", script],
    capture_output=True,
    text=True,
    env={**os.environ, "HOME": str(rollback_home)},
)
assert rollback.returncode == 0, rollback.stderr
print(rollback.stdout.strip())

# A failure after restarting an upgraded understudy must stop it, restore the
# old files, and verify the old service before deleting the journal.
service_home = pathlib.Path(
    tempfile.mkdtemp(prefix="rappter-install-service-rollback-")
)
service_script = textwrap.dedent(
    f"""
    import argparse, importlib.util
    spec = importlib.util.spec_from_file_location(
        "installer", {str(root / "install_local.py")!r}
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for target in (module.RUNTIME, module.EXTENSION, module.SKILL_DIR):
        target.mkdir(parents=True, exist_ok=True)
        (target / "old-marker").write_text("old")
    module.UNDERSTUDY_BARRIER.write_text("committed")
    state = {{"loaded": True, "stops": 0, "starts": 0, "barriers": []}}
    module.voice_service_loaded = lambda: False
    module.understudy_service_loaded = lambda: state["loaded"]
    def stop():
        state["loaded"] = False
        state["stops"] += 1
    def restart():
        state["loaded"] = True
        state["starts"] += 1
        state["barriers"].append(module.UNDERSTUDY_BARRIER.exists())
    module.stop_understudy_service = stop
    module.restart_understudy_service = restart
    original_write = module.write_json_atomic
    def fail_commit(path, value):
        if path == module.JOURNAL and value.get("phase") == "committed":
            raise RuntimeError("commit journal failed")
        return original_write(path, value)
    module.write_json_atomic = fail_commit
    try:
        module.install(argparse.Namespace(keep_legacy=False, no_open=True))
    except RuntimeError as exc:
        assert "commit journal failed" in str(exc)
    else:
        raise AssertionError("expected post-restart failure")
    assert state == {{
        "loaded": True,
        "stops": 2,
        "starts": 2,
        "barriers": [False, True],
    }}
    for target in (module.RUNTIME, module.EXTENSION, module.SKILL_DIR):
        assert (target / "old-marker").read_text() == "old"
    assert not module.JOURNAL.exists()
    print("installer service rollback passed")
    """
)
service_rollback = subprocess.run(
    [sys.executable, "-c", service_script],
    capture_output=True,
    text=True,
    env={**os.environ, "HOME": str(service_home)},
)
assert service_rollback.returncode == 0, (
    service_rollback.stdout,
    service_rollback.stderr,
)
print(service_rollback.stdout.strip())

# SIGKILL after the first published directory leaves a journal that restores
# the coherent previous generation on the next run.
kill_home = pathlib.Path(tempfile.mkdtemp(prefix="rappter-install-kill-"))
kill_env = {**os.environ, "HOME": str(kill_home)}
baseline = subprocess.run(
    [sys.executable, str(root / "install_local.py"), "--no-open"],
    capture_output=True,
    text=True,
    env=kill_env,
)
assert baseline.returncode == 0, baseline.stderr
for target in (
    kill_home / ".rappter-chrome" / "runtime",
    kill_home / ".rappter-chrome" / "extension",
    kill_home / ".copilot" / "skills" / "rappter-chrome-local",
):
    (target / "old-marker").write_text("old")

kill_script = textwrap.dedent(
    f"""
    import argparse, importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "installer", {str(root / "install_local.py")!r}
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.swap_dir
    calls = 0
    def die_after_first(stage, destination, backup=None):
        global calls
        result = original(stage, destination, backup)
        calls += 1
        if calls == 1:
            os._exit(77)
        return result
    module.swap_dir = die_after_first
    module.install(argparse.Namespace(keep_legacy=False, no_open=True))
    """
)
killed = subprocess.run(
    [sys.executable, "-c", kill_script],
    env=kill_env,
)
assert killed.returncode == 77
journal = kill_home / ".rappter-chrome" / ".install-journal.json"
assert journal.exists()

recover_script = textwrap.dedent(
    f"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "installer", {str(root / "install_local.py")!r}
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.install_lock():
        module.recover_interrupted_install()
    for target in (module.RUNTIME, module.EXTENSION, module.SKILL_DIR):
        assert (target / "old-marker").read_text() == "old"
    assert not module.JOURNAL.exists()
    print("SIGKILL journal recovery passed")
    """
)
recovered = subprocess.run(
    [sys.executable, "-c", recover_script],
    capture_output=True,
    text=True,
    env=kill_env,
)
assert recovered.returncode == 0, recovered.stderr
print(recovered.stdout.strip())

# SIGKILL after journaling but before the first rename must leave the intact
# previous runtime in place. A prepared backup path does not prove a swap ran.
preswap_home = pathlib.Path(tempfile.mkdtemp(prefix="rappter-install-preswap-"))
preswap_env = {**os.environ, "HOME": str(preswap_home)}
baseline = subprocess.run(
    [sys.executable, str(root / "install_local.py"), "--no-open"],
    capture_output=True,
    text=True,
    env=preswap_env,
)
assert baseline.returncode == 0, baseline.stderr
preswap_targets = (
    preswap_home / ".rappter-chrome" / "runtime",
    preswap_home / ".rappter-chrome" / "extension",
    preswap_home / ".copilot" / "skills" / "rappter-chrome-local",
)
for target in preswap_targets:
    (target / "old-marker").write_text("old")

preswap_script = textwrap.dedent(
    f"""
    import argparse, importlib.util, os
    spec = importlib.util.spec_from_file_location(
        "installer", {str(root / "install_local.py")!r}
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def die_before_swap(stage, destination, backup=None):
        os._exit(78)
    module.swap_dir = die_before_swap
    module.install(argparse.Namespace(keep_legacy=False, no_open=True))
    """
)
killed = subprocess.run([sys.executable, "-c", preswap_script], env=preswap_env)
assert killed.returncode == 78
recovered = subprocess.run(
    [sys.executable, "-c", recover_script],
    capture_output=True,
    text=True,
    env=preswap_env,
)
assert recovered.returncode == 0, recovered.stderr
for target in preswap_targets:
    assert (target / "old-marker").read_text() == "old"
print("pre-swap SIGKILL recovery passed")
