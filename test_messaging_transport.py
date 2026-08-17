#!/usr/bin/env python3
"""Universal RAPP Messaging state, trust, and transport isolation tests."""

import json
import pathlib
import tempfile
import threading

import messaging_transport
import universal_messaging
import voice_twin

tmp = pathlib.Path(tempfile.mkdtemp(prefix="universal-messaging-test-"))
voice_twin.ROOT = tmp / "runtime-root"
voice_twin.TWIN_ROOT = voice_twin.ROOT / "voice-twin"
voice_twin.AGENTS_DIR = voice_twin.TWIN_ROOT / "agents"
voice_twin.TURN_DIR = voice_twin.TWIN_ROOT / "turns"
voice_twin.FRAME_DIR = voice_twin.TWIN_ROOT / "frames"
voice_twin.IDENTITY_FILE = voice_twin.TWIN_ROOT / "rappid.json"
voice_twin.INSTALLATION_FILE = voice_twin.TWIN_ROOT / "installation.json"
voice_twin.TRANSPORT_FILE = voice_twin.TWIN_ROOT / "transport-binding.json"
voice_twin.BINDINGS_DIR = voice_twin.TWIN_ROOT / "bindings"
voice_twin.SECRET_FILE = voice_twin.TWIN_ROOT / "transport-binding.key"
voice_twin.MEMORY_FILE = voice_twin.TWIN_ROOT / "memory.json"
voice_twin.LOCK_FILE = voice_twin.TWIN_ROOT / ".twin.lock"

cfg = {"rapp_owner": "example-owner"}


def envelope(
    remote_event_id,
    *,
    transport="discord",
    account="app-raw",
    principal="user-raw",
    conversation="channel-raw",
    scope="principal-private",
    participants=None,
    text="hello",
):
    return {
        "schema": "rapp-messaging-inbound/1.0",
        "transport": transport,
        "remote_event_id": remote_event_id,
        "account_subject": account,
        "principal_subject": principal,
        "conversation_subject": conversation,
        "scope": scope,
        "participant_subjects": participants or [principal],
        "roster_epoch": "epoch-raw",
        "text": text,
        "reply_target": {"channel_id": conversation},
    }


twin = universal_messaging.UniversalMessagingTwin(
    cfg,
    root=tmp / "journal",
)
first = envelope("event-1")
observed = twin.observe(first)
assert observed["state"] == "observed"
assert observed["event_id"].startswith("event:")

sent = []


def submit(target, text, *, attempt_id):
    sent.append((target, text, attempt_id))
    return {"state": "submitted", "remote_message_id": "remote-raw-id"}


submitted = twin.process(
    first,
    submit,
    responder=lambda value: "reply:" + value["text"],
)
assert submitted["state"] == "submitted"
assert submitted["remote_id"].startswith("remote:")
assert len(sent) == 1
assert twin.process(
    first,
    lambda *_: (_ for _ in ()).throw(AssertionError("replay resent")),
    responder=lambda _: (_ for _ in ()).throw(AssertionError("replay reran twin")),
)["state"] == "submitted"

persisted = "\n".join(
    path.read_text(encoding="utf-8")
    for path in (tmp / "journal").rglob("*.json")
)
for raw in ("app-raw", "user-raw", "channel-raw", "remote-raw-id"):
    assert raw not in persisted

# Ambiguous sends are terminal and cannot be retried automatically.
ambiguous_event = envelope("event-2", text="ambiguous")
twin.observe(ambiguous_event)
unknown = twin.process(
    ambiguous_event,
    lambda *args, **kwargs: (_ for _ in ()).throw(
        messaging_transport.AmbiguousSend("lost response")
    ),
    responder=lambda _: "uncertain reply",
)
assert unknown["state"] == "unknown"
assert unknown["ambiguous"] is True
assert twin.process(
    ambiguous_event,
    lambda *args, **kwargs: (
        (_ for _ in ()).throw(AssertionError("unknown resent"))
    ),
    responder=lambda _: (_ for _ in ()).throw(AssertionError("unknown reran")),
)["state"] == "unknown"

# Official Discord, WhatsApp, and iMessage clients return a durable remote ID
# string; the universal boundary normalizes it without persisting the raw ID.
string_event = envelope(
    "event-3",
    transport="imessage",
    account="mac-account",
    principal="owner",
    conversation="self-chat",
    scope="owner-private",
    text="mac turn",
)
twin.observe(string_event)
assert twin.process(
    string_event,
    lambda target, text, *, attempt_id: "RAW-IMESSAGE-GUID",
    responder=lambda _: "mac reply",
)["state"] == "submitted"
assert "RAW-IMESSAGE-GUID" not in "\n".join(
    path.read_text(encoding="utf-8")
    for path in (tmp / "journal").rglob("*.json")
)

# A crash after the durable attempted transition is ambiguous by definition;
# recovery marks unknown and never calls the sender again.
crash_event = envelope("event-4", text="attempt crash")
_, crash_binding, crash_context, _ = twin._sanitized_context(crash_event)
twin.journal.observe(
    event_id=crash_context["source_event_id"],
    transport="discord",
    conversation_id=crash_context["conversation_id"],
    audience_id=crash_context["audience_id"],
    scope=crash_context["scope"],
    text=crash_event["text"],
)
twin.journal.transition_inbound(crash_context["source_event_id"], "claimed")
crash_outbox = twin.journal.prepare_outbound(
    event_id=crash_context["source_event_id"],
    conversation_id=crash_context["conversation_id"],
    text="durable reply",
)
twin.journal.transition_outbound(crash_outbox["outbox_id"], "attempted")
recovered_unknown = twin.process(
    crash_event,
    lambda *args, **kwargs: (
        (_ for _ in ()).throw(AssertionError("attempted send retried"))
    ),
    responder=lambda _: "durable reply",
)
assert recovered_unknown["state"] == "unknown"
assert recovered_unknown["attempt_count"] == 1

# Terminal failures survive a crash without becoming retryable.
terminal_event = envelope("event-5", text="terminal")
terminal_info = twin.observe(terminal_event)
terminal_context = twin._sanitized_context(terminal_event)[2]
twin.journal.transition_inbound(terminal_info["event_id"], "claimed")
terminal_outbox = twin.journal.prepare_outbound(
    event_id=terminal_info["event_id"],
    conversation_id=terminal_context["conversation_id"],
    text="terminal reply",
)
twin.journal.transition_outbound(terminal_outbox["outbox_id"], "attempted")
twin.journal.transition_outbound(
    terminal_outbox["outbox_id"],
    "failed",
    failure_disposition="terminal",
)
assert twin.process(
    terminal_event,
    lambda *args, **kwargs: (
        (_ for _ in ()).throw(AssertionError("terminal failure resent"))
    ),
    responder=lambda _: "terminal reply",
)["failure_disposition"] == "terminal"
stale_sent = twin.journal.record_provider_status(
    terminal_outbox["outbox_id"],
    "sent",
    observed_at="2020-01-01T00:00:00.000Z",
)
assert stale_sent["state"] == "failed"
assert stale_sent["failure_disposition"] == "terminal"

# One event has one immutable outbox response, including retry-safe failures.
retry_event = envelope("event-6", text="retry")
twin.observe(retry_event)
try:
    twin.process(
        retry_event,
        lambda *args, **kwargs: (
            (_ for _ in ()).throw(
                messaging_transport.RetryableSend("no effect")
            )
        ),
        responder=lambda _: "first reply",
    )
    raise AssertionError("retryable send did not surface")
except messaging_transport.RetryableSend:
    pass
retry_sends = []
retried = twin.process(
    retry_event,
    lambda target, text, *, attempt_id: (
        retry_sends.append(text) or "RETRY-REMOTE-ID"
    ),
    responder=lambda _: (
        (_ for _ in ()).throw(AssertionError("retry reran responder"))
    ),
)
assert retried["state"] == "submitted"
assert retry_sends == ["first reply"]

# Replaying one provider event under another audience/scope is refused.
context_event = envelope("event-7", text="context")
twin.observe(context_event)
changed_context = {
    **context_event,
    "scope": "public",
    "principal_subject": "different-user",
    "participant_subjects": ["different-user"],
}
try:
    twin.observe(changed_context)
    raise AssertionError("event audience changed across replay")
except RuntimeError as exc:
    assert "different content" in str(exc)

# Provider evidence resolves unknown sends monotonically and never regresses.
resolved = twin.reconcile_provider_status(
    transport="discord",
    attempt_id=unknown["attempt_id"],
    remote_message_id="REMOTE-STATUS-ID",
    status="read",
    observed_at="2026-08-17T00:00:03.000Z",
)
assert resolved["state"] == "read"
regressed = twin.reconcile_provider_status(
    transport="discord",
    attempt_id=unknown["attempt_id"],
    remote_message_id="REMOTE-STATUS-ID",
    status="delivered",
    observed_at="2026-08-17T00:00:02.000Z",
)
assert regressed["state"] == "read"
assert len(regressed["provider_evidence"]) == 2

# Provider batches are completely observed before the oldest event is drained.
batch = universal_messaging.UniversalMessagingTwin(
    cfg,
    root=tmp / "batch-journal",
)
batch_calls = []
later = {
    **envelope("batch-late", text="later"),
    "remote_created_at": "2026-08-17T00:00:02.000Z",
}
earlier = {
    **envelope("batch-early", text="earlier"),
    "remote_created_at": "2026-08-17T00:00:01.000Z",
}
batch.process_batch(
    [earlier, later],
    lambda target, text, *, attempt_id: "REMOTE-" + attempt_id,
    responder=lambda value: batch_calls.append(value["text"]) or value["text"],
)
assert batch_calls == ["earlier", "later"]
batch_records = batch.journal._inbound_records(
    batch._sanitized_context(earlier)[2]["conversation_id"]
)
assert [item["conversation_sequence"] for item in batch_records] == [1, 2]

# Concurrent duplicate callers reload state after the lease; only one sends.
concurrent = universal_messaging.UniversalMessagingTwin(
    cfg,
    root=tmp / "concurrent-journal",
)
concurrent_event = envelope("concurrent-event", text="once")
concurrent.observe(concurrent_event)
gate = threading.Barrier(2)
concurrent_sends = []
concurrent_results = []
concurrent_errors = []


def concurrent_worker():
    try:
        gate.wait()
        result = concurrent.process(
            concurrent_event,
            lambda target, text, *, attempt_id: (
                concurrent_sends.append(attempt_id) or "ONE-REMOTE-ID"
            ),
            responder=lambda _: "one reply",
        )
        concurrent_results.append(result["state"])
    except Exception as exc:
        concurrent_errors.append(exc)


threads = [threading.Thread(target=concurrent_worker) for _ in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=5)
assert not any(thread.is_alive() for thread in threads)
assert concurrent_errors == []
assert concurrent_results == ["submitted", "submitted"]
assert len(concurrent_sends) == 1

# FIFO applies across distinct events in the same conversation.
journal = messaging_transport.MessagingJournal(tmp / "fifo")
secret = b"s" * 32
conversation_id = messaging_transport.private_id(
    secret,
    "conversation",
    "same",
)
audience_id = messaging_transport.private_id(secret, "audience", "same")
early = messaging_transport.private_id(secret, "event", "early")
late = messaging_transport.private_id(secret, "event", "late")
for event_id, observed_at in (
    (early, "2026-08-17T00:00:00.000Z"),
    (late, "2026-08-17T00:00:01.000Z"),
):
    journal.observe(
        event_id=event_id,
        transport="discord",
        conversation_id=conversation_id,
        audience_id=audience_id,
        scope="principal-private",
        text=event_id,
        observed_at=observed_at,
    )
try:
    journal.transition_inbound(late, "claimed")
    raise AssertionError("later event overtook unresolved earlier event")
except RuntimeError as exc:
    assert "FIFO" in str(exc)
journal.transition_inbound(early, "claimed")
journal.transition_inbound(early, "processed")
journal.transition_inbound(late, "claimed")

# Group scope requires a real multi-participant roster.
group = envelope(
    "group-event",
    scope="group-shared",
    participants=["one"],
)
with voice_twin.twin_lock():
    rappid = voice_twin.ensure_identity(cfg)
    try:
        voice_twin.channel_context(cfg, rappid, group)
        raise AssertionError("single-participant group scope was accepted")
    except RuntimeError as exc:
        assert "verified roster" in str(exc)

# Different audiences cannot enter each other's model history.
original_run = voice_twin._run_twin
captured_contexts = []


def fake_twin(message_id, text, state, config, rappid, context):
    captured_contexts.append((message_id, context))
    return "twin:" + text, []


voice_twin._run_twin = fake_twin
try:
    owner = envelope(
        "owner-event",
        transport="imessage",
        account="mac-account",
        principal="owner",
        conversation="owner-chat",
        scope="owner-private",
        text="owner secret",
    )
    public = envelope(
        "public-event",
        account="discord-app",
        principal="public-user",
        conversation="public-channel",
        scope="public",
        text="public turn",
    )
    assert voice_twin.chat_channel(owner, {}, cfg) == "twin:owner secret"
    assert voice_twin.chat_channel(public, {}, cfg) == "twin:public turn"
finally:
    voice_twin._run_twin = original_run

owner_context = captured_contexts[0][1]
public_context = captured_contexts[1][1]
assert "VoiceTwin" in owner_context["allowed_tools"]
assert "VoiceTwin" not in public_context["allowed_tools"]
assert owner_context["conversation_id"] != public_context["conversation_id"]
assert voice_twin._stored_history(public_context, "none") == [
    {"role": "user", "content": "public turn"},
    {"role": "assistant", "content": "twin:public turn"},
]
assert all(
    "owner secret" not in row["content"]
    for row in voice_twin._stored_history(public_context, "none")
)
assert (voice_twin.FRAME_DIR / "discord").is_dir()
assert (voice_twin.FRAME_DIR / "imessage").is_dir()

# One bot/business account legitimately serves multiple conversation scopes;
# account binding role is stable while per-turn scope remains in the context.
shared_account_owner = envelope(
    "mixed-owner",
    account="shared-app",
    principal="linked-owner",
    conversation="dm",
    scope="owner-private",
)
shared_account_public = envelope(
    "mixed-public",
    account="shared-app",
    principal="guild-user",
    conversation="guild-channel",
    scope="public",
)
with voice_twin.twin_lock():
    rappid = voice_twin.ensure_identity(cfg)
    owner_binding, owner_mixed_context, _ = voice_twin.channel_context(
        cfg,
        rappid,
        shared_account_owner,
    )
    public_binding, public_mixed_context, _ = voice_twin.channel_context(
        cfg,
        rappid,
        shared_account_public,
    )
assert owner_binding["binding_id"] == public_binding["binding_id"]
assert owner_binding["role"] == public_binding["role"] == "service"
assert owner_mixed_context["scope"] == "owner-private"
assert public_mixed_context["scope"] == "public"

print("Universal messaging: journal, FIFO, trust, and no-resend checks passed")
