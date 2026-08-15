#!/usr/bin/env python3
"""Vendorless stdio MCP server for the Rappter Chromium extension."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import BridgeError, Chrome  # noqa: E402


TOOLS = [
    {
        "name": "tabs_context_mcp",
        "description": "List tabs in the connected real Edge/Chrome profile.",
        "inputSchema": {
            "type": "object",
            "properties": {"createIfEmpty": {"type": "boolean"}},
        },
    },
    {
        "name": "tabs_create_mcp",
        "description": "Create a browser tab.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "active": {"type": "boolean"},
            },
        },
    },
    {
        "name": "tabs_close_mcp",
        "description": "Close a browser tab by tabId.",
        "inputSchema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}},
            "required": ["tabId"],
        },
    },
    {
        "name": "navigate",
        "description": "Navigate a real browser tab to a URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "url": {"type": "string"},
            },
            "required": ["tabId", "url"],
        },
    },
    {
        "name": "get_page_text",
        "description": "Read visible text from a real browser tab.",
        "inputSchema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}},
            "required": ["tabId"],
        },
    },
    {
        "name": "read_page",
        "description": "Read elements matching a CSS selector, or page text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "selector": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["tabId"],
        },
    },
    {
        "name": "form_input",
        "description": "Set a form field through its native value setter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "selector": {"type": "string"},
                "value": {"type": "string"},
                "submit": {"type": "boolean"},
            },
            "required": ["tabId", "selector", "value"],
        },
    },
    {
        "name": "computer",
        "description": "Click, type, activate, or screenshot a real browser tab.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "activate", "screenshot"],
                },
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "index": {"type": "integer"},
                "submit": {"type": "boolean"},
            },
            "required": ["tabId", "action"],
        },
    },
    {
        "name": "javascript_tool",
        "description": "Evaluate JavaScript in a real browser tab.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer"},
                "code": {"type": "string"},
            },
            "required": ["tabId", "code"],
        },
    },
    {
        "name": "browser_batch",
        "description": "Execute browser actions in order in one round trip.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "input": {"type": "object"},
                        },
                        "required": ["name", "input"],
                    },
                }
            },
            "required": ["actions"],
        },
    },
    {
        "name": "list_connected_browsers",
        "description": "Report the connected local Chromium browser.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


NAME_TO_COMMAND = {
    "navigate": "navigate",
    "get_page_text": "text",
    "javascript_tool": "eval",
    "tabs_create_mcp": "create",
    "tabs_close_mcp": "close",
}


class Server:
    def __init__(self):
        self.chrome = None

    def connection(self):
        if self.chrome is None:
            self.chrome = Chrome(wait=35)
            self.chrome.connect()
        return self.chrome

    def reset(self):
        if self.chrome:
            self.chrome.close()
        self.chrome = None

    def call(self, name, args):
        chrome = self.connection()

        if name == "tabs_context_mcp":
            tabs = chrome.tabs()
            if not tabs and args.get("createIfEmpty"):
                chrome.create(active=True)
                tabs = chrome.tabs()
            return {"availableTabs": tabs}

        if name == "read_page":
            if args.get("selector"):
                return chrome.query(
                    args["tabId"],
                    args["selector"],
                    args.get("limit", 40),
                )
            return chrome.text(args["tabId"])

        if name == "form_input":
            return chrome.type(
                args["tabId"],
                args["selector"],
                args["value"],
                args.get("submit", False),
            )

        if name == "computer":
            action = args["action"]
            tab = args["tabId"]
            if action == "click":
                return chrome.click(
                    tab,
                    args["selector"],
                    args.get("index", 0),
                )
            if action == "type":
                return chrome.type(
                    tab,
                    args["selector"],
                    args.get("text", ""),
                    args.get("submit", False),
                )
            if action == "activate":
                return chrome.activate(tab)
            if action == "screenshot":
                return chrome.screenshot(tab)

        if name == "browser_batch":
            actions = []
            for item in args["actions"]:
                tool_name = item["name"]
                tool_args = item["input"]
                command = NAME_TO_COMMAND.get(tool_name, tool_name)
                if command == "form_input":
                    command = "type"
                    tool_args = {
                        "tabId": tool_args["tabId"],
                        "selector": tool_args["selector"],
                        "text": tool_args["value"],
                        "submit": tool_args.get("submit", False),
                    }
                actions.append({"cmd": command, "args": tool_args})
            return chrome.batch(actions)

        if name == "list_connected_browsers":
            return [{"name": "local Chromium", "connected": True}]

        command = NAME_TO_COMMAND.get(name)
        if command:
            if command == "eval":
                return chrome.eval(args["tabId"], args["code"])
            return chrome.call(command, **args)
        raise BridgeError(f"unknown tool: {name}")


def text_result(value, is_error=False):
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def main():
    server = Server()
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue

            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "rappter-chrome-local",
                        "version": "1.0.0",
                    },
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "ping":
                result = {}
            elif method == "tools/call":
                params = request.get("params", {})
                try:
                    result = text_result(
                        server.call(
                            params.get("name", ""),
                            params.get("arguments", {}),
                        )
                    )
                except Exception as exc:
                    # A broken browser session is not reusable. The next call
                    # gets a fresh listener and a fresh extension connection.
                    server.reset()
                    result = text_result(
                        f"{type(exc).__name__}: {exc}",
                        is_error=True,
                    )
            else:
                result = text_result(f"unknown method: {method}", is_error=True)

            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
                + "\n"
            )
            sys.stdout.flush()
    finally:
        server.reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
