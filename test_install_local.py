#!/usr/bin/env python3
"""Installer must never clobber an existing malformed MCP config."""

import os
import pathlib
import subprocess
import sys
import tempfile

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
