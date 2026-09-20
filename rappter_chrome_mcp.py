#!/usr/bin/env python3
"""Vendorless stdio MCP server for the Rappter Chromium extension."""

import base64
import binascii
import json
import math
import struct
import sys
import urllib.parse
import zlib
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
                "tabId": {"type": "integer", "minimum": 0},
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "activate", "screenshot"],
                },
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "index": {"type": "integer"},
                "submit": {"type": "boolean"},
                "fullPage": {
                    "type": "boolean",
                    "description": (
                        "Capture the full page (screenshot only); true cannot "
                        "be combined with region."
                    ),
                },
                "region": {
                    "type": "object",
                    "description": "Screenshot crop in CSS page coordinates.",
                    "properties": {
                        "x": {"type": "number", "minimum": 0},
                        "y": {"type": "number", "minimum": 0},
                        "width": {"type": "number", "exclusiveMinimum": 0},
                        "height": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["x", "y", "width", "height"],
                    "additionalProperties": False,
                },
                "scale": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 2,
                    "description": "Screenshot scale, greater than zero and at most 2.",
                },
            },
            "required": ["tabId", "action"],
            "allOf": [{
                "if": {
                    "properties": {
                        "action": {"const": "screenshot"},
                        "fullPage": {"const": True},
                    },
                    "required": ["fullPage"],
                },
                "then": {"not": {"required": ["region"]}},
            }],
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
        "description": "Query the live browser's instance, profile, and connection state.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_page_ax",
        "description": (
            "Read the tab's main-document accessibility tree, bounded by a "
            "node limit; child frames are excluded. "
            "References are document-scoped and invalidated by navigation "
            "or reload. Text is whitespace-normalized AX value plus exposed "
            "descendant StaticText names (InlineTextBox fallback), in tree "
            "order; accessible labels alone are not text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer", "minimum": 0},
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 5000,
                    "default": 1000,
                },
                "includeIgnored": {"type": "boolean", "default": False},
            },
            "required": ["tabId"],
        },
    },
    {
        "name": "find_elements",
        "description": (
            "Find main-document accessible elements matching all supplied "
            "role, name, and text criteria; child frames are excluded. Use literal, "
            "case-insensitive, whitespace-normalized matching. Role matches "
            "exactly; name/text use substrings unless exact. Text is AX value "
            "plus exposed descendant StaticText names (InlineTextBox fallback), "
            "in tree order; accessible labels alone are not text. "
            "References are document-scoped and invalidated by navigation "
            "or reload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer", "minimum": 0},
                "role": {"type": "string", "minLength": 1, "pattern": "\\S"},
                "name": {"type": "string", "minLength": 1, "pattern": "\\S"},
                "text": {"type": "string", "minLength": 1, "pattern": "\\S"},
                "exact": {"type": "boolean", "default": False},
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 5000,
                    "default": 40,
                },
            },
            "required": ["tabId"],
            "anyOf": [
                {"required": ["role"]},
                {"required": ["name"]},
                {"required": ["text"]},
            ],
        },
    },
    {
        "name": "wait_for",
        "description": "Wait for a CSS selector to exist; timeout is milliseconds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer", "minimum": 0},
                "selector": {"type": "string", "minLength": 1, "pattern": "\\S"},
                "timeout": {
                    "type": "integer", "minimum": 1, "maximum": 30000,
                    "default": 15000,
                },
            },
            "required": ["tabId", "selector"],
        },
    },
    {
        "name": "assert_page",
        "description": (
            "Assert visible, enabled, text-contains, or url-matches. Unmet "
            "assertions return passed:false and a tool error. Timeout is "
            "milliseconds; zero checks immediately. URL patterns are "
            "JavaScript regular expression source, without slash delimiters."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer", "minimum": 0},
                "condition": {
                    "type": "string",
                    "enum": ["visible", "enabled", "text-contains", "url-matches"],
                },
                "selector": {"type": "string", "minLength": 1, "pattern": "\\S"},
                "text": {"type": "string"},
                "pattern": {"type": "string"},
                "timeout": {
                    "type": "integer", "minimum": 0, "maximum": 30000,
                    "default": 0,
                },
            },
            "required": ["tabId", "condition"],
            "allOf": [
                {
                    "if": {"properties": {"condition": {
                        "enum": ["visible", "enabled", "text-contains"],
                    }}},
                    "then": {"required": ["selector"]},
                },
                {
                    "if": {"properties": {"condition": {"const": "text-contains"}}},
                    "then": {"required": ["text"]},
                },
                {
                    "if": {"properties": {"condition": {"const": "url-matches"}}},
                    "then": {"required": ["pattern"]},
                },
            ],
        },
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
NEW_COMMANDS = {
    "read_page_ax": "read_page_ax",
    "find_elements": "find",
    "wait_for": "waitfor",
    "assert_page": "assert",
}


def integer_arg(args, key, default=None, minimum=None, maximum=None):
    value = args.get(key, default)
    if type(value) is not int:
        raise BridgeError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise BridgeError(f"{key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise BridgeError(f"{key} must be at most {maximum}")
    return value


def boolean_arg(args, key, default=False):
    value = args.get(key, default)
    if type(value) is not bool:
        raise BridgeError(f"{key} must be a boolean")
    return value


def string_arg(args, key, nonempty=False):
    value = args.get(key)
    if not isinstance(value, str) or (nonempty and not value.strip()):
        qualifier = "nonempty " if nonempty else ""
        raise BridgeError(f"{key} must be a {qualifier}string")
    return value


def new_command(name, args):
    command_args = {"tabId": integer_arg(args, "tabId", minimum=0)}
    if name == "read_page_ax":
        command_args.update(
            limit=integer_arg(args, "limit", 1000, 1, 5000),
            includeIgnored=boolean_arg(args, "includeIgnored"),
        )
    elif name == "find_elements":
        for key in ("role", "name", "text"):
            if key in args:
                command_args[key] = string_arg(args, key, nonempty=True)
        if len(command_args) == 1:
            raise BridgeError("find_elements requires at least one nonempty role, name, text")
        command_args.update(
            exact=boolean_arg(args, "exact"),
            limit=integer_arg(args, "limit", 40, 1, 5000),
        )
    elif name == "wait_for":
        command_args.update(
            selector=string_arg(args, "selector", nonempty=True),
            timeout=integer_arg(args, "timeout", 15000, 1, 30000),
        )
    elif name == "assert_page":
        condition = string_arg(args, "condition")
        if condition not in ("visible", "enabled", "text-contains", "url-matches"):
            raise BridgeError(f"unsupported assertion condition: {condition}")
        command_args.update(
            condition=condition,
            timeout=integer_arg(args, "timeout", 0, 0, 30000),
        )
        for key in ("selector", "text", "pattern"):
            if key in args:
                string_arg(args, key, nonempty=key == "selector")
        if condition == "url-matches":
            command_args["pattern"] = string_arg(args, "pattern")
        else:
            command_args["selector"] = string_arg(args, "selector", nonempty=True)
            if condition == "text-contains":
                command_args["text"] = string_arg(args, "text")
    return {"cmd": NEW_COMMANDS[name], "args": command_args}


def screenshot_options(args):
    def finite_number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False

    options = {}
    if "fullPage" in args:
        options["fullPage"] = boolean_arg(args, "fullPage")
    if options.get("fullPage") and "region" in args:
        raise BridgeError("fullPage:true and region are mutually exclusive")
    if "region" in args:
        region = args["region"]
        keys = {"x", "y", "width", "height"}
        if not isinstance(region, dict) or set(region) != keys:
            raise BridgeError("region must contain exactly x, y, width, height")
        for key, value in region.items():
            if (
                not finite_number(value)
                or (value < 0 if key in ("x", "y") else value <= 0)
            ):
                bound = "nonnegative" if key in ("x", "y") else "positive"
                raise BridgeError(f"region.{key} must be a finite {bound} number")
        options["region"] = dict(region)
    if "scale" in args:
        value = args["scale"]
        if (
            not finite_number(value)
            or not 0 < value <= 2
        ):
            raise BridgeError("scale must be a finite number greater than 0 and at most 2")
        options["scale"] = value
    return options


def batch_step(name, args):
    """Translate an MCP tool call to the extension's command vocabulary."""
    if name in NEW_COMMANDS:
        return new_command(name, args)
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
        elif action == "activate":
            command_args = {"tabId": args["tabId"]}
        elif action == "screenshot":
            command_args = {
                "tabId": integer_arg(args, "tabId", minimum=0),
                **screenshot_options(args),
            }
        else:
            raise BridgeError(f"unsupported computer action in batch: {action}")
        return {"cmd": action, "args": command_args}
    if name == "list_connected_browsers":
        return {"cmd": "browser_info", "args": {}}
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
        if not isinstance(args, dict):
            raise BridgeError("tool arguments must be an object")

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
        else:
            translated = batch_step(name, args)

        chrome = self.connection()

        if name in NEW_COMMANDS:
            return chrome.call(translated["cmd"], **translated["args"])

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
                options = dict(translated["args"])
                del options["tabId"]
                if "fullPage" in options:
                    options["full_page"] = options.pop("fullPage")
                return chrome.screenshot(tab, **options)

        if name == "browser_batch":
            return chrome.batch(translated_batch)

        if name == "list_connected_browsers":
            return chrome.call("browser_info")

        command = NAME_TO_COMMAND.get(name)
        if command:
            if command == "eval":
                return chrome.eval(args["tabId"], args["code"])
            return chrome.call(command, **args)
        raise BridgeError(f"unsupported tool: {name}")


def png_base64(value):
    """Recognize PNG data URLs without treating arbitrary base64 as an image."""
    if not isinstance(value, str):
        return None
    header, comma, payload = value.partition(",")
    parts = header.split(";")
    if not comma or parts[0].lower() != "data:image/png":
        return None
    encoded = parts[-1].lower() == "base64"
    parameters = parts[1:-1] if encoded else parts[1:]
    if any("=" not in part for part in parameters):
        return None
    try:
        data = urllib.parse.unquote_to_bytes(payload)
        if encoded:
            data = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset = 8
    seen_data = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            return None
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:end - 4]
        expected_crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(data[offset + 4:end - 4]) != expected_crc:
            return None
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                return None
            width, height, depth, color, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", chunk)
            )
            depths = {
                0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                4: (8, 16), 6: (8, 16),
            }
            if (
                not width or not height or depth not in depths.get(color, ())
                or compression != 0 or filtering != 0 or interlace not in (0, 1)
            ):
                return None
        elif kind == b"IHDR":
            return None
        if kind == b"IDAT":
            seen_data = True
        if kind == b"IEND":
            if length == 0 and end == len(data) and seen_data:
                return base64.b64encode(data).decode("ascii")
            return None
        offset = end
    return None


def text_result(value, is_error=False):
    images = []

    def replace_images(item, path="$"):
        encoded = png_base64(item)
        if encoded is not None:
            images.append({"type": "image", "data": encoded, "mimeType": "image/png"})
            return f"[MCP image {len(images)} at {path}]"
        if isinstance(item, dict):
            return {
                key: replace_images(child, f"{path}[{json.dumps(key)}]")
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [
                replace_images(child, f"{path}[{index}]")
                for index, child in enumerate(item)
            ]
        return item

    replaced = replace_images(value)
    text = replaced if isinstance(replaced, str) else json.dumps(replaced, indent=2)
    result = {"content": [{"type": "text", "text": text}, *images]}
    if is_error:
        result["isError"] = True
    return result


def assertion_failed(name, value):
    if name == "assert_page":
        return isinstance(value, dict) and value.get("passed") is False
    if name == "browser_batch" and isinstance(value, list):
        return any(
            isinstance(step, dict) and step.get("cmd") == "assert"
            and assertion_failed("assert_page", step.get("result"))
            for step in value
        )
    return False


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
            value = server.call(name, arguments)
            result = text_result(value, is_error=assertion_failed(name, value))
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
