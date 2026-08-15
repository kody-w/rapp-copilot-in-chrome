#!/usr/bin/env python3
"""Protocol smoke test for the vendorless MCP server."""

import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
process = subprocess.Popen(
    [sys.executable, str(root / "rappter_chrome_mcp.py")],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
)


def rpc(message):
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())


try:
    initialized = rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    assert initialized["result"]["serverInfo"]["name"] == "rappter-chrome-local"

    listed = rpc(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "tabs_context_mcp" in names
    assert "navigate" in names
    assert "get_page_text" in names
    assert "form_input" in names
    assert "javascript_tool" in names
    assert "browser_batch" in names
    print(f"MCP server: initialize + {len(names)} tools passed")
finally:
    process.terminate()
    process.wait(timeout=5)
