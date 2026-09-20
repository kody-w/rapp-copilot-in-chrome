#!/usr/bin/env python3
"""Protocol smoke test for the vendorless MCP server."""

import base64
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
from rappter_chrome_mcp import batch_step
from bridge import BridgeError, Chrome
from rappter_chrome_mcp import Server, TOOLS, handle, text_result


def png_url(red=0):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0])))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


PNG = png_url()
SECOND_PNG = png_url(255)
IDENTITY = [{
    "name": "local Chromium",
    "instanceId": "test-instance-7",
    "profileName": "Named test profile",
    "connected": True,
}]


class FakeChrome(Chrome):
    """Run the real verb helpers, replacing only the connection and wire call."""

    def __init__(self, **kwargs):
        super().__init__(instance="", **kwargs)
        self.calls = []
        self.closed = False
        self.responses = {
            "browser_info": IDENTITY,
            "screenshot": PNG,
            "assert": {"passed": True, "condition": "visible", "waitedMs": 0},
        }

    def call(self, command, **args):
        self.calls.append((command, args))
        if command == "batch":
            results = []
            for step in args["actions"]:
                result = self.call(step["cmd"], **step["args"])
                results.append({"cmd": step["cmd"], "result": result})
                if step["cmd"] == "assert" and result.get("passed") is False:
                    break
            return results
        result = self.responses.get(command, {"command": command, "args": args})
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def tool_call(chrome, name, args):
    server = Server()
    server.chrome = chrome
    response = handle(server, {
        "jsonrpc": "2.0",
        "id": 77,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 77
    return response["result"]


def result_value(result):
    return json.loads(result["content"][0]["text"])


expected_names = {
    "tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate",
    "get_page_text", "read_page", "form_input", "computer", "javascript_tool",
    "browser_batch", "list_connected_browsers", "read_page_ax", "find_elements",
    "wait_for", "assert_page", "input", "observe_start", "observe_read", "observe_stop",
}
schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
assert set(schemas) == expected_names and len(TOOLS) == 19
assert schemas["read_page_ax"]["properties"]["limit"] == {
    "type": "integer", "minimum": 1, "maximum": 5000, "default": 1000,
}
assert schemas["find_elements"]["properties"]["limit"]["default"] == 40
assert schemas["find_elements"]["properties"]["limit"]["maximum"] == 5000
assert len(schemas["find_elements"]["anyOf"]) == 3
assert schemas["wait_for"]["properties"]["timeout"]["default"] == 15000
assert schemas["wait_for"]["properties"]["timeout"]["maximum"] == 30000
assert schemas["assert_page"]["properties"]["timeout"]["default"] == 0
assert schemas["assert_page"]["properties"]["timeout"]["maximum"] == 30000
assert schemas["assert_page"]["properties"]["condition"]["enum"] == [
    "visible", "enabled", "text-contains", "url-matches",
]
assert len(schemas["assert_page"]["allOf"]) == 3
assert schemas["computer"]["properties"]["scale"]["maximum"] == 2
for name in ("read_page_ax", "find_elements", "wait_for", "assert_page", "computer"):
    assert schemas[name]["properties"]["tabId"]["minimum"] == 0
assert schemas["computer"]["properties"]["region"]["required"] == [
    "x", "y", "width", "height",
]
for tool in TOOLS:
    if tool["name"] in ("read_page_ax", "find_elements"):
        assert "invalidated by navigation or reload" in tool["description"]
        assert "StaticText" in tool["description"]
        assert "accessible labels alone are not text" in tool["description"]

for name in ("input", "observe_start", "observe_read", "observe_stop", "form_input"):
    assert schemas[name]["additionalProperties"] is False
    assert schemas[name]["properties"]["tabId"] == {
        "type": "integer", "minimum": 0, "maximum": 9007199254740991,
    }
assert schemas["input"]["required"] == ["tabId", "action"]
assert schemas["input"]["properties"]["action"]["enum"] == [
    "click", "hover", "scroll", "type", "key",
]
assert schemas["computer"]["properties"]["action"]["enum"] == [
    "click", "type", "activate", "screenshot",
]
for key in ("x", "y"):
    assert schemas["input"]["properties"][key] == {
        "type": "number", "minimum": 0, "maximum": 1000000,
    }
for key in ("deltaX", "deltaY"):
    assert schemas["input"]["properties"][key] == {
        "type": "number", "minimum": -1000000, "maximum": 1000000, "default": 0,
    }
assert schemas["input"]["properties"]["modifiers"]["uniqueItems"] is True
assert schemas["input"]["properties"]["clickCount"] == {
    "type": "integer", "minimum": 1, "maximum": 3, "default": 1,
}
assert schemas["input"]["properties"]["text"]["maxLength"] == 1048576
assert schemas["input"]["properties"]["index"]["default"] == 0
assert len(schemas["input"]["allOf"]) == 7
for name in ("observe_start", "observe_read", "observe_stop"):
    assert schemas[name]["required"] == ["tabId", "kind"]
    assert schemas[name]["properties"]["kind"]["enum"] == ["console", "network"]
    description = next(tool["description"] for tool in TOOLS if tool["name"] == name)
    for phrase in ("Opt-in capture", "no historical data guarantees", "omits headers and bodies",
                   "sensitive data", "MCP caller", "bounded in worker memory", "navigation",
                   "stop", "detach", "worker eviction", "holds the debugger"):
        assert phrase in description
for key, minimum, maximum, default in (
    ("maxEvents", 1, 5000, 500),
    ("maxBytes", 1024, 8388608, 1048576),
    ("maxBodyBytes", 1, 1048576, 65536),
):
    assert schemas["observe_start"]["properties"][key] == {
        "type": "integer", "minimum": minimum, "maximum": maximum, "default": default,
    }
for key in ("includeHeaders", "includeBodies"):
    assert schemas["observe_start"]["properties"][key] == {"type": "boolean", "default": False}
assert schemas["observe_read"]["properties"]["clear"] == {"type": "boolean", "default": False}
assert set(schemas["observe_stop"]["properties"]) == {"tabId", "kind"}
assert schemas["form_input"]["properties"]["value"]["oneOf"] == [
    {"type": "string"}, {"type": "boolean"}, {"type": "number"},
    {"type": "array", "items": {"type": "string"}},
]
assert schemas["form_input"]["properties"]["by"] == {
    "type": "string", "enum": ["value", "label"], "default": "value",
}

translations = [
    ("read_page_ax", {"tabId": 0}, "read_page_ax",
     {"tabId": 0, "limit": 1000, "includeIgnored": False}),
    ("read_page_ax", {"tabId": 7}, "read_page_ax",
     {"tabId": 7, "limit": 1000, "includeIgnored": False}),
    ("read_page_ax", {"tabId": 7, "limit": 5000, "includeIgnored": True},
     "read_page_ax", {"tabId": 7, "limit": 5000, "includeIgnored": True}),
    ("find_elements", {"tabId": 7, "role": "BUTTON"}, "find",
     {"tabId": 7, "role": "BUTTON", "exact": False, "limit": 40}),
    ("find_elements", {"tabId": 7, "role": "link", "name": "Docs",
                       "text": "Read", "exact": True, "limit": 5000}, "find",
     {"tabId": 7, "role": "link", "name": "Docs",
      "text": "Read", "exact": True, "limit": 5000}),
    ("wait_for", {"tabId": 7, "selector": "#ready"}, "waitfor",
     {"tabId": 7, "selector": "#ready", "timeout": 15000}),
    ("wait_for", {"tabId": 7, "selector": "#ready", "timeout": 30000}, "waitfor",
     {"tabId": 7, "selector": "#ready", "timeout": 30000}),
    ("assert_page", {"tabId": 7, "condition": "visible", "selector": "button"},
     "assert", {"tabId": 7, "condition": "visible", "selector": "button", "timeout": 0}),
    ("assert_page", {"tabId": 7, "condition": "enabled", "selector": "button",
                     "timeout": 30000}, "assert",
     {"tabId": 7, "condition": "enabled", "selector": "button", "timeout": 30000}),
    ("assert_page", {"tabId": 7, "condition": "text-contains", "selector": "main",
                     "text": "Ready"}, "assert",
     {"tabId": 7, "condition": "text-contains", "selector": "main",
      "text": "Ready", "timeout": 0}),
    ("assert_page", {"tabId": 7, "condition": "url-matches",
                     "pattern": r"^https://example\.com/(?<section>.*)$"}, "assert",
     {"tabId": 7, "condition": "url-matches",
      "pattern": r"^https://example\.com/(?<section>.*)$", "timeout": 0}),
    ("list_connected_browsers", {}, "browser_info", {}),
    ("input", {"tabId": 0, "action": "click", "x": 0, "y": 1000000}, "input",
     {"tabId": 0, "action": "click", "x": 0, "y": 1000000, "button": "left", "clickCount": 1}),
    ("input", {"tabId": 7, "action": "click", "selector": "button", "button": "middle",
               "clickCount": 3, "modifiers": ["ctrl", "cmd", "shift", "alt"]}, "input",
     {"tabId": 7, "action": "click", "selector": "button", "index": 0, "button": "middle",
      "clickCount": 3, "modifiers": ["ctrl", "cmd", "shift", "alt"]}),
    ("input", {"tabId": 7, "action": "click", "ref": "ax:7:document:d:101",
               "button": "right", "clickCount": 2}, "input",
     {"tabId": 7, "action": "click", "ref": "ax:7:document:d:101",
      "button": "right", "clickCount": 2}),
    ("input", {"tabId": 7, "action": "hover", "selector": "a", "index": 2}, "input",
     {"tabId": 7, "action": "hover", "selector": "a", "index": 2}),
    ("input", {"tabId": 7, "action": "hover", "x": 1.5, "y": 2, "modifiers": []}, "input",
     {"tabId": 7, "action": "hover", "x": 1.5, "y": 2, "modifiers": []}),
    ("input", {"tabId": 7, "action": "scroll", "x": 0, "y": 0, "deltaY": -1000000}, "input",
     {"tabId": 7, "action": "scroll", "x": 0, "y": 0, "deltaX": 0, "deltaY": -1000000}),
    ("input", {"tabId": 7, "action": "scroll", "selector": "main", "deltaX": 1000000,
               "deltaY": 0.5, "modifiers": ["shift"]}, "input",
     {"tabId": 7, "action": "scroll", "selector": "main", "index": 0, "deltaX": 1000000,
      "deltaY": 0.5, "modifiers": ["shift"]}),
    ("input", {"tabId": 7, "action": "type", "text": ""}, "input",
     {"tabId": 7, "action": "type", "text": ""}),
    ("input", {"tabId": 7, "action": "type", "selector": "textarea", "text": "café 🙂"}, "input",
     {"tabId": 7, "action": "type", "selector": "textarea", "index": 0, "text": "café 🙂"}),
    ("input", {"tabId": 7, "action": "key", "key": "Enter", "modifiers": ["ctrl"]}, "input",
     {"tabId": 7, "action": "key", "key": "Enter", "modifiers": ["ctrl"]}),
    ("input", {"tabId": 7, "action": "key", "key": " ", "ref": "ax:7:doc:d:12"}, "input",
     {"tabId": 7, "action": "key", "key": " ", "ref": "ax:7:doc:d:12"}),
    ("observe_start", {"tabId": 7, "kind": "console"}, "observe_start",
     {"tabId": 7, "kind": "console", "maxEvents": 500, "maxBytes": 1048576}),
    ("observe_start", {"tabId": 7, "kind": "network"}, "observe_start",
     {"tabId": 7, "kind": "network", "maxEvents": 500, "maxBytes": 1048576,
      "includeHeaders": False, "includeBodies": False}),
    ("observe_start", {"tabId": 7, "kind": "network", "maxEvents": 1, "maxBytes": 1024,
                       "includeHeaders": True, "includeBodies": True}, "observe_start",
     {"tabId": 7, "kind": "network", "maxEvents": 1, "maxBytes": 1024,
      "includeHeaders": True, "includeBodies": True, "maxBodyBytes": 65536}),
    ("observe_start", {"tabId": 7, "kind": "network", "maxEvents": 5000, "maxBytes": 8388608,
                       "includeBodies": True, "maxBodyBytes": 1048576}, "observe_start",
     {"tabId": 7, "kind": "network", "maxEvents": 5000, "maxBytes": 8388608,
      "includeHeaders": False, "includeBodies": True, "maxBodyBytes": 1048576}),
    ("observe_read", {"tabId": 7, "kind": "console"}, "observe_read",
     {"tabId": 7, "kind": "console", "clear": False}),
    ("observe_read", {"tabId": 7, "kind": "network", "clear": True}, "observe_read",
     {"tabId": 7, "kind": "network", "clear": True}),
    ("observe_stop", {"tabId": 7, "kind": "console"}, "observe_stop",
     {"tabId": 7, "kind": "console"}),
    ("observe_stop", {"tabId": 7, "kind": "network"}, "observe_stop",
     {"tabId": 7, "kind": "network"}),
    ("form_input", {"tabId": 7, "selector": "input", "value": "hello"}, "type",
     {"tabId": 7, "selector": "input", "text": "hello", "submit": False}),
    ("form_input", {"tabId": 7, "selector": "input", "value": False, "index": 0}, "type",
     {"tabId": 7, "selector": "input", "text": False, "submit": False, "index": 0}),
    ("form_input", {"tabId": 7, "selector": "input", "value": True, "submit": True}, "type",
     {"tabId": 7, "selector": "input", "text": True, "submit": True}),
    ("form_input", {"tabId": 7, "selector": "input", "value": 12.5}, "type",
     {"tabId": 7, "selector": "input", "text": 12.5, "submit": False}),
    ("form_input", {"tabId": 7, "selector": "select", "value": ["One", "Two"],
                    "index": 2, "by": "label", "submit": True}, "type",
     {"tabId": 7, "selector": "select", "text": ["One", "Two"],
      "index": 2, "by": "label", "submit": True}),
    ("form_input", {"tabId": 7, "selector": "select", "value": [], "by": "value"}, "type",
     {"tabId": 7, "selector": "select", "text": [], "by": "value", "submit": False}),
]
for name, args, command, expected in list(translations):
    if name in ("input", "observe_start", "observe_read", "observe_stop", "form_input"):
        translations.append((name, {**args, "tabId": 9007199254740991}, command,
                             {**expected, "tabId": 9007199254740991}))
for name, args, command, expected in translations:
    assert batch_step(name, args) == {"cmd": command, "args": expected}
    fake = FakeChrome()
    response = tool_call(fake, name, args)
    assert not response.get("isError"), response
    assert fake.calls == [(command, expected)]
    standalone_value = result_value(response)
    batch_response = tool_call(fake, "browser_batch", {
        "actions": [{"name": name, "input": args}],
    })
    assert not batch_response.get("isError"), batch_response
    assert result_value(batch_response) == [{"cmd": command, "result": standalone_value}]

for text in ("a" * 1048576, "é" * 524288, "🙂" * 262144):
    args = {"tabId": 7, "action": "type", "text": text}
    assert batch_step("input", args) == {"cmd": "input", "args": args}


class VerbTrackingChrome(FakeChrome):
    def __init__(self):
        super().__init__()
        self.verbs = []

    def input(self, *args, **kwargs):
        self.verbs.append("input")
        return super().input(*args, **kwargs)

    def observe_start(self, *args, **kwargs):
        self.verbs.append("observe_start")
        return super().observe_start(*args, **kwargs)

    def observe_read(self, *args, **kwargs):
        self.verbs.append("observe_read")
        return super().observe_read(*args, **kwargs)

    def observe_stop(self, *args, **kwargs):
        self.verbs.append("observe_stop")
        return super().observe_stop(*args, **kwargs)

    def form_input(self, *args, **kwargs):
        self.verbs.append("form_input")
        return super().form_input(*args, **kwargs)

    def type(self, *args, **kwargs):
        self.verbs.append("type")
        return super().type(*args, **kwargs)


for name, args, _, _ in translations:
    if name not in ("input", "observe_start", "observe_read", "observe_stop", "form_input"):
        continue
    fake = VerbTrackingChrome()
    assert not tool_call(fake, name, args).get("isError")
    assert fake.verbs == (["form_input", "type"] if name == "form_input" else [name])

fake = VerbTrackingChrome()
for name, args in [
    ("computer", {"tabId": 7, "action": "type", "selector": "input"}),
    ("computer", {"tabId": 7, "action": "type", "selector": "input", "text": "legacy",
                  "submit": True}),
]:
    expected = {"tabId": 7, "selector": "input", "text": args.get("text", ""),
                "submit": args.get("submit", False)}
    assert batch_step(name, args) == {"cmd": "type", "args": expected}
    assert not tool_call(fake, name, args).get("isError")
    assert fake.calls[-1] == ("type", expected)
assert fake.verbs == ["type", "type"]

region = {"x": 0, "y": 25.5, "width": 400, "height": 200}
for options in ({}, {"fullPage": True}, {"fullPage": False, "scale": 2},
                {"region": region, "scale": 0.5},
                {"fullPage": False, "region": region}):
    args = {"tabId": 7, "action": "screenshot", **options}
    assert batch_step("computer", args) == {
        "cmd": "screenshot", "args": {"tabId": 7, **options},
    }
    fake = FakeChrome()
    response = tool_call(fake, "computer", args)
    assert not response.get("isError"), response
    assert fake.calls == [("screenshot", {"tabId": 7, **options})]
    assert response["content"][1] == {
        "type": "image", "data": PNG.split(",", 1)[1], "mimeType": "image/png",
    }
    assert response["content"][0]["text"] == "[MCP image 1 at $]"


class LegacyScreenshot:
    def screenshot(self, tab):
        assert tab == 7
        return PNG


assert tool_call(LegacyScreenshot(), "computer", {
    "tabId": 7, "action": "screenshot",
})["content"][1]["type"] == "image"

fake = FakeChrome()
fake.browser_info()
fake.read_page_ax(7)
fake.find_elements(7, name="Go", exact=True)
fake.assert_page(7, "text-contains", selector="main", text="", timeout=10)
fake.waitfor(7, "#ready")
fake.screenshot(7)
fake.screenshot(7, full_page=False, region=region, scale=1.25)
assert fake.calls == [
    ("browser_info", {}),
    ("read_page_ax", {"tabId": 7, "limit": 1000, "includeIgnored": False}),
    ("find", {"tabId": 7, "name": "Go", "exact": True, "limit": 40}),
    ("assert", {"tabId": 7, "condition": "text-contains", "selector": "main",
                "text": "", "timeout": 10}),
    ("waitfor", {"tabId": 7, "selector": "#ready", "timeout": 15000}),
    ("screenshot", {"tabId": 7}),
    ("screenshot", {"tabId": 7, "fullPage": False, "region": region, "scale": 1.25}),
]

fake = FakeChrome()
fake.input(7, "click", selector="button", index=1, clickCount=2, modifiers=["shift"])
fake.input(7, "scroll", ref="ax:7:doc:d:1", deltaY=10)
fake.observe_start(7, "console")
fake.observe_start(7, "network", max_events=12, max_bytes=2048,
                   include_headers=False, include_bodies=True, max_body_bytes=1024)
fake.observe_read(7, "console")
fake.observe_read(7, "network", clear=True)
fake.observe_stop(7, "console")
fake.type(7, "input", "legacy")
fake.type(7, "select", ["a"], True, index=2, by="value")
fake.form_input(7, "input", False)
fake.form_input(7, "select", ["A"], index=0, by="label")
assert fake.calls == [
    ("input", {"tabId": 7, "action": "click", "selector": "button", "index": 1,
               "clickCount": 2, "modifiers": ["shift"]}),
    ("input", {"tabId": 7, "action": "scroll", "ref": "ax:7:doc:d:1", "deltaY": 10}),
    ("observe_start", {"tabId": 7, "kind": "console", "maxEvents": 500, "maxBytes": 1048576}),
    ("observe_start", {"tabId": 7, "kind": "network", "maxEvents": 12, "maxBytes": 2048,
                       "includeHeaders": False, "includeBodies": True, "maxBodyBytes": 1024}),
    ("observe_read", {"tabId": 7, "kind": "console", "clear": False}),
    ("observe_read", {"tabId": 7, "kind": "network", "clear": True}),
    ("observe_stop", {"tabId": 7, "kind": "console"}),
    ("type", {"tabId": 7, "selector": "input", "text": "legacy", "submit": False}),
    ("type", {"tabId": 7, "selector": "select", "text": ["a"], "submit": True,
              "index": 2, "by": "value"}),
    ("type", {"tabId": 7, "selector": "input", "text": False, "submit": False}),
    ("type", {"tabId": 7, "selector": "select", "text": ["A"], "submit": False,
              "index": 0, "by": "label"}),
]


def no_connection():
    raise AssertionError("invalid arguments must fail before connecting")


invalid_calls = [
    ("read_page_ax", {}),
    ("read_page_ax", {"tabId": True}),
    ("read_page_ax", {"tabId": -1}),
    ("find_elements", {"tabId": -1, "role": "button"}),
    ("wait_for", {"tabId": -1, "selector": "main"}),
    ("assert_page", {"tabId": -1, "condition": "visible", "selector": "main"}),
    ("computer", {"tabId": -1, "action": "screenshot"}),
    ("read_page_ax", {"tabId": 7, "includeIgnored": 1}),
    ("find_elements", {"tabId": 7}),
    ("find_elements", {"tabId": 7, "role": " \n"}),
    ("find_elements", {"tabId": 7, "role": "button", "name": ""}),
    ("find_elements", {"tabId": 7, "role": " ", "name": "Docs", "text": ""}),
    ("find_elements", {"tabId": 7, "name": None}),
    ("find_elements", {"tabId": 7, "text": "Go", "exact": "yes"}),
    ("wait_for", {"tabId": 7}),
    ("wait_for", {"tabId": 7, "selector": " "}),
    ("assert_page", {"tabId": 7, "condition": "unknown"}),
    ("assert_page", {"tabId": 7, "condition": "visible"}),
    ("assert_page", {"tabId": 7, "condition": "text-contains", "selector": "main"}),
    ("assert_page", {"tabId": 7, "condition": "url-matches"}),
    ("assert_page", {"tabId": 7, "condition": "url-matches", "pattern": []}),
]
for value in (0, -1, 5001, 1.5, True, "2", None):
    invalid_calls.extend([
        ("read_page_ax", {"tabId": 7, "limit": value}),
        ("find_elements", {"tabId": 7, "text": "Go", "limit": value}),
    ])
for value in (-1, 30001, 1.5, True, "2", None):
    invalid_calls.extend([
        ("wait_for", {"tabId": 7, "selector": "main", "timeout": value}),
        ("assert_page", {"tabId": 7, "condition": "visible",
                         "selector": "main", "timeout": value}),
    ])
invalid_calls.append(("wait_for", {"tabId": 7, "selector": "main", "timeout": 0}))
for options in (
    {"fullPage": "yes"}, {"region": None}, {"region": {}},
    {"fullPage": True, "region": region},
    {"region": {**region, "x": -1}}, {"region": {**region, "y": float("nan")}},
    {"region": {**region, "width": 0}}, {"region": {**region, "height": True}},
    {"region": {**region, "x": 10 ** 400}},
    {"region": {**region, "other": 1}},
    *({"scale": value} for value in (0, -1, 2.1, True, "1", None, float("inf"), 10 ** 400)),
):
    invalid_calls.append(("computer", {"tabId": 7, "action": "screenshot", **options}))
new_valid_calls = [
    ("input", {"tabId": 7, "action": "click", "x": 0, "y": 0}),
    ("observe_start", {"tabId": 7, "kind": "console"}),
    ("observe_read", {"tabId": 7, "kind": "network"}),
    ("observe_stop", {"tabId": 7, "kind": "console"}),
    ("form_input", {"tabId": 7, "selector": "input", "value": "text"}),
]
for name, args in new_valid_calls:
    invalid_calls.append((name, {**args, "unknown": False}))
    for value in (None, True, -1, 0.5, "7", 9007199254740992, float("inf"), 10 ** 400):
        invalid_calls.append((name, {**args, "tabId": value}))
    for key in schemas[name]["required"]:
        invalid_calls.append((name, {k: v for k, v in args.items() if k != key}))
for options in (
    {"action": "screenshot"}, {"action": True},
    {"x": None}, {"y": True}, {"x": -1}, {"y": 1000001}, {"x": float("nan")},
    {"y": float("inf")}, {"x": 10 ** 400},
    {"selector": "button"}, {"ref": "ax:7:doc:d:1"}, {"index": 0},
    {"button": "back"}, {"button": True}, {"button": []},
    *({"clickCount": value} for value in (0, 4, True, 1.5, "2", None)),
    *({"modifiers": value} for value in
      (None, "ctrl", ["ctrl", "ctrl"], ["CTRL"], ["meta"], [1], [{}])),
    {"text": ""}, {"key": "Enter"}, {"deltaX": 0}, {"submit": False},
):
    invalid_calls.append(("input", {"tabId": 7, "action": "click", "x": 0, "y": 0, **options}))
for action in ("click", "hover", "scroll"):
    for target in ({}, {"x": 1}, {"y": 1}, {"index": 0},
                   {"selector": ""}, {"selector": " "}, {"ref": None}, {"ref": ""},
                   {"selector": "button", "ref": "ax:7:doc:d:1"}):
        invalid_calls.append(("input", {"tabId": 7, "action": action,
                                       **({"deltaY": 1} if action == "scroll" else {}), **target}))
for value in (-1, True, 1.5, "0", None, 9007199254740992):
    invalid_calls.extend([
        ("input", {"tabId": 7, "action": "hover", "selector": "a", "index": value}),
        ("form_input", {"tabId": 7, "selector": "input", "value": "", "index": value}),
    ])
for action, payload in (
    ("hover", {"button": "left"}), ("hover", {"clickCount": 1}),
    ("hover", {"deltaY": 0}), ("hover", {"text": ""}), ("hover", {"key": "Enter"}),
    ("scroll", {}), ("scroll", {"deltaX": 0, "deltaY": 0}),
    ("scroll", {"deltaY": 1, "button": "left"}),
    ("scroll", {"deltaY": 1, "clickCount": 1}),
    ("scroll", {"deltaY": 1, "text": ""}), ("scroll", {"deltaY": 1, "key": "Enter"}),
    ("type", {}), ("type", {"text": None}), ("type", {"text": True}),
    ("type", {"text": "", "modifiers": []}), ("type", {"text": "", "key": "Enter"}),
    ("type", {"text": "", "button": "left"}), ("type", {"text": "", "deltaX": 0}),
    ("type", {"text": "", "submit": False}),
    ("key", {}), ("key", {"key": ""}), ("key", {"key": None}),
    ("key", {"key": "Enter", "text": ""}), ("key", {"key": "Enter", "button": "left"}),
    ("key", {"key": "Enter", "deltaX": 0}), ("key", {"key": "Enter", "clickCount": 1}),
):
    invalid_calls.append(("input", {"tabId": 7, "action": action, "selector": "main", **payload}))
for text in ("a" * 1048577, "é" * 524289, "🙂" * 262145, "\ud800"):
    invalid_calls.append(("input", {"tabId": 7, "action": "type", "text": text}))
for key in ("deltaX", "deltaY"):
    for value in (None, True, "1", -1000001, 1000001, float("nan"), float("inf"), 10 ** 400):
        invalid_calls.append(("input", {"tabId": 7, "action": "scroll", "x": 0, "y": 0,
                                       "deltaX": 1, "deltaY": 1, key: value}))
for name in ("observe_start", "observe_read", "observe_stop"):
    for kind in ("all", "Network", "", None, 1):
        invalid_calls.append((name, {"tabId": 7, "kind": kind}))
for key, values in (
    ("maxEvents", (0, 5001, 1.5, True, "1", None)),
    ("maxBytes", (1023, 8388609, 1024.5, True, "1024", None)),
    ("maxBodyBytes", (0, 1048577, 1.5, True, "1", None)),
):
    for value in values:
        invalid_calls.append(("observe_start", {"tabId": 7, "kind": "network",
                                               "includeBodies": True, key: value}))
for key in ("includeHeaders", "includeBodies"):
    for value in (None, 1, "false", []):
        invalid_calls.append(("observe_start", {"tabId": 7, "kind": "network", key: value}))
    for value in (False, True):
        invalid_calls.append(("observe_start", {"tabId": 7, "kind": "console", key: value}))
for options in ({"maxBodyBytes": 1}, {"includeBodies": False, "maxBodyBytes": 1}):
    invalid_calls.append(("observe_start", {"tabId": 7, "kind": "network", **options}))
for name, options in (
    ("observe_start", {"maxBodyBytes": 1}), ("observe_start", {"clear": False}),
    ("observe_read", {"maxEvents": 1}), ("observe_read", {"includeHeaders": False}),
    ("observe_stop", {"clear": False}), ("observe_stop", {"maxBytes": 1024}),
    ("observe_stop", {"includeBodies": False}),
):
    invalid_calls.append((name, {"tabId": 7, "kind": "console", **options}))
for clear in (None, 0, "false", []):
    invalid_calls.append(("observe_read", {"tabId": 7, "kind": "network", "clear": clear}))
for options in (
    {"selector": ""}, {"selector": " "}, {"selector": None}, {"selector": []},
    *({"value": value} for value in (None, {}, [1], [True], ["a", None],
                                    float("nan"), float("inf"), 10 ** 400)),
    *({"by": value} for value in ("text", "LABEL", None, 1, [])),
    *({"submit": value} for value in (None, 1, "false")),
    {"text": "other"}, {"checked": False},
):
    invalid_calls.append(("form_input", {"tabId": 7, "selector": "input", "value": "", **options}))
for name, args in invalid_calls:
    server = Server()
    server.connection = no_connection
    for tool_name, tool_args in (
        (name, args),
        ("browser_batch", {"actions": [
            {"name": "get_page_text", "input": {"tabId": 7}},
            {"name": name, "input": args},
        ]}),
    ):
        try:
            server.call(tool_name, tool_args)
            raise AssertionError(f"invalid arguments accepted: {name} {args}")
        except BridgeError:
            pass
        response = handle(server, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": tool_args},
        })
        assert response["result"]["isError"] is True
        assert "Internal browser tool error" not in response["result"]["content"][0]["text"]

for name, args in new_valid_calls:
    command = batch_step(name, args)["cmd"]
    for batched in (False, True):
        fake = FakeChrome()
        fake.responses[command] = BridgeError(f"{command}: native operation failed")
        response = tool_call(fake, "browser_batch" if batched else name,
                             {"actions": [{"name": name, "input": args}]} if batched else args)
        assert response == {
            "isError": True,
            "content": [{"type": "text", "text": f"{command}: native operation failed"}],
        }
        assert fake.closed
        assert fake.calls[-1][0] == command

for name in ("observe_start", "observe_read", "observe_stop"):
    fake = FakeChrome()
    snapshot = {"tabId": 7, "kind": "network", "events": [{"url": "https://example.test/"}],
                "dropped": 2, "bytes": 120, "passed": False}
    fake.responses[name] = snapshot
    response = tool_call(fake, name, {"tabId": 7, "kind": "network"})
    assert not response.get("isError")
    assert result_value(response) == snapshot
    assert not fake.closed

fake = FakeChrome()
failed_assertion = {
    "passed": False, "condition": "visible", "selector": "#missing",
    "waitedMs": 0, "actual": {"visible": False},
}
fake.responses["assert"] = failed_assertion
assert_args = {"tabId": 7, "condition": "visible", "selector": "#missing"}
response = tool_call(fake, "assert_page", assert_args)
assert response["isError"] is True
assert result_value(response) == failed_assertion
response = tool_call(fake, "browser_batch", {
    "actions": [
        {"name": "computer", "input": {"tabId": 7, "action": "screenshot"}},
        {"name": "assert_page", "input": assert_args},
        {"name": "computer", "input": {"tabId": 7, "action": "screenshot"}},
    ],
})
assert response["isError"] is True
assert len(result_value(response)) == 2
assert result_value(response)[1]["result"] == failed_assertion
assert response["content"][1]["type"] == "image"
assert len([call for call in fake.calls if call[0] == "screenshot"]) == 1
assert fake.closed is False
response = tool_call(fake, "browser_batch", {
    "actions": [
        {"name": "assert_page", "input": assert_args},
        {"name": "computer", "input": {"tabId": 7, "action": "screenshot"}},
    ],
})
assert response["isError"] is True
assert result_value(response) == [{"cmd": "assert", "result": failed_assertion}]
assert len(response["content"]) == 1
assert len([call for call in fake.calls if call[0] == "screenshot"]) == 1
fake.responses["eval"] = {"passed": False}
assert not tool_call(fake, "javascript_tool", {"tabId": 7, "code": "value"}).get("isError")

fake = FakeChrome()
assert result_value(tool_call(fake, "list_connected_browsers", {})) == IDENTITY
fake.responses["browser_info"] = [{**IDENTITY[0], "connected": False}]
assert result_value(tool_call(fake, "list_connected_browsers", {}))[0]["connected"] is False
fake.responses["browser_info"] = BridgeError("extension closed the connection")
response = tool_call(fake, "list_connected_browsers", {})
assert response["isError"] is True and fake.closed
assert response["content"] == [{"type": "text", "text": "extension closed the connection"}]

disconnected = Server()


def connection_failure():
    raise BridgeError("no authenticated extension dialled in")


disconnected.connection = connection_failure
response = handle(disconnected, {
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "list_connected_browsers", "arguments": {}},
})["result"]
assert response == {
    "isError": True,
    "content": [{"type": "text", "text": "no authenticated extension dialled in"}],
}

fake = FakeChrome()
fake.responses["eval"] = {
    "title": "nested image", "shots": [SECOND_PNG, {"image": PNG, "scale": 1}],
    "ordinary": "keep this",
}
response = tool_call(fake, "browser_batch", {"actions": [
    {"name": "computer", "input": {"tabId": 7, "action": "screenshot"}},
    {"name": "javascript_tool", "input": {"tabId": 7, "code": "snapshots"}},
]})
assert len(response["content"]) == 4
assert [item["data"] for item in response["content"][1:]] == [
    PNG.split(",", 1)[1], SECOND_PNG.split(",", 1)[1], PNG.split(",", 1)[1],
]
replaced = result_value(response)
assert replaced[0] == {
    "cmd": "screenshot", "result": '[MCP image 1 at $[0]["result"]]',
}
assert replaced[1]["result"] == {
    "title": "nested image",
    "shots": ['[MCP image 2 at $[1]["result"]["shots"][0]]', {
        "image": '[MCP image 3 at $[1]["result"]["shots"][1]["image"]]', "scale": 1,
    }],
    "ordinary": "keep this",
}
assert "data:image/png" not in response["content"][0]["text"]
assert fake.responses["eval"]["shots"][0] == SECOND_PNG  # Do not mutate source results.
fake.responses["text"] = "Plain text\nincluding Unicode: café"
assert tool_call(fake, "get_page_text", {"tabId": 7}) == {
    "content": [{"type": "text", "text": fake.responses["text"]}],
}

corrupt_png = bytearray(base64.b64decode(PNG.split(",", 1)[1]))
corrupt_png[20] ^= 1
for value in (
    "ordinary\ntext", {"some": [1, True, None, "text"]}, [],
    "data:image/png;base64,not-base64",
    "data:image/png;base64," + base64.b64encode(b"not a PNG").decode(),
    "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
    "data:image/png;base64," + base64.b64encode(corrupt_png).decode(),
    PNG[:-4], PNG + "!", PNG.replace("image/png", "image/jpeg"),
    "prefix " + PNG,
):
    expected = value if isinstance(value, str) else json.dumps(value, indent=2)
    assert text_result(value) == {"content": [{"type": "text", "text": expected}]}
    assert text_result(value, is_error=True) == {
        "content": [{"type": "text", "text": expected}], "isError": True,
    }
assert text_result(PNG.replace("image/png", "IMAGE/PNG"))["content"][1]["type"] == "image"
assert text_result(PNG.replace("png;", "png;charset=binary;"))["content"][1]["type"] == "image"
percent_url = "data:image/png," + "".join(
    f"%{byte:02X}" for byte in base64.b64decode(PNG.split(",", 1)[1])
)
assert text_result(percent_url)["content"][1]["data"] == PNG.split(",", 1)[1]
print("MCP schemas + standalone/batch verbs + preflight + images + live identity passed")

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
    assert names == expected_names
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
