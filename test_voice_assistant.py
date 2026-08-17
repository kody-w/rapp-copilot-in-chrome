#!/usr/bin/env python3
"""Deterministic safety tests for the Google Voice assistant."""

import json
import pathlib
import tempfile
import threading
from datetime import timedelta

import voice_assistant as assistant

tmp = pathlib.Path(tempfile.mkdtemp(prefix="voice-assistant-test-"))
assistant.STATE_FILE = tmp / "state.json"
assistant.CONFIG_FILE = tmp / "config.json"
assistant.LOG_FILE = tmp / "assistant.log"
assistant.CONFIG_FILE.write_text(
    json.dumps(
        {
            "google_voice_account": "expected@example.com",
            "google_voice_peer": "5558675309",
            "max_replies_per_hour": 2,
        }
    )
)
loaded_config = assistant.config()
assert loaded_config["google_voice_account"] == "expected@example.com"
assert loaded_config["google_voice_peer"] == "+15558675309"
assert loaded_config["google_voice_peer_legacy"] == "5558675309"

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
original_collect = assistant.collect
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
assert len(sent) == 1
assert sent[0].startswith("answer:new question [#")
first_delivery = sent[0]
assert assistant.tick(responder=responder, sender=sender) == 0
assert sent == [first_delivery], "duplicate poll must not duplicate reply"

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
unlabelled_now = {
    "direction": "inbound",
    "from": "5558675309",
    "body": "same message",
    "raw": (
        "10:30 PM\nMessage from , same message, Friday, August 14 2026, "
        "10:30 PM.\nperson\nsame message"
    ),
}
unlabelled_aged = {
    **unlabelled_now,
    "raw": unlabelled_now["raw"].replace("10:30 PM\n", "Aug 14\n", 1),
}
assert assistant.message_id(unlabelled_now) == assistant.message_id(unlabelled_aged)
assert assistant.normalize_number("(555) 867-5309") == "+15558675309"
assert assistant.normalize_number("+1 555 867 5309") == "+15558675309"
assert assistant.normalize_number("+44 555 867 5309") == "+445558675309"
assert not assistant.eligible(
    {
        "direction": "inbound",
        "from": "+44 555 867 5309",
        "body": "wrong country",
    },
    {"google_voice_peer": "+1 555 867 5309"},
)
assert not assistant.eligible(
    {
        "direction": "inbound",
        "from": "442071838750",
        "body": "unsupported bare international number",
    },
    {"google_voice_peer": "442071838750"},
)
assert not assistant.eligible(
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "2FA 123456",
    },
    {"google_voice_peer": "5558675309"},
)
duplicate_one = {**older, "occurrence": 1}
duplicate_two = {**older, "occurrence": 2}
assert assistant.message_id(duplicate_one) != assistant.message_id(duplicate_two)
ledger = assistant.default_state()
first_rows, _ = assistant.assign_message_ids(ledger, [duplicate_one])
two_rows, _ = assistant.assign_message_ids(
    ledger,
    [duplicate_one, duplicate_two],
)
surviving_rows, _ = assistant.assign_message_ids(ledger, [duplicate_two])
assert first_rows[0][0] == two_rows[0][0]
assert two_rows[1][0] != two_rows[0][0]
assert surviving_rows[0][0] == two_rows[1][0]
reexpanded_rows, _ = assistant.assign_message_ids(
    ledger,
    [duplicate_one, duplicate_two],
)
assert reexpanded_rows[0][0] == surviving_rows[0][0]
assert reexpanded_rows[1][0] not in {
    first_rows[0][0],
    two_rows[1][0],
}
assert reexpanded_rows[1][0] in ledger["migration_notices"]
signature = assistant.message_signature(duplicate_one)
assert ledger["message_rows"][signature][:2] == [
    first_rows[0][0],
    two_rows[1][0],
]

mixed_ledger = assistant.default_state()
mixed_rows, _ = assistant.assign_message_ids(
    mixed_ledger,
    [duplicate_one, duplicate_two],
)
mixed_ledger["handled"] = [mixed_rows[0][0]]
mixed_survivor, _ = assistant.assign_message_ids(
    mixed_ledger,
    [duplicate_one],
)
assert mixed_survivor[0][0] not in {
    mixed_rows[0][0],
    mixed_rows[1][0],
}
assert mixed_survivor[0][0] in mixed_ledger["migration_notices"]

canonical_blank = {
    "direction": "inbound",
    "from": "+15558675309",
    "_legacy_from": "5558675309",
    "body": "legacy blank sender",
    "raw": "legacy blank sender",
}
legacy_blank = {**canonical_blank, "from": "5558675309"}
assert assistant.message_id(legacy_blank) in assistant.legacy_message_ids(
    canonical_blank,
    loaded_config,
)

assert "\u202e" not in assistant.safe_text("safe\u202eevil", 100)
assert assistant.outbound_count(
    [{"direction": "outbound", "body": "line one line two"}],
    "line one\nline two",
) == 1
assert assistant.outbound_count(
    [{"direction": "outbound", "body": "same result [#BBBBBB]"}],
    "same result [#AAAAAA]",
) == 0
assert assistant.outbound_count(
    [{"direction": "outbound", "body": "same reply"}],
    "same\nreply",
) == 1
assert assistant.legacy_outbound_count(
    [{"direction": "outbound", "body": "same reply"}],
    "same\nreply",
) == 0

original_chrome = assistant.Chrome
original_open_voice = assistant.gvoice.open_voice


class EmptyChrome:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


assistant.Chrome = EmptyChrome
assistant.gvoice.open_voice = lambda chrome: (
    (_ for _ in ()).throw(SystemExit("ordinary Voice failure"))
)
try:
    for operation in (
        lambda: original_collect({"google_voice_peer": "5558675309"}),
        lambda: assistant.deliver(
            {"google_voice_peer": "5558675309"},
            "test",
        ),
    ):
        try:
            operation()
            raise AssertionError("SystemExit must be contained")
        except RuntimeError as exc:
            assert "ordinary Voice failure" in str(exc)
finally:
    assistant.Chrome = original_chrome
    assistant.gvoice.open_voice = original_open_voice

original_twin_chat = assistant.voice_twin.chat
twin_call = {}
assistant.voice_twin.chat = (
    lambda message_id, text, state, cfg: (
        twin_call.update({"message_id": message_id, "text": text}),
        "twin reply",
    )[1]
)
try:
    assert assistant.respond(
        {
            "direction": "inbound",
            "from": "5558675309",
            "body": "status",
            "raw": "x",
            "_stable_message_id": "d" * 20,
        },
        {},
        {"google_voice_peer": "5558675309"},
    ) == "twin reply"
    assert twin_call == {"message_id": "d" * 20, "text": "status"}
finally:
    assistant.voice_twin.chat = original_twin_chat

# Same-conversation turns are FIFO: failure on the oldest inbound prevents a
# later message or tool action from overtaking it.
assistant.STATE_FILE = tmp / "fifo-state.json"
fifo_messages = [{
    "direction": "inbound",
    "from": "5558675309",
    "body": "existing",
    "raw": "existing",
}]
assistant.collect = lambda cfg: list(fifo_messages)
assistant.tick(responder=responder, sender=sender)
fifo_messages.extend([
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "first",
        "raw": "first",
    },
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "second",
        "raw": "second",
    },
])
fifo_sent = []


def fail_first(item, state, cfg):
    if item["body"] == "first":
        raise RuntimeError("first is unresolved")
    return "second must not overtake"


assert assistant.tick(
    responder=fail_first,
    sender=lambda cfg, text: fifo_sent.append(text),
) == 0
assert fifo_sent == []

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

# Stable-ID migration maps only legacy handled rows; a genuinely new visible
# inbound remains a candidate and is never silently watermarked.
assistant.STATE_FILE = tmp / "stable-migration-state.json"
migration_old = {
    "direction": "inbound",
    "from": "5558675309",
    "body": "legacy handled",
    "label": "Message from 5 5 5, legacy handled, August 16 2026, 1:00 PM.",
    "raw": "legacy handled",
}
migration_new = {
    "direction": "inbound",
    "from": "5558675309",
    "body": "new during upgrade",
    "label": "Message from 5 5 5, new during upgrade, August 16 2026, 1:01 PM.",
    "raw": "new during upgrade",
}
migration_state = assistant.default_state()
migration_state["initialized_at"] = assistant.iso()
migration_state["handled"] = [assistant.message_id(migration_old)]
assistant.save_state(migration_state)
assistant.collect = lambda cfg: [migration_old, migration_new]
migration_sent = []
assert assistant.tick(
    responder=responder,
    sender=lambda cfg, text: migration_sent.append(text),
) == 2
assert len(migration_sent) == 2
assert any("No command was run" in value for value in migration_sent)
assert any(
    value.startswith("answer:new during upgrade [#")
    for value in migration_sent
)

# A pre-ledger pending delivery is mapped to the unique stable row before
# readback recovery, so the inbound cannot become a second candidate.
assistant.STATE_FILE = tmp / "pending-migration-state.json"
pending_inbound = {
    "direction": "inbound",
    "from": "5558675309",
    "body": "legacy pending",
    "label": (
        "Message from 5 5 5, legacy pending, "
        "August 16 2026, 1:02 PM."
    ),
    "raw": "legacy pending",
}
legacy_pending_id = assistant.message_id(pending_inbound)
pending_reply = "already delivered legacy reply"
pending_state = assistant.default_state()
pending_state["initialized_at"] = assistant.iso()
pending_state["pending"] = {
    "message_id": legacy_pending_id,
    "inbound_text": pending_inbound["body"],
    "reply": pending_reply,
    "baseline": 0,
    "created_at": assistant.iso(),
}
assistant.save_state(pending_state)
assistant.collect = lambda cfg: [
    pending_inbound,
    {
        "direction": "outbound",
        "from": "you",
        "body": pending_reply,
        "raw": pending_reply,
    },
]
assert assistant.tick(
    responder=lambda *args: (
        (_ for _ in ()).throw(AssertionError("pending command ran twice"))
    ),
    sender=lambda *args: (
        (_ for _ in ()).throw(AssertionError("pending reply was resent"))
    ),
) == 0
mapped_pending_state = assistant.load_state()
assert mapped_pending_state["pending"] is None
assert legacy_pending_id not in mapped_pending_state["handled"]
assert len(mapped_pending_state["handled"]) == 1

# Legacy pending baselines used exact body equality. Whitespace-normalized
# readback must not falsely finalize such a pending reply.
assistant.STATE_FILE = tmp / "legacy-baseline-state.json"
baseline_inbound = {
    "direction": "inbound",
    "from": "5558675309",
    "body": "legacy baseline",
    "raw": "legacy baseline",
}
baseline_state = assistant.default_state()
baseline_state["initialized_at"] = assistant.iso()
baseline_rows, _ = assistant.assign_message_ids(
    baseline_state,
    [baseline_inbound],
)
baseline_id = baseline_rows[0][0]
baseline_state["message_rows_initialized"] = True
baseline_state["pending"] = {
    "message_id": baseline_id,
    "inbound_text": baseline_inbound["body"],
    "reply": "same\nreply",
    "baseline": 0,
    "created_at": assistant.iso(),
}
assistant.save_state(baseline_state)
assistant.collect = lambda cfg: [
    baseline_inbound,
    {
        "direction": "outbound",
        "from": "you",
        "body": "same reply",
        "raw": "same reply",
    },
]
assert assistant.tick(
    responder=lambda *args: (
        (_ for _ in ()).throw(AssertionError("legacy pending reran"))
    ),
    sender=lambda *args: (
        (_ for _ in ()).throw(AssertionError("legacy pending resent"))
    ),
) == 0
legacy_baseline_state = assistant.load_state()
assert legacy_baseline_state["pending"]["delivery_state"] == "unknown"
assert baseline_id not in legacy_baseline_state["handled"]

# If the legacy pending ID cannot be mapped uniquely, recovery remains blocked
# rather than resending or executing the visible inbound.
assistant.STATE_FILE = tmp / "unmapped-pending-state.json"
missing_pending = {**pending_inbound, "body": "not visible", "raw": "not visible"}
unmapped_state = assistant.default_state()
unmapped_state["initialized_at"] = assistant.iso()
unmapped_state["pending"] = {
    "message_id": assistant.message_id(missing_pending),
    "inbound_text": missing_pending["body"],
    "reply": "must not resend",
    "baseline": 0,
    "created_at": assistant.iso(),
}
assistant.save_state(unmapped_state)
assistant.collect = lambda cfg: [pending_inbound]
for _ in range(2):
    assert assistant.tick(
        responder=lambda *args: (
            (_ for _ in ()).throw(AssertionError("unmapped command ran"))
        ),
        sender=lambda *args: (
            (_ for _ in ()).throw(AssertionError("unmapped reply was sent"))
        ),
    ) == 0
persisted_unmapped = assistant.load_state()
assert persisted_unmapped["pending"]["message_id"] == assistant.message_id(
    missing_pending
)
assert persisted_unmapped["message_rows_initialized"] is False

assistant.STATE_FILE = tmp / "ambiguous-migration-state.json"
ambiguous_state = assistant.default_state()
ambiguous_state["initialized_at"] = assistant.iso()
ambiguous_state["handled"] = [assistant.message_id(duplicate_one)]
ambiguous_state["replies"] = [
    {"at": assistant.iso(), "message_id": "a" * 20},
    {"at": assistant.iso(), "message_id": "b" * 20},
]
assistant.save_state(ambiguous_state)
surviving_duplicate = {**duplicate_two, "occurrence": 1}
assistant.collect = lambda cfg: [surviving_duplicate]
ambiguous_sent = []
assert assistant.tick(
    responder=lambda *args: (
        (_ for _ in ()).throw(AssertionError("ambiguous command executed"))
    ),
    sender=lambda cfg, text: ambiguous_sent.append(text),
) == 0
assert ambiguous_sent == []
persisted_ambiguous = assistant.load_state()
assert len(persisted_ambiguous["migration_notices"]) == 1
persisted_ambiguous["replies"] = []
assistant.save_state(persisted_ambiguous)
assert assistant.tick(
    responder=lambda *args: (
        (_ for _ in ()).throw(AssertionError("ambiguous command executed"))
    ),
    sender=lambda cfg, text: ambiguous_sent.append(text),
) == 1
assert len(ambiguous_sent) == 1
assert "No command was run" in ambiguous_sent[0]
assert "Please resend" in ambiguous_sent[0]
assert assistant.load_state()["migration_notices"] == []

# A send that lands and then crashes must be finalized by readback, not sent
# a second time.
assistant.STATE_FILE = tmp / "crash-state.json"
crash_messages = [
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "existing",
        "raw": "existing",
    }
]
assistant.collect = lambda cfg: list(crash_messages)
assistant.tick(responder=responder, sender=sender)  # watermark
crash_messages.append(
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "crash window",
        "raw": "crash window",
    }
)
deliveries = []
def crash_after_delivery(cfg, text):
    deliveries.append(text)
    crash_messages.append(
        {
            "direction": "outbound",
            "from": "you",
            "body": text,
            "raw": f"outbound {text}",
        }
    )
    raise RuntimeError("simulated SIGKILL window")
assert assistant.tick(
    responder=responder,
    sender=crash_after_delivery,
) == 0
assert assistant.load_state()["pending"] is not None
def must_not_resend(cfg, text):
    raise AssertionError("confirmed pending reply was sent twice")
assert assistant.tick(responder=responder, sender=must_not_resend) == 0
assert len(deliveries) == 1
assert assistant.load_state()["pending"] is None

# Identical replies must reserve against a freshly collected baseline.
assistant.STATE_FILE = tmp / "identical-reply-state.json"
identical_messages = [{
    "direction": "inbound",
    "from": "5558675309",
    "body": "existing",
    "raw": "existing",
}]
assistant.collect = lambda cfg: list(identical_messages)
assistant.tick(responder=responder, sender=sender)
identical_messages.extend([
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "first identical",
        "raw": "first identical",
    },
    {
        "direction": "inbound",
        "from": "5558675309",
        "body": "second identical",
        "raw": "second identical",
    },
])
identical_deliveries = []


def identical_responder(item, state, cfg):
    return "same reply"


def fail_second_identical(cfg, text):
    identical_deliveries.append(text)
    if len(identical_deliveries) == 1:
        identical_messages.append({
            "direction": "outbound",
            "from": "you",
            "body": text,
            "raw": text,
        })
        return
    raise RuntimeError("second send did not land")


assert assistant.tick(
    responder=identical_responder,
    sender=fail_second_identical,
) == 1
identical_pending = assistant.load_state()["pending"]
assert identical_pending["baseline"] == 0
assert identical_pending["delivery_state"] == "unknown"
retry_calls = []


def retry_identical(cfg, text):
    retry_calls.append(text)
    identical_messages.append({
        "direction": "outbound",
        "from": "you",
        "body": text,
        "raw": text,
    })


assistant.tick(responder=identical_responder, sender=retry_identical)
assert retry_calls == [], "an ambiguous send must never be retried automatically"
assert assistant.load_state()["pending"]["delivery_state"] == "unknown"
assert assistant.delivery_text("same", "a" * 20) != assistant.delivery_text(
    "same",
    "b" * 20,
)
assert assistant.delivery_text(
    "same",
    ("a" * 6) + ("b" * 14),
) != assistant.delivery_text(
    "same",
    ("a" * 6) + ("c" * 14),
)
assert len(assistant.delivery_text("x" * 900, "c" * 20)) == 900

# A backup captured before an attempted send is conservatively promoted to
# unknown if the newer primary is lost; it can never resurrect a resendable
# prepared state.
assistant.STATE_FILE = tmp / "backup-ambiguous-state.json"
backup_base = assistant.default_state()
backup_base["initialized_at"] = assistant.iso()
assistant.save_state(backup_base)
backup_prepared = json.loads(json.dumps(backup_base))
backup_prepared["pending"] = {
    "message_id": "d" * 20,
    "inbound_text": "ambiguous backup",
    "reply": "possibly delivered",
    "baseline": 0,
    "created_at": assistant.iso(),
    "delivery_state": "prepared",
    "attempted_at": None,
}
assistant.save_state(backup_prepared)
backup_attempted = json.loads(json.dumps(backup_prepared))
backup_attempted["pending"]["delivery_state"] = "attempted"
backup_attempted["pending"]["attempted_at"] = assistant.iso()
assistant.save_state(backup_attempted)
assistant.STATE_FILE.write_text("{broken", encoding="utf-8")
recovered_ambiguous = assistant.load_state()
assert recovered_ambiguous["pending"]["delivery_state"] == "unknown"
assert json.loads(
    assistant.state_backup_path().read_text(encoding="utf-8")
)["pending"]["delivery_state"] == "unknown"
assistant.STATE_FILE.unlink()
recovered_missing_primary = assistant.load_state()
assert recovered_missing_primary["pending"]["delivery_state"] == "unknown"

# Corruption recovers from a known-good backup and never watermarks silently.
assistant.STATE_FILE = tmp / "recover-state.json"
first = assistant.default_state()
first["initialized_at"] = "first"
assistant.save_state(first)
second = {**first, "initialized_at": "second"}
assistant.save_state(second)
assistant.STATE_FILE.write_text("{broken")
recovered = assistant.load_state()
assert recovered["initialized_at"] == "first"
(assistant.STATE_FILE).unlink()
assistant.state_backup_path().unlink(missing_ok=True)
assistant.STATE_FILE.write_text("{broken")
try:
    assistant.load_state()
    raise AssertionError("corrupt state without backup must fail closed")
except RuntimeError as exc:
    assert "no valid backup" in str(exc)

oversized_pending = assistant.default_state()
oversized_pending["pending"] = {
    "message_id": "a" * 20,
    "inbound_text": "ok",
    "reply": "x" * 901,
    "baseline": 0,
    "created_at": assistant.iso(),
}
assert not assistant.valid_state(oversized_pending)
malformed_handled = assistant.default_state()
malformed_handled["handled"] = [{}]
assert not assistant.valid_state(malformed_handled)

# Future clock artifacts cannot lock the one-hour budget forever.
future = assistant.iso(assistant.now() + timedelta(days=1))
assert assistant.recent_reply_count({"replies": [{"at": future}]}) == 0

# A second tick cannot enter while the durable-state lock is held.
assistant.STATE_FILE = tmp / "lock-state.json"
lock_results = []
with assistant.tick_lock() as acquired:
    assert acquired
    thread = threading.Thread(
        target=lambda: lock_results.append(
            assistant.tick(responder=responder, sender=sender)
        )
    )
    thread.start()
    thread.join(timeout=3)
assert lock_results == [0]

print("voice assistant: 46 safety assertions passed")
