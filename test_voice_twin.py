#!/usr/bin/env python3
"""Deterministic tests for the hatched RAPP/1 Voice twin."""

import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types

import build_voice_twin_egg
import rapp1
import voice_twin

tmp = pathlib.Path(tempfile.mkdtemp(prefix="voice-twin-test-"))
root = tmp / "runtime-root"
voice_twin.ROOT = root
voice_twin.TWIN_ROOT = root / "voice-twin"
voice_twin.AGENTS_DIR = voice_twin.TWIN_ROOT / "agents"
voice_twin.TURN_DIR = voice_twin.TWIN_ROOT / "turns"
voice_twin.FRAME_DIR = voice_twin.TWIN_ROOT / "frames"
voice_twin.IDENTITY_FILE = voice_twin.TWIN_ROOT / "rappid.json"
voice_twin.INSTALLATION_FILE = voice_twin.TWIN_ROOT / "installation.json"
voice_twin.TRANSPORT_FILE = voice_twin.TWIN_ROOT / "transport-binding.json"
voice_twin.SECRET_FILE = voice_twin.TWIN_ROOT / "transport-binding.key"
voice_twin.MEMORY_FILE = voice_twin.TWIN_ROOT / "memory.json"
voice_twin.LOCK_FILE = voice_twin.TWIN_ROOT / ".twin.lock"

brainstem = tmp / "brainstem"
python = brainstem / "venv" / "bin" / "python"
source = brainstem / "src" / "rapp_brainstem"
agents = source / "agents"
agents.mkdir(parents=True)
python.parent.mkdir(parents=True)
python.write_text("", encoding="utf-8")
(source / "brainstem.py").write_text("", encoding="utf-8")
for name in voice_twin.CURATED_AGENT_FILES:
    (agents / name).write_text(f"# {name}\n", encoding="utf-8")

cfg = {
    "brainstem_root": str(brainstem),
    "google_voice_account": "expected@example.com",
    "google_voice_peer": "5558675309",
    "google_voice_model": "test-model",
    "rapp_owner": "example-owner",
}

calls = []


def fake_run(command, **kwargs):
    calls.append((command, kwargs))
    request = json.loads(pathlib.Path(command[-2]).read_text(encoding="utf-8"))
    assert request["idempotency_key"] == "a" * 20
    assert request["session_id"].startswith("conversation:")
    assert request["conversation_history"] == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "prior reply"},
    ]
    pathlib.Path(command[-1]).write_text(
        json.dumps({
            "status": 200,
            "body": {
                "response": "I fetched current Hacker News stories.",
                "agent_logs": "[HackerNews] live result",
            },
        }),
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


original_run = voice_twin.subprocess.run
voice_twin.subprocess.run = fake_run
os.environ["SHOULD_NOT_REACH_TWIN"] = "secret"
try:
    reply = voice_twin.chat(
        "a" * 20,
        "get Hacker News",
        {
            "transcript": [
                {"role": "Owner", "text": "earlier"},
                {"role": "Copilot", "text": "prior reply [#" + ("B" * 20) + "]"},
            ]
        },
        cfg,
    )
    assert reply == "I fetched current Hacker News stories."
    assert voice_twin.chat(
        "a" * 20,
        "get Hacker News",
        {},
        cfg,
    ) == reply
finally:
    voice_twin.subprocess.run = original_run
    os.environ.pop("SHOULD_NOT_REACH_TWIN", None)

assert len(calls) == 1, "a replay must return the durable twin result"
assert "SHOULD_NOT_REACH_TWIN" not in calls[0][1]["env"]
assert calls[0][1]["env"]["AGENTS_PATH"] == str(voice_twin.AGENTS_DIR)
manifest = json.loads(
    (voice_twin.AGENTS_DIR / "manifest.json").read_text(encoding="utf-8")
)
assert set(manifest["agents"]) == {
    *voice_twin.CURATED_AGENT_FILES,
    "voice_twin_agent.py",
}
assert "copilot_cli_agent.py" not in manifest["agents"]
assert "learn_new_agent.py" not in manifest["agents"]
assert "imessage_agent.py" not in manifest["agents"]

identity = json.loads(voice_twin.IDENTITY_FILE.read_text(encoding="utf-8"))
assert rapp1.rappid_valid(identity["rappid"])
assert identity["rappid"].startswith("rappid:@example-owner/voice-twin:")
assert identity["rappid"].rsplit(":", 1)[1] != hashlib.sha256(
    b"example-owner/voice-twin"
).hexdigest()

binding_text = voice_twin.TRANSPORT_FILE.read_text(encoding="utf-8")
assert "expected@example.com" not in binding_text
assert "5558675309" not in binding_text
binding = json.loads(binding_text)
assert binding["schema"] == "rapp-messaging-transport-binding/1.0"
assert binding["transport"] == "google-voice-web"

frames = voice_twin._load_frames(f"{identity['rappid']}:google-voice")
assert len(frames) == 1
assert set(frames[0]) == rapp1.FRAME_KEYS
assert frames[0]["kind"] == "memory.chat-turn"
assert frames[0]["payload"]["message_id"] == "a" * 20
assert frames[0]["payload"]["agent_names"] == ["HackerNews"]
assert voice_twin.successful_agent_names(
    '[HackerNews] {"status":"error","message":"offline"}'
) == []
assert voice_twin.successful_agent_names(
    '[HackerNews] {"status":"success","stories":[]}'
) == ["HackerNews"]
assert not voice_twin.action_claim_supported(
    "I fetched current stories.",
    [],
)
assert not voice_twin.action_claim_supported(
    "I fetched current stories.",
    ["HashText"],
)
assert voice_twin.action_claim_supported(
    "I fetched current stories.",
    ["HackerNews"],
)
assert not voice_twin.action_claim_supported(
    "I remembered that.",
    ["HackerNews"],
)
assert voice_twin.action_claim_supported(
    "I remembered that.",
    ["VoiceTwin"],
)

# An executing record after a crash is never rerun. It becomes a terminal,
# evidence-backed interruption response.
interrupted_id = "b" * 20
interrupted_input = "remember this"
voice_twin._write_turn({
    "schema": "rapp-voice-twin-turn/1.0",
    "message_id": interrupted_id,
    "request_hash": hashlib.sha256(interrupted_input.encode()).hexdigest(),
    "user_input": interrupted_input,
    "status": "executing",
    "created_at": voice_twin._utc(),
})
voice_twin.subprocess.run = lambda *args, **kwargs: (
    (_ for _ in ()).throw(AssertionError("interrupted turn reran agents"))
)
try:
    interrupted = voice_twin.chat(
        interrupted_id,
        interrupted_input,
        {},
        cfg,
    )
finally:
    voice_twin.subprocess.run = original_run
assert "interrupted before a verifiable result" in interrupted
assert len(voice_twin._load_frames(f"{identity['rappid']}:google-voice")) == 2

# The portable artifact is a deterministic, verified RAPP/1 rapplication egg
# with exactly one root agent.py.
egg_path = tmp / "voice-twin.rapp.egg"
result = build_voice_twin_egg.build(
    cfg,
    output=egg_path,
    created_utc="2026-08-17T00:00:00.000Z",
)
assert result["status"] == "structural-pre-acceptance"
blob = egg_path.read_bytes()
assert rapp1.verify_egg(blob)[0]
egg_manifest, files = rapp1.read_egg(blob)
assert egg_manifest["variant"] == "rapplication"
assert set(files) == {
    "agent.py",
    "rappid.json",
    "state/conformance.json",
}
assert files["agent.py"] == voice_twin.AGENT_FILE.read_bytes()

# The single-file agent writes idempotent owner-private memory records with
# the RAPP Messaging trust fields and returns only a projected view.
agents_module = types.ModuleType("agents")
agents_module.__path__ = []
basic_module = types.ModuleType("agents.basic_agent")


class BasicAgent:
    def __init__(self, *args, **kwargs):
        pass


basic_module.BasicAgent = BasicAgent
agents_module.basic_agent = basic_module
old_agents = sys.modules.get("agents")
old_basic = sys.modules.get("agents.basic_agent")
sys.modules["agents"] = agents_module
sys.modules["agents.basic_agent"] = basic_module
agent_spec = importlib.util.spec_from_file_location(
    "tested_voice_twin_agent",
    voice_twin.AGENT_FILE,
)
agent_module = importlib.util.module_from_spec(agent_spec)
agent_spec.loader.exec_module(agent_module)
memory_home = tmp / "memory-home"
memory_path = memory_home / ".rappter-chrome" / "voice-twin" / "memory.json"
old_home = os.environ.get("HOME")
old_twin_env = {
    key: value
    for key, value in os.environ.items()
    if key.startswith("VOICE_TWIN_")
}
os.environ.update({
    "HOME": str(memory_home),
    "VOICE_TWIN_AUDIENCE_ID": "audience:" + ("1" * 64),
    "VOICE_TWIN_CONVERSATION_ID": "conversation:" + ("2" * 64),
    "VOICE_TWIN_EVENT_ID": "c" * 20,
    "VOICE_TWIN_MEMORY_FILE": str(memory_path),
    "VOICE_TWIN_PRINCIPAL_ID": "principal:" + ("3" * 64),
    "VOICE_TWIN_RAPPID": identity["rappid"],
})
try:
    memory_agent = agent_module.VoiceTwinAgent()
    remembered = json.loads(memory_agent.perform(
        action="remember",
        content="The owner explicitly asked to remember this.",
    ))
    assert remembered["status"] == "remembered"
    assert json.loads(memory_agent.perform(
        action="remember",
        content="The owner explicitly asked to remember this.",
    ))["id"] == remembered["id"]
    projected = json.loads(memory_agent.perform(
        action="recall",
        query="explicitly",
    ))
    assert len(projected["memories"]) == 1
    assert set(projected["memories"][0]) == {"id", "content", "recorded_at"}
    memory_state = json.loads(memory_path.read_text(encoding="utf-8"))
    assert len(memory_state["records"]) == 1
    memory_record = memory_state["records"][0]
    assert memory_record["schema"] == "rapp-messaging-memory/1.0"
    assert memory_record["visibility"] == "owner-private"
    assert memory_record["grants"] == []
    assert memory_record["provenance"]["event_id"] == "c" * 20
finally:
    for key in tuple(os.environ):
        if key.startswith("VOICE_TWIN_"):
            os.environ.pop(key, None)
    os.environ.update(old_twin_env)
    if old_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old_home
    if old_agents is None:
        sys.modules.pop("agents", None)
    else:
        sys.modules["agents"] = old_agents
    if old_basic is None:
        sys.modules.pop("agents.basic_agent", None)
    else:
        sys.modules["agents.basic_agent"] = old_basic

reference_hash = hashlib.sha256(
    (voice_twin.HERE / "rapp1.py").read_bytes()
).hexdigest()
assert reference_hash == (
    "c945ee85f01af5cd374490b40721d07f2aca7c8bd6d209e0d2933420f55db284"
)

print("Voice Twin: RAPP/1 identity, frames, hatch, and replay checks passed")
