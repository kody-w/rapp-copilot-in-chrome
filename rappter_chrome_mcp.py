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
TOOL_NAMES = {tool["name"] for tool in TOOLS}


NAME_TO_COMMAND = {
    "navigate": "navigate",
    "get_page_text": "text",
    "javascript_tool": "eval",
    "tabs_create_mcp": "create",
    "tabs_close_mcp": "close",
}


def batch_step(name, args):
    """Translate an MCP tool call to the extension's command vocabulary."""
    if name == "tabs_context_mcp":
        return {"cmd": "tabs", "args": {}}
    if name == "read_page":
        if args.get("selector"):
            return {
                "cmd": "query",
                "args": {
                    "tabId": args["tabId"],
                    "selector": args["selector"],
                    "limit": args.get("limit", 40),
                },
            }
        return {"cmd": "text", "args": {"tabId": args["tabId"]}}
    if name == "form_input":
        return {
            "cmd": "type",
            "args": {
                "tabId": args["tabId"],
                "selector": args["selector"],
                "text": args["value"],
                "submit": args.get("submit", False),
            },
        }
    if name == "computer":
        action = args["action"]
        if action == "click":
            command_args = {
                "tabId": args["tabId"],
                "selector": args["selector"],
                "index": args.get("index", 0),
            }
        elif action == "type":
            command_args = {
                "tabId": args["tabId"],
                "selector": args["selector"],
                "text": args.get("text", ""),
                "submit": args.get("submit", False),
            }
        elif action in ("activate", "screenshot"):
            command_args = {"tabId": args["tabId"]}
        else:
            raise BridgeError(f"unsupported computer action in batch: {action}")
        return {"cmd": action, "args": command_args}
    if name == "list_connected_browsers":
        return {"cmd": "ping", "args": {}}
    if name == "browser_batch":
        raise BridgeError("nested browser_batch is not supported")
    command = NAME_TO_COMMAND.get(name)
    if command:
        if command == "eval":
            return {
                "cmd": "eval",
                "args": {"tabId": args["tabId"], "code": args["code"]},
            }
        return {"cmd": command, "args": args}
    raise BridgeError(f"unsupported tool in browser_batch: {name}")


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
        if name not in TOOL_NAMES:
            raise BridgeError(f"unknown tool: {name}")

        # Translate and validate before opening the browser channel. Invalid
        # tool calls must fail immediately rather than waiting 35 seconds for
        # an extension connection they can never use.
        translated_batch = None
        if name == "browser_batch":
            actions = args.get("actions")
            if not isinstance(actions, list):
                raise BridgeError("browser_batch.actions must be an array")
            translated_batch = []
            for item in actions:
                if not isinstance(item, dict):
                    raise BridgeError("browser_batch action must be an object")
                tool_name = item.get("name")
                tool_input = item.get("input")
                if not isinstance(tool_name, str) or not isinstance(
                    tool_input, dict
                ):
                    raise BridgeError(
                        "browser_batch action requires string name and object input"
                    )
                translated_batch.append(batch_step(tool_name, tool_input))

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
            return chrome.batch(translated_batch)

        if name == "list_connected_browsers":
            return [{"name": "local Chromium", "connected": True}]

        command = NAME_TO_COMMAND.get(name)
        if command:
            if command == "eval":
                return chrome.eval(args["tabId"], args["code"])
            return chrome.call(command, **args)
        raise BridgeError(f"unsupported tool: {name}")


def text_result(value, is_error=False):
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def rpc_error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def handle(server, request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Invalid Request")
    if not isinstance(request.get("method"), str):
        return rpc_error(request.get("id"), -32600, "Invalid Request")

    notification = "id" not in request
    request_id = request.get("id")
    method = request["method"]
    params = request.get("params", {})
    if not isinstance(params, dict):
        return None if notification else rpc_error(
            request_id, -32602, "Invalid params"
        )

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
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
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return None if notification else rpc_error(
                request_id, -32602, "Invalid params"
            )
        try:
            result = text_result(server.call(name, arguments))
        except BridgeError as exc:
            server.reset()
            result = text_result(str(exc), is_error=True)
        except Exception:
            server.reset()
            print("unexpected browser tool failure", file=sys.stderr)
            result = text_result("Internal browser tool error", is_error=True)
    else:
        return None if notification else rpc_error(
            request_id, -32601, "Method not found"
        )

    return None if notification else rpc_result(request_id, result)


def main():
    server = Server()
    try:
        for raw in sys.stdin.buffer:
            try:
                line = raw.decode("utf-8")
                request = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                emit(rpc_error(None, -32700, "Parse error"))
                continue
            response = handle(server, request)
            if response is not None:
                emit(response)
    finally:
        server.reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
