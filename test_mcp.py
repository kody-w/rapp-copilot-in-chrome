#!/usr/bin/env python3
"""Protocol smoke test for the vendorless MCP server."""

import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
from rappter_chrome_mcp import batch_step
from rappter_chrome_mcp import Server, handle

assert batch_step(
    "read_page",
    {"tabId": 7, "selector": "a", "limit": 3},
) == {
    "cmd": "query",
    "args": {"tabId": 7, "selector": "a", "limit": 3},
}

server = Server()
assert handle(server, [])["error"]["code"] == -32600
assert handle(
    server,
    {"jsonrpc": "2.0", "id": None, "method": "ping"},
) == {"jsonrpc": "2.0", "id": None, "result": {}}
assert handle(
    server,
    {"jsonrpc": "2.0", "method": "ping"},
) is None
assert handle(
    server,
    {"jsonrpc": "2.0", "id": 9, "method": "missing"},
)["error"]["code"] == -32601
assert handle(
    server,
    {"jsonrpc": "2.0", "id": 10, "method": "ping", "params": 1},
)["error"]["code"] == -32602
try:
    server.call("not-a-tool", {})
    raise AssertionError("unknown tool should fail")
except Exception as exc:
    assert "unknown tool" in str(exc)
assert server.chrome is None
assert batch_step(
    "computer",
    {"tabId": 7, "action": "click", "selector": "button", "index": 2},
) == {
    "cmd": "click",
    "args": {"tabId": 7, "selector": "button", "index": 2},
}
assert batch_step(
    "form_input",
    {"tabId": 7, "selector": "input", "value": "hello"},
) == {
    "cmd": "type",
    "args": {
        "tabId": 7,
        "selector": "input",
        "text": "hello",
        "submit": False,
    },
}

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
    print(
        f"MCP server: initialize + {len(names)} tools + batch mappings "
        "+ JSON-RPC validation passed"
    )
finally:
    process.terminate()
    process.wait(timeout=5)

binary = subprocess.Popen(
    [sys.executable, str(root / "rappter_chrome_mcp.py")],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
)
try:
    binary.stdin.write(b"\xff\n")
    binary.stdin.write(b"{broken\n")
    binary.stdin.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": 33, "method": "ping"}
        ).encode()
        + b"\n"
    )
    binary.stdin.flush()
    replies = [json.loads(binary.stdout.readline()) for _ in range(3)]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["error"]["code"] == -32700
    assert replies[2] == {"jsonrpc": "2.0", "id": 33, "result": {}}
    print("MCP malformed-byte recovery passed")
finally:
    binary.terminate()
    binary.wait(timeout=5)
