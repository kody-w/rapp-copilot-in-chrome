#!/usr/bin/env python3
"""Transport-neutral RAPP Messaging inbox/outbox and single-writer boundary."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

INBOUND_STATES = {
    "observed",
    "claimed",
    "processed",
    "dropped",
    "retryable",
}
OUTBOUND_STATES = {
    "prepared",
    "attempted",
    "submitted",
    "unknown",
    "failed",
    "delivered",
    "read",
}
TERMINAL_INBOUND = {"processed", "dropped"}
TERMINAL_OUTBOUND = {"unknown", "failed", "delivered", "read"}
SCOPES = {"owner-private", "principal-private", "group-shared", "public"}
TRANSPORT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PRIVATE_ID = re.compile(
    r"^(?:event|conversation|audience|principal|account|binding|remote):"
    r"[0-9a-f]{64}$"
)
JOURNAL_SCHEMA = "rapp-messaging-journal/1.0"


class TransportError(RuntimeError):
    pass


class AmbiguousSend(TransportError):
    """The request may have reached the transport; automatic retry is forbidden."""


class RetryableSend(TransportError):
    """The transport proved no effect occurred; an explicit retry remains safe."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def evidence_timestamp(value):
    text = str(value or "")
    try:
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return float(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("provider evidence timestamp is invalid") from exc


def private_id(secret, label, value):
    import hmac

    if not re.fullmatch(r"[a-z]+", str(label or "")):
        raise ValueError("private ID label is invalid")
    digest = hmac.new(
        secret,
        f"{label}\n{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{label}:{digest}"


def outbound_id(event_id, reply):
    digest = hashlib.sha256(
        f"rapp-messaging-outbox/1.0\n{event_id}".encode("utf-8")
    ).hexdigest()
    return f"outbox:{digest}"


def validate_inbound_envelope(value):
    if not isinstance(value, dict):
        raise ValueError("inbound envelope must be an object")
    required = {
        "schema",
        "transport",
        "remote_event_id",
        "account_subject",
        "principal_subject",
        "conversation_subject",
        "scope",
        "participant_subjects",
        "roster_epoch",
        "text",
        "reply_target",
    }
    if set(value) - (required | {"remote_created_at", "metadata"}):
        raise ValueError("inbound envelope contains unsupported fields")
    if not required <= set(value):
        raise ValueError("inbound envelope is missing required fields")
    if value["schema"] != "rapp-messaging-inbound/1.0":
        raise ValueError("inbound envelope schema is invalid")
    if not TRANSPORT.fullmatch(str(value["transport"] or "")):
        raise ValueError("transport name is invalid")
    for key in (
        "remote_event_id",
        "account_subject",
        "principal_subject",
        "conversation_subject",
        "roster_epoch",
        "text",
    ):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"{key} must be a non-empty string")
        if len(value[key]) > (4000 if key == "text" else 512):
            raise ValueError(f"{key} exceeds its limit")
    if value["scope"] not in SCOPES:
        raise ValueError("conversation scope is invalid")
    if (
        not isinstance(value["participant_subjects"], list)
        or not value["participant_subjects"]
        or len(value["participant_subjects"]) > 1000
        or not all(
            isinstance(item, str) and 0 < len(item) <= 512
            for item in value["participant_subjects"]
        )
        or len(set(value["participant_subjects"]))
        != len(value["participant_subjects"])
    ):
        raise ValueError("participant subjects are invalid")
    if not isinstance(value["reply_target"], dict):
        raise ValueError("reply_target must be an object")
    return value


def _fsync_directory(path):
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_atomic(path, value):
    path = Path(path)
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
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError(f"messaging journal record too large: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"messaging journal record invalid: {path.name}") from exc


class MessagingJournal:
    def __init__(self, root):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.outbox = self.root / "outbox"
        self.locks = self.root / "locks"
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    @contextmanager
    def _lock(self, name):
        self.locks.mkdir(parents=True, exist_ok=True)
        path = self.locks / f"{name}.lock"
        handle = open(path, "a+", encoding="utf-8")
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    @contextmanager
    def transport_lease(self, binding_id):
        if not PRIVATE_ID.fullmatch(binding_id):
            raise ValueError("binding ID is invalid")
        with self._lock(f"lease-{binding_id.split(':', 1)[1]}"):
            yield

    def _inbox_path(self, event_id):
        if not PRIVATE_ID.fullmatch(event_id) or not event_id.startswith("event:"):
            raise ValueError("event ID is invalid")
        return self.inbox / f"{event_id.split(':', 1)[1]}.json"

    def _outbox_path(self, identifier):
        if not re.fullmatch(r"outbox:[0-9a-f]{64}", identifier):
            raise ValueError("outbox ID is invalid")
        return self.outbox / f"{identifier.split(':', 1)[1]}.json"

    def observe(
        self,
        *,
        event_id,
        transport,
        conversation_id,
        audience_id,
        scope,
        text,
        observed_at=None,
    ):
        if (
            not PRIVATE_ID.fullmatch(conversation_id)
            or not conversation_id.startswith("conversation:")
            or not PRIVATE_ID.fullmatch(audience_id)
            or not audience_id.startswith("audience:")
            or not TRANSPORT.fullmatch(transport)
            or scope not in SCOPES
            or not isinstance(text, str)
            or not text
            or len(text) > 4000
        ):
            raise ValueError("sanitized inbound record is invalid")
        path = self._inbox_path(event_id)
        with self._lock("inbox-observe"):
            existing = read_json(path)
            if existing is not None:
                if (
                    existing.get("transport") != transport
                    or existing.get("conversation_id") != conversation_id
                    or existing.get("audience_id") != audience_id
                    or existing.get("scope") != scope
                    or existing.get("text") != text
                ):
                    raise RuntimeError("event ID was reused with different content")
                return existing
            prior = self._inbound_records(conversation_id)
            sequence = max(
                (
                    int(item.get("conversation_sequence", 0))
                    for item in prior
                    if type(item.get("conversation_sequence", 0)) is int
                ),
                default=0,
            ) + 1
            current = observed_at or utc_now()
            record = {
                "schema": JOURNAL_SCHEMA,
                "direction": "inbound",
                "event_id": event_id,
                "transport": transport,
                "conversation_id": conversation_id,
                "audience_id": audience_id,
                "scope": scope,
                "text": text,
                "conversation_sequence": sequence,
                "state": "observed",
                "created_at": current,
                "updated_at": current,
                "transitions": [{"state": "observed", "at": current}],
            }
            write_json_atomic(path, record)
            return record

    def _inbound_records(self, conversation_id):
        records = []
        if not self.inbox.exists():
            return records
        for path in self.inbox.glob("*.json"):
            value = read_json(path)
            if (
                isinstance(value, dict)
                and value.get("schema") == JOURNAL_SCHEMA
                and value.get("direction") == "inbound"
                and value.get("conversation_id") == conversation_id
            ):
                records.append(value)
        return sorted(
            records,
            key=lambda value: (
                int(value.get("conversation_sequence", 0)),
                value["event_id"],
            ),
        )

    def inbound_record(self, event_id):
        value = read_json(self._inbox_path(event_id))
        if value is not None and not isinstance(value, dict):
            raise RuntimeError("inbound record is invalid")
        return value

    def oldest_unresolved(self, conversation_id):
        values = [
            value
            for value in self._inbound_records(conversation_id)
            if value.get("state") not in TERMINAL_INBOUND
        ]
        return values[0] if values else None

    def transition_inbound(self, event_id, target):
        if target not in INBOUND_STATES:
            raise ValueError("inbound target state is invalid")
        path = self._inbox_path(event_id)
        with self._lock("inbox-transitions"):
            record = read_json(path)
            if not isinstance(record, dict):
                raise RuntimeError("inbound record is missing")
            current = record.get("state")
            allowed = {
                "observed": {"claimed", "dropped"},
                "claimed": {"processed", "retryable", "dropped"},
                "retryable": {"claimed", "dropped"},
            }
            if target == current:
                return record
            if target not in allowed.get(current, set()):
                raise RuntimeError(f"invalid inbound transition {current}->{target}")
            if target == "claimed":
                unresolved = [
                    item
                    for item in self._inbound_records(record["conversation_id"])
                    if item.get("state") not in TERMINAL_INBOUND
                ]
                if not unresolved or unresolved[0]["event_id"] != event_id:
                    raise RuntimeError("FIFO violation: an older inbound is unresolved")
            now = utc_now()
            record["state"] = target
            record["updated_at"] = now
            record.setdefault("transitions", []).append({"state": target, "at": now})
            write_json_atomic(path, record)
            return record

    def prepare_outbound(self, *, event_id, conversation_id, text):
        if (
            not PRIVATE_ID.fullmatch(event_id)
            or not event_id.startswith("event:")
            or not PRIVATE_ID.fullmatch(conversation_id)
            or not isinstance(text, str)
            or not text
            or len(text) > 4000
        ):
            raise ValueError("outbound preparation is invalid")
        identifier = outbound_id(event_id, text)
        path = self._outbox_path(identifier)
        with self._lock(f"outbox-{identifier.split(':', 1)[1]}"):
            existing = read_json(path)
            if existing is not None:
                if (
                    existing.get("event_id") != event_id
                    or existing.get("text") != text
                ):
                    raise RuntimeError(
                        "outbox event was reused with different content"
                    )
                return existing
            now = utc_now()
            record = {
                "schema": JOURNAL_SCHEMA,
                "direction": "outbound",
                "outbox_id": identifier,
                "event_id": event_id,
                "conversation_id": conversation_id,
                "text": text,
                "state": "prepared",
                "attempt_count": 0,
                "ambiguous": False,
                "created_at": now,
                "updated_at": now,
                "transitions": [{"state": "prepared", "at": now}],
            }
            write_json_atomic(path, record)
            return record

    def outbound_for_event(self, event_id):
        if not PRIVATE_ID.fullmatch(event_id) or not event_id.startswith("event:"):
            raise ValueError("event ID is invalid")
        if not self.outbox.exists():
            return None
        matches = []
        for path in self.outbox.glob("*.json"):
            value = read_json(path)
            if isinstance(value, dict) and value.get("event_id") == event_id:
                matches.append(value)
        if len(matches) > 1:
            raise RuntimeError("one inbound event has multiple outbound records")
        return matches[0] if matches else None

    def transition_outbound(
        self,
        outbox_id,
        target,
        remote_id=None,
        failure_disposition=None,
    ):
        if target not in OUTBOUND_STATES:
            raise ValueError("outbound target state is invalid")
        path = self._outbox_path(outbox_id)
        with self._lock(f"outbox-{outbox_id.split(':', 1)[1]}"):
            record = read_json(path)
            if not isinstance(record, dict):
                raise RuntimeError("outbound record is missing")
            current = record.get("state")
            allowed = {
                "prepared": {"attempted", "failed"},
                "attempted": {"submitted", "unknown", "failed"},
                "submitted": {"delivered", "read", "failed"},
                "delivered": {"read"},
                "failed": {"attempted"},
            }
            if target == current:
                return record
            if target not in allowed.get(current, set()):
                raise RuntimeError(f"invalid outbound transition {current}->{target}")
            if (
                current == "failed"
                and target == "attempted"
                and record.get("failure_disposition") != "retryable"
            ):
                raise RuntimeError("terminal outbound failure cannot be retried")
            if target == "attempted":
                record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
                record["attempt_id"] = outbox_id.split(":", 1)[1][:25]
            if target == "unknown":
                record["ambiguous"] = True
            if remote_id is not None:
                if not PRIVATE_ID.fullmatch(remote_id) or not remote_id.startswith(
                    "remote:"
                ):
                    raise ValueError("remote evidence ID is invalid")
                record["remote_id"] = remote_id
            if target == "failed":
                if failure_disposition not in {"retryable", "terminal"}:
                    raise ValueError("failed outbound requires a disposition")
                record["failure_disposition"] = failure_disposition
            now = utc_now()
            record["state"] = target
            record["updated_at"] = now
            record.setdefault("transitions", []).append({"state": target, "at": now})
            write_json_atomic(path, record)
            return record

    def outbox_for_attempt(self, attempt_id):
        if not isinstance(attempt_id, str) or not re.fullmatch(
            r"[0-9a-f]{25}",
            attempt_id,
        ):
            raise ValueError("attempt ID is invalid")
        matches = []
        if self.outbox.exists():
            for path in self.outbox.glob("*.json"):
                value = read_json(path)
                if isinstance(value, dict) and value.get("attempt_id") == attempt_id:
                    matches.append(value)
        if len(matches) > 1:
            raise RuntimeError("attempt ID maps to multiple outbox records")
        return matches[0] if matches else None

    def record_provider_status(
        self,
        outbox_id,
        status,
        *,
        remote_id=None,
        observed_at=None,
    ):
        normalized = str(status or "").strip().lower()
        if normalized not in {
            "sent",
            "submitted",
            "delivered",
            "read",
            "played",
            "failed",
        }:
            raise ValueError("provider status is invalid")
        path = self._outbox_path(outbox_id)
        with self._lock(f"outbox-{outbox_id.split(':', 1)[1]}"):
            record = read_json(path)
            if not isinstance(record, dict):
                raise RuntimeError("outbound record is missing")
            if remote_id is not None and (
                not PRIVATE_ID.fullmatch(remote_id)
                or not remote_id.startswith("remote:")
            ):
                raise ValueError("remote evidence ID is invalid")
            at = observed_at or utc_now()
            at_value = evidence_timestamp(at)
            current_at = evidence_timestamp(
                record.get("state_evidence_at")
                or record.get("updated_at")
                or record.get("created_at")
            )
            evidence = {
                "status": normalized,
                "at": at,
                **({"remote_id": remote_id} if remote_id else {}),
            }
            items = record.setdefault("provider_evidence", [])
            if evidence not in items:
                items.append(evidence)
            current = record.get("state")
            if normalized in {"read", "played"}:
                effective = "read"
            elif normalized == "delivered":
                effective = "read" if current == "read" else "delivered"
            elif normalized in {"sent", "submitted"}:
                effective = (
                    current
                    if current in {"delivered", "read"}
                    or (
                        current == "failed"
                        and record.get("failure_disposition") == "terminal"
                    )
                    else "submitted"
                )
            else:
                effective = (
                    current
                    if current in {"delivered", "read"}
                    or (current == "submitted" and at_value < current_at)
                    else "failed"
                )
            if effective == "failed":
                record["failure_disposition"] = "terminal"
            if remote_id:
                record["remote_id"] = remote_id
            if effective != current:
                record["state"] = effective
                record["state_evidence_at"] = at
                record.setdefault("transitions", []).append({
                    "state": effective,
                    "at": at,
                    "source": "provider",
                })
            record["updated_at"] = at
            write_json_atomic(path, record)
            return record
