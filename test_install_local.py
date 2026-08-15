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
    def fail_second(stage, destination):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected swap failure")
        return original(stage, destination)
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
