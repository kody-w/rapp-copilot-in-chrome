#!/usr/bin/env python3
"""Focused regressions for account cold-start and thread render failures."""

import json
import inspect
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
assert gvoice.trusted_voice_url("https://voice.google.com/u/3/messages")
assert not gvoice.trusted_voice_url(
    "https://voice.google.com.attacker.example/u/3/messages"
)
assert not gvoice.trusted_voice_url("http://voice.google.com/u/3/messages")


class SwitchingChrome(ColdChrome):
    def __init__(self):
        super().__init__()
        self.opened = 12
        self.url = "https://voice.google.com/u/3/messages"
        self.account_reads = 0

    def eval(self, tab, expression):
        if "Google Account:" in expression:
            self.account_reads += 1
            if self.account_reads <= 2:
                return "Google Account: Example (expected@example.com)"
            return "Google Account: Other (other@example.net)"
        return True


try:
    gvoice.open_voice(SwitchingChrome())
    raise AssertionError("account switch during navigation must fail")
except SystemExit as exc:
    assert "account changed during navigation" in str(exc)

assert gvoice.canonical_peer_number("5558675309") == "+15558675309"
assert gvoice.canonical_peer_number("+44 20 7183 8750") == "+442071838750"
assert gvoice.account_from_label(
    "Google Account: Example (expected@example.com)"
) == "expected@example.com"
assert not gvoice.account_from_label(
    "Google Account: expected@example.com (attacker@example.net)"
)
for invalid_peer in ("442071838750", "00442071838750", "1234567", "+1234567890123456"):
    try:
        gvoice.canonical_peer_number(invalid_peer)
        raise AssertionError("unsupported peer representation must be refused")
    except BridgeError:
        pass

gvoice.CONFIG_FILE.write_text(json.dumps({
    "google_voice_account": "expected@example.com",
    "google_voice_url": "https://voice.google.com.attacker.example/u/3/messages",
}))
try:
    gvoice.open_voice(ColdChrome())
    raise AssertionError("attacker-controlled Voice origin must be refused")
except BridgeError as exc:
    assert "exact https://voice.google.com origin" in str(exc)
gvoice.CONFIG_FILE.write_text(
    json.dumps(
        {
            "google_voice_account": "expected@example.com",
            "google_voice_url": "https://voice.google.com/u/3/messages",
        }
    )
)


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


class SendChrome:
    def __init__(self, race=False):
        self.url = "https://voice.google.com/u/3/messages"
        self.race = race
        self.count_reads = 0
        self.scripts = []

    def tabs(self):
        return [{"tabId": 11, "url": self.url}]

    def navigate(self, tab, url, timeout=0):
        self.url = url

    def eval(self, tab, expression):
        self.scripts.append(expression)
        if (
            "data-rapp-navigation-marker" in expression
            and "setAttribute" in expression
        ):
            return True
        if "if (document.querySelector('gv-message-item'))" in expression:
            return True
        if "no message input appeared" in expression:
            return {"ok": True}
        if "send button never became enabled" in expression:
            if self.race:
                return {
                    "ok": False,
                    "why": "account or thread changed before send",
                }
            return {"ok": True}
        if "const count = [...document.querySelectorAll(" in expression:
            self.count_reads += 1
            return {"ok": True, "count": self.count_reads - 1}
        raise AssertionError("unexpected JavaScript operation")


send_chrome = SendChrome()
assert gvoice.send(
    send_chrome,
    11,
    "5558675309",
    "atomic context test",
) == {"sent": True, "verified": True}
assert any("account === \"expected@example.com\"" in value for value in send_chrome.scripts)
assert any("itemIds[0] === \"t.+15558675309\"" in value for value in send_chrome.scripts)

try:
    gvoice.send(
        SendChrome(race=True),
        11,
        "5558675309",
        "must not click",
    )
    raise AssertionError("send-time thread race must fail")
except SystemExit as exc:
    assert "account or thread changed before send" in str(exc)


class SnapshotChrome:
    def __init__(self, ok=True):
        self.ok = ok
        self.expression = ""

    def eval(self, tab, expression):
        self.expression = expression
        if not self.ok:
            return {
                "ok": False,
                "why": "account, URL, or rendered peer did not match",
            }
        return {
            "ok": True,
            "items": [{
                "direction": "inbound",
                "from": "",
                "body": "locked snapshot",
                "identity": "Message from , locked snapshot",
                "occurrence": 1,
                "raw": "locked snapshot",
            }],
        }


snapshot = SnapshotChrome()
assert gvoice.messages_locked(
    snapshot,
    11,
    "5558675309",
    "expected@example.com",
)[0]["body"] == "locked snapshot"
assert "gv-thread-details-header h2" in snapshot.expression
assert "emails.length === 1" in snapshot.expression
assert "itemIds.length === 1" in snapshot.expression
try:
    gvoice.messages_locked(
        SnapshotChrome(ok=False),
        11,
        "5558675309",
        "expected@example.com",
    )
    raise AssertionError("stale or wrong-account snapshot must fail")
except BridgeError as exc:
    assert "rendered peer did not match" in str(exc)

send_source = inspect.getsource(gvoice.send)
assert "normalize(n.innerText).includes(normalize(" in send_source
assert send_source.count("itemIds[0] ===") >= 4
assert send_source.count("account ===") >= 4
assert send_source.count("gv-thread-details-header h2") >= 4
assert send_source.index(
    "if (!box.isConnected || !validContext())"
) < send_source.index("setter.call")
assert send_source.rindex(
    "if (!validContext())"
) < send_source.index("button.click()")
assert "a configured Google Voice account is required to send" in send_source
open_source = inspect.getsource(gvoice.open_thread)
assert "data-rapp-navigation-marker" in open_source
assert "previous_marker=marker" in open_source

print("Google Voice: cold-start + stale-thread regressions passed")
