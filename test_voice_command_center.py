#!/usr/bin/env python3
"""Deterministic safety tests for the Google Voice command center."""

import json
import pathlib
import subprocess
import tempfile
from datetime import timedelta
from types import SimpleNamespace

import voice_command_center as center

tmp = pathlib.Path(tempfile.mkdtemp(prefix="voice-command-center-test-"))
center.STATE_FILE = tmp / "commands.json"
center.AUDIT_FILE = tmp / "audit.jsonl"
center.ACTIVE_LAYOUT = tmp / "layout.json"
center.CITY_STATE = tmp / "city"
center.CITY_STATE.mkdir()

repair_action = {
    "id": "restart",
    "label": "Restart supervised daemon",
    "kind": "launchd_restart",
    "payload": {"label": "com.rapp.gateway"},
    "approval_required": True,
}
layout = {
    "schema": "rapp-infrastructure-city-layout/1",
    "generated_at": "2026-08-16T12:00:00+00:00",
    "summary": {
        "structures": 2,
        "features": 1,
        "operations": 10,
        "overall_status": "critical",
    },
    "structures": [
        {
            "entity_id": "daemon:com.rapp.gateway",
            "kind": "daemon",
            "name": "com.rapp.gateway",
            "status": "critical",
            "evidence": [{
                "source": "launchd",
                "detail": "loaded=True pid=- last_exit=1",
                "observed_at": "2026-08-16T12:00:00+00:00",
            }],
            "repairs": [repair_action],
            "features": [],
        },
        {
            "entity_id": "repo:kody-w/example",
            "kind": "repository",
            "name": "example",
            "status": "critical",
            "evidence": [{
                "source": "github",
                "detail": "archived=False private=False",
                "observed_at": "2026-08-16T12:00:00+00:00",
            }],
            "repairs": [],
            "features": [{
                "entity_id": "workflow:kody-w/example:42",
                "name": "CI",
                "status": "critical",
                "evidence": [{
                    "source": "github-actions",
                    "detail": "status=completed conclusion=failure",
                    "observed_at": "2026-08-16T12:00:00+00:00",
                }],
                "repairs": [{
                    "id": "rerun",
                    "label": "Rerun latest workflow",
                    "kind": "github_rerun",
                    "payload": {
                        "repository": "kody-w/example",
                        "run_id": 123,
                    },
                    "approval_required": True,
                }],
            }],
        },
    ],
}
center.ACTIVE_LAYOUT.write_text(json.dumps(layout))
center.bridge_health = lambda: {
    "status": "ok",
    "infrastructure_explorer": {"connected": True},
}
cfg = {
    "google_voice_model": "gpt-5.6-sol",
    "max_voice_actions_per_hour": 4,
}

assert center.parse_command("what's broken") == ("status", "")
assert center.parse_command("evidence snapshot") == ("snapshot", "")
assert center.parse_command("INSPECT gateway") == ("inspect", "gateway")
assert center.parse_command("look into CI") == ("investigate", "CI")
assert center.parse_command("request repair gateway") == ("repair", "gateway")
assert center.parse_command("APPROVE A1B2C3") == ("approve", "A1B2C3")
assert center.parse_command("cancel a1b2c3") == ("cancel", "A1B2C3")
assert center.parse_command("approve A1B2C3 and run rm -rf") == (None, None)
assert center.parse_command(
    "approve A1B2C3 " + ("x" * 4001)
) == (None, None)
assert center.parse_command("hello teammate") == (None, None)
assert "APPROVE <6-char token>" in center.help_text()

status = center.status_text()
assert "SYSTEM CRITICAL" in status
assert "3 entities" in status
assert "com.rapp.gateway" in status
assert "Bridge=ok" in status
assert "EVIDENCE STALE" in status
assert "EVIDENCE SNAPSHOT" in center.snapshot_text()
entity, problem = center.one_entity("gateway")
assert not problem and entity["entity_id"] == "daemon:com.rapp.gateway"
assert "last_exit=1" in center.evidence_detail(entity)
entity, problem = center.one_entity("missing")
assert entity is None and "No infrastructure entity" in problem
ambiguous_layout = {
    **layout,
    "structures": layout["structures"] + [
        {
            "entity_id": "daemon:api-east",
            "kind": "daemon",
            "name": "api-east",
            "status": "healthy",
            "evidence": [],
            "repairs": [],
            "features": [],
        },
        {
            "entity_id": "daemon:api-west",
            "kind": "daemon",
            "name": "api-west",
            "status": "healthy",
            "evidence": [],
            "repairs": [],
            "features": [],
        },
    ],
}
center.ACTIVE_LAYOUT.write_text(json.dumps(ambiguous_layout))
entity, problem = center.one_entity("api")
assert entity is None and "ambiguous" in problem
center.ACTIVE_LAYOUT.write_text(json.dumps(layout))

# Every recognized command result is journaled and replayed, not recomputed.
calls = []
original_dispatch = center.dispatch
center.dispatch = lambda action, argument, message_id, config, operation=None: (
    calls.append((action, argument)) or "stable reply"
)
try:
    assert center.handle("a" * 20, "status", {}, cfg) == "stable reply"
    assert center.handle("a" * 20, "status", {}, cfg) == "stable reply"
finally:
    center.dispatch = original_dispatch
assert calls == [("status", "")]

# Fake repair runtime proves request dedupe and one-time execution.
records = {}
request_calls = []
execute_calls = []
cancel_calls = []


def fake_request(entity_id, action, player):
    request_calls.append((entity_id, action["id"], player))
    record = {
        "token": "A1B2C3",
        "entity_id": entity_id,
        "action": action,
        "player": player,
        "created_at": center.iso(),
        "expires_at": center.iso(center.now() + timedelta(minutes=10)),
        "status": "pending",
    }
    records[record["token"]] = record
    return record


def fake_execute(token):
    execute_calls.append(token)
    records[token]["status"] = "executed"
    records[token]["result"] = "restart accepted"
    return records[token]


def fake_cancel(token):
    cancel_calls.append(token)
    records[token]["status"] = "cancelled"
    return records[token]


center.repair_records = lambda: records
center.load_repair_module = lambda: SimpleNamespace(
    request=fake_request,
    execute=fake_execute,
    cancel=fake_cancel,
)
repair_reply = center.handle("b" * 20, "repair gateway", {}, cfg)
assert "APPROVAL REQUIRED A1B2C3" in repair_reply
assert center.handle("b" * 20, "repair gateway", {}, cfg) == repair_reply
assert len(request_calls) == 1

approve_reply = center.handle("c" * 20, "approve A1B2C3", {}, cfg)
assert "EXECUTED" in approve_reply
assert center.handle("c" * 20, "approve A1B2C3", {}, cfg) == approve_reply
assert execute_calls == ["A1B2C3"]
# A second message with the consumed token reads terminal state, never reruns.
assert "EXECUTED" in center.handle("d" * 20, "approve A1B2C3", {}, cfg)
assert execute_calls == ["A1B2C3"]

records["D4E5F6"] = {
    **records["A1B2C3"],
    "token": "D4E5F6",
    "status": "pending",
}
assert "CANCELLED" in center.handle("e" * 20, "cancel D4E5F6", {}, cfg)
assert cancel_calls == ["D4E5F6"]

# Crash recovery: an executing command recovers from terminal repair state.
state = center.load_state()
state["commands"]["f" * 20] = {
    "action": "approve",
    "argument": "A1B2C3",
    "created_at": center.iso(),
    "status": "executing",
}
center.write_json_atomic(center.STATE_FILE, state)
assert "EXECUTED" in center.handle("f" * 20, "approve A1B2C3", {}, cfg)
assert execute_calls == ["A1B2C3"]

# Rate limiting fails closed before creating another repair request.
state = center.load_state()
for index in range(4):
    state["commands"][f"{index:020x}"] = {
        "action": "approve",
        "created_at": center.iso(),
        "status": "completed",
        "reply": "done",
    }
center.write_json_atomic(center.STATE_FILE, state)
limited = center.handle("1" * 20, "repair gateway", {}, cfg)
assert "rate limit" in limited.lower()
assert len(request_calls) == 1
future_state = center.default_state()
for index in range(4):
    future_state["commands"][f"{index + 10:020x}"] = {
        "action": "approve",
        "created_at": center.iso(center.now() + timedelta(days=1)),
        "status": "completed",
        "reply": "future",
    }
assert center.recent_action_count(future_state) == 0
assert center.parse_iso("2026-08-16T12:00:00Z").tzinfo is not None
future_layout = json.loads(json.dumps(layout))
future_layout["generated_at"] = center.iso(center.now() + timedelta(days=1))
assert "EVIDENCE FUTURE" in center.status_text(layout=future_layout)

# Expired pending records are never listed or counted as live.
records["DEAD00"] = {
    **records["A1B2C3"],
    "token": "DEAD00",
    "status": "pending",
    "expires_at": "2020-01-01T00:00:00Z",
}
records["BROKEN"] = None
assert "No pending" in center.repairs_text()
assert "expired" in center.repairs_text()
assert "invalid" in center.repairs_text()

# A pre-consumption approval failure remains retryable for the same message.
center.STATE_FILE = tmp / "preconsume-state.json"
records.clear()
records["BEEFED"] = {
    "token": "BEEFED",
    "entity_id": "daemon:com.rapp.gateway",
    "action": repair_action,
    "player": "Voice_test",
    "created_at": center.iso(),
    "expires_at": center.iso(center.now() + timedelta(minutes=10)),
    "status": "pending",
}
failures = {"remaining": 1}


def preconsume_execute(token):
    if failures["remaining"]:
        failures["remaining"] -= 1
        raise RuntimeError("injected pre-consumption failure")
    return fake_execute(token)


center.load_repair_module = lambda: SimpleNamespace(
    request=fake_request,
    execute=preconsume_execute,
    cancel=fake_cancel,
)
try:
    center.handle("4" * 20, "approve BEEFED", {}, cfg)
    raise AssertionError("pre-consumption failure must remain retryable")
except RuntimeError as exc:
    assert "did not reach a terminal state" in str(exc)
assert center.load_state()["commands"]["4" * 20]["status"] == "executing"
assert "EXECUTED" in center.handle("4" * 20, "approve BEEFED", {}, cfg)

# A request persisted before an exception is recovered, not orphaned.
center.STATE_FILE = tmp / "post-effect-state.json"
records.clear()
request_calls.clear()


def request_then_raise(entity_id, action, player):
    fake_request(entity_id, action, player)
    raise RuntimeError("injected post-effect crash")


center.load_repair_module = lambda: SimpleNamespace(
    request=request_then_raise,
    execute=fake_execute,
    cancel=fake_cancel,
)
post_effect = center.handle("5" * 20, "repair gateway", {}, cfg)
assert "APPROVAL REQUIRED A1B2C3" in post_effect
assert len(request_calls) == 1

# Repair metadata is frozen before reservation and survives layout drift.
center.STATE_FILE = tmp / "layout-drift-state.json"
records.clear()
request_calls.clear()
operation, problem = center.prepare_repair_operation("gateway")
assert not problem
drift_state = center.default_state()
drift_state["commands"]["6" * 20] = {
    "action": "repair",
    "argument": "gateway",
    "created_at": center.iso(),
    "status": "executing",
    "operation": operation,
}
center.write_json_atomic(center.STATE_FILE, drift_state)
changed_layout = json.loads(json.dumps(layout))
changed_layout["structures"][0]["repairs"][0]["id"] = "changed"
changed_layout["structures"][0]["repairs"][0]["label"] = "Changed repair"
center.ACTIVE_LAYOUT.write_text(json.dumps(changed_layout))
center.load_repair_module = lambda: SimpleNamespace(
    request=fake_request,
    execute=fake_execute,
    cancel=fake_cancel,
)
drift_reply = center.handle("6" * 20, "repair gateway", {}, cfg)
assert "Restart supervised daemon" in drift_reply
assert request_calls[0][1] == "restart"
center.ACTIVE_LAYOUT.write_text(json.dumps(layout))

# Oversized/untrusted repair metadata is rejected before request creation or
# command-journal publication.
center.STATE_FILE = tmp / "oversized-operation-state.json"
oversized_layout = json.loads(json.dumps(layout))
oversized_layout["structures"][0]["repairs"][0]["payload"]["padding"] = (
    "x" * 5000
)
center.ACTIVE_LAYOUT.write_text(json.dumps(oversized_layout))
request_calls.clear()
oversized = center.handle("7" * 20, "repair gateway", {}, cfg)
assert "invalid repair metadata" in oversized
assert request_calls == []
assert center.load_state()["commands"]["7" * 20]["status"] == "completed"
center.STATE_FILE = tmp / "invalid-payload-state.json"
invalid_layout = json.loads(json.dumps(layout))
invalid_layout["structures"][0]["repairs"][0]["payload"]["label"] = (
    "not-allowlisted"
)
center.ACTIVE_LAYOUT.write_text(json.dumps(invalid_layout))
invalid = center.handle("8" * 20, "repair gateway", {}, cfg)
assert "invalid repair metadata" in invalid
assert request_calls == []
center.STATE_FILE = tmp / "invalid-repository-state.json"
invalid_repo_layout = json.loads(json.dumps(layout))
invalid_repo_layout["structures"][1]["features"][0]["repairs"][0][
    "payload"
]["repository"] = "../.."
center.ACTIVE_LAYOUT.write_text(json.dumps(invalid_repo_layout))
invalid_repo = center.handle("9" * 20, "repair CI", {}, cfg)
assert "invalid repair metadata" in invalid_repo
assert request_calls == []
center.ACTIVE_LAYOUT.write_text(json.dumps(layout))

# SNAPSHOT uses one immutable layout for both digest and status.
loads = []
original_load_layout = center.load_layout


def one_snapshot_layout():
    loads.append(True)
    if len(loads) > 1:
        raise AssertionError("snapshot loaded two generations")
    return layout


center.load_layout = one_snapshot_layout
try:
    assert "EVIDENCE SNAPSHOT" in center.snapshot_text()
finally:
    center.load_layout = original_load_layout
assert len(loads) == 1

# Investigation gives Copilot evidence but no tools or inherited secrets.
center.STATE_FILE = tmp / "investigation-state.json"
captured = []
original_run = subprocess.run


def fake_run(command, **kwargs):
    captured.append((command, kwargs))
    if command[0] == "gh":
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([{
                "name": "CI",
                "status": "completed",
                "conclusion": "failure",
                "createdAt": "2026-08-16T00:00:00Z",
                "url": "https://example.invalid/run",
            }]),
            stderr="",
        )
    return subprocess.CompletedProcess(
        command,
        0,
        stdout="Likely a deterministic CI failure. Confidence high.",
        stderr="",
    )


subprocess.run = fake_run
try:
    diagnosis = center.handle("2" * 20, "investigate CI", {}, cfg)
finally:
    subprocess.run = original_run
assert "Confidence high" in diagnosis
copilot_call = next(value for value in captured if value[0][0] == "copilot")
assert "--available-tools=" in copilot_call[0]
assert "--disable-builtin-mcps" in copilot_call[0]
assert "OPENAI_API_KEY" not in copilot_call[1]["env"]
assert str(copilot_call[1]["cwd"]).endswith("command-sandbox")

assert center.handle("3" * 20, "ordinary conversation", {}, cfg) is None
assert center.AUDIT_FILE.exists()
assert all(
    "ordinary conversation" not in line
    for line in center.AUDIT_FILE.read_text().splitlines()
)

center.STATE_FILE.write_text(json.dumps({
    "version": 1,
    "commands": {"a" * 20: None},
}))
try:
    center.load_state()
    raise AssertionError("malformed command records must fail closed")
except RuntimeError as exc:
    assert "state is invalid" in str(exc)

center.STATE_FILE.write_text("{broken")
try:
    center.load_state()
    raise AssertionError("corrupt command state must fail closed")
except json.JSONDecodeError:
    pass

print("voice command center: 42 safety assertions passed")
