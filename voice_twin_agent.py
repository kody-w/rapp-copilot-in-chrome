#!/usr/bin/env python3
"""Single-file agent of record for the owner-private Voice twin rapplication."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agents.basic_agent import BasicAgent

__manifest__ = {
    "schema": "rapp-agent/1.0",
    "name": "@rapp/voice-twin",
    "version": "1.0.0",
    "display_name": "Voice Twin",
    "description": "Owner-private memory and identity for a transport-neutral twin.",
    "capabilities": ["filesystem-write"],
    "tags": ["twin", "voice", "memory", "owner-private"],
    "category": "identity",
    "quality_tier": "official",
    "requires_env": [
        "VOICE_TWIN_AUDIENCE_ID",
        "VOICE_TWIN_CONVERSATION_ID",
        "VOICE_TWIN_EVENT_ID",
        "VOICE_TWIN_MEMORY_FILE",
        "VOICE_TWIN_PRINCIPAL_ID",
        "VOICE_TWIN_RAPPID",
    ],
}

MEMORY_SCHEMA = "rapp-messaging-memory-store/1.0"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _memory_path():
    configured = Path(os.environ.get("VOICE_TWIN_MEMORY_FILE", "")).expanduser()
    root = Path.home() / ".rappter-chrome" / "voice-twin"
    try:
        inside = os.path.commonpath(
            [str(root.resolve()), str(configured.resolve())]
        ) == str(root.resolve())
    except (OSError, ValueError):
        inside = False
    if not configured.is_absolute() or not inside:
        raise RuntimeError("Voice Twin memory path is outside its private root")
    return configured


def _load():
    path = _memory_path()
    if not path.exists():
        return {"schema": MEMORY_SCHEMA, "records": []}
    if path.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError("Voice Twin memory exceeds its safety limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != MEMORY_SCHEMA
        or not isinstance(value.get("records"), list)
        or not all(
            isinstance(record, dict)
            and record.get("schema") == "rapp-messaging-memory/1.0"
            and isinstance(record.get("id"), str)
            and isinstance(record.get("content"), str)
            and record.get("visibility") == "owner-private"
            and isinstance(record.get("provenance"), dict)
            for record in value.get("records", [])
        )
    ):
        raise RuntimeError("Voice Twin memory is invalid")
    return value


def _save(value):
    path = _memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class VoiceTwinAgent(BasicAgent):
    def __init__(self):
        self.name = "VoiceTwin"
        self.metadata = {
            "name": self.name,
            "description": (
                "The owner-private Voice twin. Use remember for durable facts the "
                "owner explicitly asks you to retain, recall for relevant saved "
                "facts, and status for hatch identity/capabilities."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "remember", "recall"],
                    },
                    "content": {
                        "type": "string",
                        "description": "Exact owner-provided fact for remember.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive recall filter.",
                    },
                },
                "required": ["action"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def system_context(self):
        return (
            "<voice-twin-context>\n"
            "The transport broker verified one owner-private direct conversation. "
            "Use only the tools exposed in this turn. Remembering is local and "
            "owner-private. Never disclose hidden trust metadata.\n"
            "</voice-twin-context>"
        )

    def perform(self, **kwargs):
        action = str(kwargs.get("action") or "status").strip().lower()
        if action == "status":
            return json.dumps({
                "status": "ok",
                "rappid": os.environ.get("VOICE_TWIN_RAPPID", ""),
                "scope": "owner-private",
                "conformance": "structural-pre-acceptance",
            })

        state = _load()
        if action == "remember":
            content = " ".join(str(kwargs.get("content") or "").split())[:2000]
            if not content:
                return json.dumps({"status": "error", "message": "content is required"})
            event_id = os.environ.get("VOICE_TWIN_EVENT_ID", "")
            material = f"{event_id}\n{content}".encode("utf-8")
            record_id = "memory:" + hashlib.sha256(material).hexdigest()
            if not any(record["id"] == record_id for record in state["records"]):
                principal = os.environ.get("VOICE_TWIN_PRINCIPAL_ID", "")
                audience = os.environ.get("VOICE_TWIN_AUDIENCE_ID", "")
                state["records"].append({
                    "schema": "rapp-messaging-memory/1.0",
                    "id": record_id,
                    "content": content,
                    "kind": "fact",
                    "custodian_ids": [os.environ.get("VOICE_TWIN_RAPPID", "")],
                    "subject_ids": [principal],
                    "provenance": {
                        "event_id": event_id,
                        "conversation_id": os.environ.get(
                            "VOICE_TWIN_CONVERSATION_ID", ""
                        ),
                        "audience_id": audience,
                        "asserted_by": principal,
                        "recorded_at": _now(),
                    },
                    "visibility": "owner-private",
                    "allowed_audiences": [audience],
                    "grants": [],
                    "legacy": False,
                })
                state["records"] = state["records"][-1000:]
                _save(state)
            return json.dumps({"status": "remembered", "id": record_id})

        if action == "recall":
            query = " ".join(str(kwargs.get("query") or "").split()).lower()[:200]
            records = [
                record
                for record in state["records"]
                if not query or query in record["content"].lower()
            ][-10:]
            projection = [
                {
                    "id": record["id"],
                    "content": record["content"],
                    "recorded_at": record["provenance"].get("recorded_at"),
                }
                for record in records
            ]
            return json.dumps({"status": "ok", "memories": projection})

        return json.dumps({"status": "error", "message": "unsupported action"})
