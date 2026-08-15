#!/usr/bin/env python3
"""Focused regressions for account cold-start and thread render failures."""

import json
import pathlib
import tempfile

import gvoice
from bridge import BridgeError

tmp = pathlib.Path(tempfile.mkdtemp(prefix="gvoice-test-"))
gvoice.CONFIG_FILE = tmp / "config.json"
gvoice.CONFIG_FILE.write_text(
    json.dumps(
        {
            "google_voice_account": "expected@example.com",
            "google_voice_url": "https://voice.google.com/u/3/messages",
        }
    )
)


class ColdChrome:
    def __init__(self):
        self.opened = None
        self.url = None

    def tabs(self):
        if self.opened is None:
            return []
        return [{"tabId": self.opened, "url": self.url}]

    def open(self, url, reuse=True):
        self.opened = 7
        self.url = url
        return self.opened

    def waitfor(self, tab, selector, timeout=0):
        return {"found": True}

    def eval(self, tab, expression):
        if "Google Account:" in expression:
            return "Google Account: Example (expected@example.com)"
        return True

    def navigate(self, tab, url, timeout=0):
        self.url = url
        return {"tabId": tab, "url": url}


cold = ColdChrome()
assert gvoice.open_voice(cold) == 7
assert cold.opened == 7
assert cold.url == "https://voice.google.com/u/3/messages"


class StaleChrome:
    def __init__(self):
        self.url = "https://voice.google.com/u/3/messages"

    def tabs(self):
        return [{"tabId": 9, "url": self.url}]

    def navigate(self, tab, url, timeout=0):
        self.url = url

    def eval(self, tab, expression):
        return False


original_sleep = gvoice.time.sleep
gvoice.time.sleep = lambda _: None
try:
    try:
        gvoice.open_thread(StaleChrome(), 9, "5558675309")
        raise AssertionError("stale thread should fail")
    except BridgeError as exc:
        assert "did not render" in str(exc)
finally:
    gvoice.time.sleep = original_sleep

print("Google Voice: cold-start + stale-thread regressions passed")
