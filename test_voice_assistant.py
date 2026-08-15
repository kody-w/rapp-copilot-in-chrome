#!/usr/bin/env python3
"""Deterministic safety tests for the Google Voice assistant."""

import json
import pathlib
import tempfile

import voice_assistant as assistant

tmp = pathlib.Path(tempfile.mkdtemp(prefix="voice-assistant-test-"))
assistant.STATE_FILE = tmp / "state.json"
assistant.CONFIG_FILE = tmp / "config.json"
assistant.CONFIG_FILE.write_text(
    json.dumps(
        {
            "google_voice_account": "expected@example.com",
            "google_voice_peer": "5558675309",
            "max_replies_per_hour": 2,
        }
    )
)

messages = [
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "old",
        "raw": "old inbound",
    },
    {
        "direction": "outbound",
        "from": "you",
        "body": "already answered",
        "raw": "old outbound",
    },
]
assistant.collect = lambda cfg: list(messages)
sent = []


def responder(item, state, cfg):
    return f"answer:{item['body']}"


def sender(cfg, text):
    sent.append(text)


assert assistant.tick(responder=responder, sender=sender) == 0
assert sent == [], "first run must watermark history"

messages.append(
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "new question",
        "raw": "new inbound",
    }
)
assert assistant.tick(responder=responder, sender=sender) == 1
assert sent == ["answer:new question"]
assert assistant.tick(responder=responder, sender=sender) == 0
assert sent == ["answer:new question"], "duplicate poll must not duplicate reply"

messages.append(
    {
        "direction": "inbound",
        "from": "9999999999",
        "body": "wrong person",
        "raw": "wrong sender",
    }
)
assert assistant.tick(responder=responder, sender=sender) == 0
assert len(sent) == 1

messages.append(
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "Your verification code is 123456",
        "raw": "security code",
    }
)
assert assistant.tick(responder=responder, sender=sender) == 0
assert len(sent) == 1

older = {
    "direction": "inbound",
    "from": "5558675309",
    "body": "same message",
    "label": "Message from 5 5 5, same message, Friday, August 14 2026, 10:30 PM.",
    "raw": "10:30 PM\nsame message",
}
aged = {**older, "raw": "Aug 14\nsame message"}
assert assistant.message_id(older) == assistant.message_id(aged)

assistant.STATE_FILE = tmp / "large-state.json"
large_messages = [
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": f"history {index}",
        "label": f"Message from 5 5 5, history {index}, January 1 2020, 1:00 PM.",
        "raw": f"history {index}",
    }
    for index in range(600)
]
assistant.collect = lambda cfg: list(large_messages)
sent.clear()
assert assistant.tick(responder=responder, sender=sender) == 0
assert assistant.tick(responder=responder, sender=sender) == 0
assert sent == [], "all first-run history must remain watermarked"
large_state = json.loads(assistant.STATE_FILE.read_text())
assert len(large_state["handled"]) == 600

print("voice assistant: 13 safety assertions passed")
