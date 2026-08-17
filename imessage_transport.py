#!/usr/bin/env python3
"""Fail-closed iMessage adapter for ``rapp-messaging-imessage/1.0``."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from messaging_transport import (
    AmbiguousSend,
    RetryableSend,
    TransportError,
    validate_inbound_envelope,
)


PROFILE_SCHEMA = "rapp-messaging-imessage/1.0"
INBOUND_SCHEMA = "rapp-messaging-inbound/1.0"
IMSG_PINNED_VERSION = "0.12.3"
MAX_RPC_LINE_CHARS = 1_048_576
GUID_STORE_SCHEMA = "rapp-imessage-outbound-guids/1.0"


def _guid_digest(value):
    return hashlib.sha256(
        f"{PROFILE_SCHEMA}\noutbound-guid\n{value}".encode("utf-8")
    ).hexdigest()


def _load_guid_store(path):
    path = Path(path)
    if not path.exists():
        return set()
    if path.stat().st_size > 1024 * 1024:
        raise TransportError("iMessage outbound GUID store exceeds its limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransportError("iMessage outbound GUID store is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != GUID_STORE_SCHEMA
        or not isinstance(value.get("guid_hashes"), list)
        or not all(
            re.fullmatch(r"[0-9a-f]{64}", str(item or ""))
            for item in value["guid_hashes"]
        )
    ):
        raise TransportError("iMessage outbound GUID store is invalid")
    return set(value["guid_hashes"])


def _save_guid_store(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema": GUID_STORE_SCHEMA,
                    "guid_hashes": sorted(values)[-10000:],
                },
                handle,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class IMessageConfigError(TransportError):
    """The local iMessage configuration is disabled or unsafe."""


class ImsgProtocolError(TransportError):
    """The supervised child violated newline JSON-RPC framing."""


class ImsgClosed(TransportError):
    """The supervised child exited or its stdio became unavailable."""


@dataclass(frozen=True)
class IMessageSettings:
    imsg_path: str
    owner_handles: frozenset[str]
    owner_chat_guids: frozenset[str]
    dm_allowlist: frozenset[str]
    group_allowlist: frozenset[str]
    mention_tokens: tuple[str, ...]
    worker_count: int
    writer_count: int
    account_subject: str
    owner_subject: str
    principal_by_handle: Mapping[str, str]

    def principal_subject(self, handle: str) -> str:
        normalized = _normalize_handle(handle)
        if normalized in self.owner_handles:
            return self.owner_subject
        linked = self.principal_by_handle.get(normalized)
        if linked:
            return linked
        return _opaque_subject("principal", self.account_subject, normalized)


@dataclass(frozen=True)
class ParsedIMessage:
    """An envelope plus its local-only read cursor and echo classification."""

    cursor: int
    envelope: dict[str, Any] | None
    outbound_guid: str | None = None

    @property
    def is_bot_echo(self) -> bool:
        return self.outbound_guid is not None


_MISSING = object()


def _configured(
    cfg: Mapping[str, Any],
    *names: str,
    default: object = _MISSING,
) -> object:
    for name in names:
        if name in cfg:
            return cfg[name]
    if default is not _MISSING:
        return default
    raise IMessageConfigError(f"missing explicit configuration: {names[0]}")


def _normalize_handle(value: object) -> str:
    return str(value or "").strip().casefold()


def _string_set(
    value: object,
    field_name: str,
    *,
    normalize: bool = False,
    allow_mapping: bool = False,
) -> frozenset[str]:
    if allow_mapping and isinstance(value, Mapping):
        value = list(value)
    if (
        isinstance(value, (str, bytes))
        or (isinstance(value, Mapping) and not allow_mapping)
        or not isinstance(value, Collection)
    ):
        raise IMessageConfigError(f"{field_name} must be an array")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            raise IMessageConfigError(f"{field_name} entries must be strings")
        text = _normalize_handle(item) if normalize else str(item).strip()
        if not text:
            raise IMessageConfigError(f"{field_name} entries cannot be empty")
        result.add(text)
    return frozenset(result)


def _opaque_subject(kind: str, *parts: object) -> str:
    body = "\n".join(
        [PROFILE_SCHEMA, kind, *(str(part) for part in parts)]
    ).encode("utf-8")
    return f"{kind}:{hashlib.sha256(body).hexdigest()}"


def _subject_from_config(value: object, kind: str, *fallback: object) -> str:
    text = str(value or "").strip()
    if text:
        if len(text) > 512:
            raise IMessageConfigError(f"{kind} subject exceeds its limit")
        if re.fullmatch(rf"{re.escape(kind)}:[0-9a-f]{{64}}", text):
            return text
        return _opaque_subject(kind, text)
    return _opaque_subject(kind, *fallback)


def _explicit_subject(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IMessageConfigError(f"{field_name} must be a non-empty string")
    subject = value.strip()
    if len(subject) > 512:
        raise IMessageConfigError(f"{field_name} exceeds its limit")
    return subject


def _principal_links(
    cfg: Mapping[str, Any],
    account_subject: str,
    owner_handles: frozenset[str],
) -> dict[str, str]:
    raw = _configured(
        cfg,
        "imessage_principal_links",
        "identity_links",
        default={},
    )
    if not isinstance(raw, Mapping):
        raise IMessageConfigError("imessage_principal_links must be an object")
    result: dict[str, str] = {}
    for label, handles in raw.items():
        label_text = str(label).strip()
        if not label_text:
            raise IMessageConfigError("principal link labels cannot be empty")
        subject = _subject_from_config(
            label_text if label_text.startswith("principal:") else "",
            "principal",
            account_subject,
            label_text,
        )
        normalized_handles = _string_set(
            handles,
            f"imessage_principal_links.{label_text}",
            normalize=True,
        )
        for handle in normalized_handles:
            if handle in owner_handles:
                raise IMessageConfigError(
                    "owner handles cannot appear in principal links"
                )
            previous = result.setdefault(handle, subject)
            if previous != subject:
                raise IMessageConfigError(
                    "a handle cannot be linked to multiple principals"
                )
    return result


def validate_imessage_config(
    cfg: Mapping[str, Any],
    *,
    platform: str | None = None,
) -> IMessageSettings:
    """Validate the opt-in, single-writer, single-worker transport settings."""

    if not isinstance(cfg, Mapping):
        raise IMessageConfigError("iMessage configuration must be an object")
    if cfg.get("imessage_enabled") is not True:
        raise IMessageConfigError("iMessage transport is disabled")
    if (sys.platform if platform is None else platform) != "darwin":
        raise IMessageConfigError("iMessage transport requires macOS")

    version = str(
        _configured(
            cfg,
            "imessage_version",
            "imessage_imsg_version",
            "imsg_version",
            default=IMSG_PINNED_VERSION,
        )
    ).strip()
    if version != IMSG_PINNED_VERSION:
        raise IMessageConfigError(
            f"imsg version must be pinned to {IMSG_PINNED_VERSION}"
        )

    owner_handles = _string_set(
        _configured(cfg, "imessage_owner_handles", "owner_handles"),
        "imessage_owner_handles",
        normalize=True,
    )
    owner_chat_guids = _string_set(
        _configured(
            cfg,
            "imessage_owner_chat_guids",
            "imessage_owner_self_chat_guids",
            "imessage_owner_chat_ids",
            "owner_chat_guids",
            "owner_chat_ids",
        ),
        "imessage_owner_chat_guids",
    )
    dm_allowlist = _string_set(
        _configured(
            cfg,
            "imessage_dm_allowlist",
            "imessage_dm_allowlist_handles",
            "imessage_allowed_dm_handles",
            "allowed_dm_handles",
        ),
        "imessage_dm_allowlist",
        normalize=True,
    )
    group_allowlist = _string_set(
        _configured(
            cfg,
            "imessage_group_allowlist",
            "imessage_group_allowlist_guids",
            "imessage_allowed_group_chat_guids",
            "imessage_allowed_group_chat_ids",
            "allowed_group_chat_guids",
            "allowed_group_chat_ids",
        ),
        "imessage_group_allowlist",
        allow_mapping=True,
    )
    if not owner_handles:
        raise IMessageConfigError("at least one owner handle is required")
    if not owner_chat_guids:
        raise IMessageConfigError(
            "at least one enrolled owner self-chat GUID is required"
        )
    if owner_chat_guids & group_allowlist:
        raise IMessageConfigError(
            "owner self-chat GUIDs cannot also be allowed groups"
        )

    mention_required = _configured(
        cfg,
        "imessage_mention_required",
        "mention_required",
        default=True,
    )
    if mention_required is not True:
        raise IMessageConfigError("iMessage groups must be mention-gated")
    mention_tokens_value = _configured(
        cfg,
        "imessage_mention_tokens",
        "imessage_group_mentions",
        "imessage_mentions",
        "mention_tokens",
        default=("@rappter",),
    )
    mention_tokens = tuple(
        _string_set(
            mention_tokens_value,
            "imessage_mention_tokens",
        )
    )
    if not mention_tokens:
        raise IMessageConfigError(
            "at least one group mention token is required"
        )

    worker_count = _configured(
        cfg,
        "imessage_worker_count",
        "worker_count",
        default=1,
    )
    writer_count = _configured(
        cfg,
        "imessage_writer_count",
        "writer_count",
        default=1,
    )
    if type(worker_count) is not int or worker_count != 1:
        raise IMessageConfigError(
            "imessage_worker_count must be exactly 1 for FIFO dispatch"
        )
    if type(writer_count) is not int or writer_count != 1:
        raise IMessageConfigError(
            "imessage_writer_count must be exactly 1"
        )

    imsg_path = str(
        _configured(
            cfg,
            "imessage_path",
            "imessage_imsg_path",
            "imsg_path",
            default="imsg",
        )
    ).strip()
    if not imsg_path:
        raise IMessageConfigError("imessage_path is required")

    if "imessage_account_subject" in cfg:
        account_subject = _explicit_subject(
            cfg["imessage_account_subject"],
            "imessage_account_subject",
        )
    else:
        account_seed = _configured(
            cfg,
            "imessage_account_id",
            "account_id",
            default="",
        )
        account_subject = _subject_from_config(
            account_seed,
            "account",
            *sorted(owner_handles),
            *sorted(owner_chat_guids),
        )
    if "imessage_owner_subject" in cfg:
        owner_subject = _explicit_subject(
            cfg["imessage_owner_subject"],
            "imessage_owner_subject",
        )
    else:
        owner_subject = _opaque_subject(
            "principal",
            account_subject,
            "owner",
            *sorted(owner_handles),
        )
    principal_by_handle = _principal_links(
        cfg,
        account_subject,
        owner_handles,
    )
    return IMessageSettings(
        imsg_path=imsg_path,
        owner_handles=owner_handles,
        owner_chat_guids=owner_chat_guids,
        dm_allowlist=dm_allowlist,
        group_allowlist=group_allowlist,
        mention_tokens=mention_tokens,
        worker_count=worker_count,
        writer_count=writer_count,
        account_subject=account_subject,
        owner_subject=owner_subject,
        principal_by_handle=principal_by_handle,
    )


def _strict_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TransportError(f"incoming {name} must be boolean")
    return value


def _read_cursor(raw: Mapping[str, Any]) -> int:
    value = raw.get("id", raw.get("rowid"))
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    raise TransportError("incoming message has no valid read cursor")


def _reply_target(raw: Mapping[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {}
    chat_id = raw.get("chat_id")
    if type(chat_id) is int and chat_id > 0:
        target["chat_id"] = chat_id
    elif isinstance(chat_id, str) and chat_id.strip():
        target["chat_id"] = chat_id.strip()
    chat_guid = str(raw.get("chat_guid") or "").strip()
    if chat_guid:
        target["chat_guid"] = chat_guid
    if not target:
        raise TransportError("incoming message has no existing chat target")
    return target


def _participants(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("participants", ())
    if value is None:
        value = ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TransportError("incoming participants must be an array")
    result = {
        _normalize_handle(item)
        for item in value
        if isinstance(item, (str, int))
        and not isinstance(item, bool)
        and str(item).strip()
    }
    return tuple(sorted(result))


def _roster_epoch(participant_subjects: Sequence[str]) -> str:
    return _opaque_subject("roster", *sorted(participant_subjects))


def _has_mention(text: str, tokens: Sequence[str]) -> bool:
    for token in tokens:
        escaped = re.escape(token)
        left = r"(?<![\w@])" if token[:1].isalnum() or token.startswith("@") else ""
        right = r"(?![\w])" if token[-1:].isalnum() else ""
        if re.search(left + escaped + right, text, flags=re.IGNORECASE):
            return True
    return False


def _known_outbound_guid(
    guid: str,
    outbound_guids: Collection[str] | Callable[[str], bool] | None,
) -> bool:
    if outbound_guids is None:
        return False
    try:
        if callable(outbound_guids):
            return bool(outbound_guids(guid))
        return guid in outbound_guids
    except Exception as error:
        raise TransportError("outbound GUID lookup failed closed") from error


def inspect_imsg_message(
    raw: Mapping[str, Any],
    cfg: Mapping[str, Any] | IMessageSettings,
    *,
    outbound_guids: Collection[str] | Callable[[str], bool] | None = None,
    platform: str | None = None,
) -> ParsedIMessage:
    """Classify one imsg row without putting its local rowid in the envelope."""

    settings = (
        cfg
        if isinstance(cfg, IMessageSettings)
        else validate_imessage_config(cfg, platform=platform)
    )
    if not isinstance(raw, Mapping):
        raise TransportError("incoming iMessage row must be an object")

    cursor = _read_cursor(raw)
    guid = str(raw.get("guid") or "").strip()
    if not guid or len(guid) > 512:
        raise TransportError("incoming message has no stable GUID")
    service = str(raw.get("service") or "").strip().casefold()
    if service != "imessage":
        raise TransportError("SMS and non-iMessage events are unsupported")
    is_from_me = _strict_bool(raw.get("is_from_me"), "is_from_me")
    is_group = _strict_bool(raw.get("is_group"), "is_group")
    target = _reply_target(raw)

    if is_from_me and _known_outbound_guid(guid, outbound_guids):
        return ParsedIMessage(
            cursor=cursor,
            envelope=None,
            outbound_guid=guid,
        )

    text_value = raw.get("text")
    if not isinstance(text_value, str):
        raise TransportError("attachment-only messages are unsupported")
    text = text_value.strip()
    if not text:
        raise TransportError("attachment-only or empty messages are unsupported")
    if len(text) > 4000:
        raise TransportError("incoming iMessage text exceeds its limit")

    sender = _normalize_handle(raw.get("sender"))
    participant_handles = set(_participants(raw))
    chat_guid = str(target.get("chat_guid") or "")

    if not is_group and chat_guid in settings.owner_chat_guids:
        if sender and sender not in settings.owner_handles:
            raise TransportError("owner self-chat sender is not enrolled")
        if participant_handles and not participant_handles.issubset(
            settings.owner_handles
        ):
            raise TransportError("owner self-chat participants are not enrolled")
        if not participant_handles:
            if sender:
                participant_handles.add(sender)
            elif is_from_me and len(settings.owner_handles) == 1:
                participant_handles.update(settings.owner_handles)
        if not participant_handles:
            raise TransportError("owner self-chat roster is incomplete")
        scope = "owner-private"
        principal_subject = settings.owner_subject
        conversation_subject = _opaque_subject(
            "conversation",
            settings.account_subject,
            "owner",
        )
    elif is_group:
        if is_from_me:
            raise TransportError("unrecorded local group messages fail closed")
        if not chat_guid or chat_guid not in settings.group_allowlist:
            raise TransportError("group chat is not explicitly allowed")
        if not sender or not participant_handles:
            raise TransportError("group sender or full roster is missing")
        if sender not in participant_handles:
            raise TransportError("group sender is not in the current roster")
        if not _has_mention(text, settings.mention_tokens):
            raise TransportError("group mention is required")
        scope = "group-shared"
        principal_subject = settings.principal_subject(sender)
        conversation_subject = _opaque_subject(
            "conversation",
            settings.account_subject,
            "group",
            chat_guid,
        )
    else:
        if is_from_me:
            raise TransportError("unrecorded local direct messages fail closed")
        if not sender or sender not in settings.dm_allowlist:
            raise TransportError("direct-message sender is not explicitly allowed")
        participant_handles.add(sender)
        scope = "principal-private"
        principal_subject = settings.principal_subject(sender)
        conversation_subject = _opaque_subject(
            "conversation",
            settings.account_subject,
            "dm",
            principal_subject,
        )

    participant_subjects = sorted(
        {
            settings.principal_subject(handle)
            for handle in participant_handles
        }
    )
    if not participant_subjects:
        raise TransportError("incoming participant roster is empty")
    if principal_subject not in participant_subjects:
        participant_subjects.append(principal_subject)
        participant_subjects.sort()
    target["service"] = "imessage"

    envelope: dict[str, Any] = {
        "schema": INBOUND_SCHEMA,
        "transport": "imessage",
        "remote_event_id": guid,
        "account_subject": settings.account_subject,
        "principal_subject": principal_subject,
        "conversation_subject": conversation_subject,
        "scope": scope,
        "participant_subjects": participant_subjects,
        "roster_epoch": _roster_epoch(participant_subjects),
        "text": text,
        "reply_target": target,
        "metadata": {
            "is_from_me": is_from_me,
            "bot_echo": False,
        },
    }
    created_at = str(raw.get("created_at") or "").strip()
    if created_at:
        envelope["remote_created_at"] = created_at
    validate_inbound_envelope(envelope)
    return ParsedIMessage(cursor=cursor, envelope=envelope)


def parse_imsg_message(
    raw: Mapping[str, Any],
    cfg: Mapping[str, Any] | IMessageSettings,
    *,
    outbound_guids: Collection[str] | Callable[[str], bool] | None = None,
    platform: str | None = None,
) -> dict[str, Any] | None:
    """Return only the canonical envelope; known outbound echoes return ``None``."""

    return inspect_imsg_message(
        raw,
        cfg,
        outbound_guids=outbound_guids,
        platform=platform,
    ).envelope


def message_cursor(raw: Mapping[str, Any]) -> int:
    """Return the local chat.db rowid for cursor bookkeeping only."""

    return _read_cursor(raw)


def _json_probe(stdout: object) -> bool:
    text = str(stdout or "").strip()
    if not text:
        return False
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        try:
            return all(json.loads(line) is not None for line in text.splitlines())
        except json.JSONDecodeError:
            return False


def preflight_imessage(
    cfg: Mapping[str, Any],
    *,
    platform: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Probe platform, pinned executable version, and read-only Messages access."""

    selected_platform = sys.platform if platform is None else platform
    errors: list[str] = []
    try:
        settings = validate_imessage_config(cfg, platform=selected_platform)
    except IMessageConfigError as error:
        return {
            "schema": PROFILE_SCHEMA,
            "ok": False,
            "enabled": cfg.get("imessage_enabled") is True
            if isinstance(cfg, Mapping)
            else False,
            "platform": selected_platform,
            "imsg_version": None,
            "read_ready": False,
            "errors": [str(error)],
        }

    version: str | None = None
    try:
        result = runner(
            [settings.imsg_path, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            check=False,
            shell=False,
        )
        output = str(result.stdout or "").strip()
        match = re.fullmatch(r"(?:imsg\s+)?([0-9]+\.[0-9]+\.[0-9]+)", output)
        version = match.group(1) if match else None
        if result.returncode != 0 or version != IMSG_PINNED_VERSION:
            errors.append(
                f"imsg version probe must report {IMSG_PINNED_VERSION}"
            )
    except (OSError, subprocess.SubprocessError, ValueError):
        errors.append("imsg executable version probe failed")

    read_ready = False
    if not errors:
        try:
            result = runner(
                [
                    settings.imsg_path,
                    "chats",
                    "--limit",
                    "1",
                    "--json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
                check=False,
                shell=False,
            )
            read_ready = result.returncode == 0 and _json_probe(result.stdout)
            if not read_ready:
                errors.append("imsg Messages read probe failed")
        except (OSError, subprocess.SubprocessError, ValueError):
            errors.append("imsg Messages read probe failed")

    return {
        "schema": PROFILE_SCHEMA,
        "ok": not errors,
        "enabled": True,
        "platform": selected_platform,
        "imsg_version": version,
        "read_ready": read_ready,
        "errors": errors,
    }


@dataclass
class _Pending:
    method: str
    event: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None
    write_started: bool = False
    written: bool = False
    flushed: bool = False


class ImsgRpcClient:
    """One supervised newline JSON-RPC child for ``imsg rpc --json``."""

    def __init__(
        self,
        imsg_path: str = "imsg",
        *,
        on_notification: Callable[[str, object], None] | None = None,
        on_diagnostic: Callable[[str], None] | None = None,
        default_timeout: float = 30.0,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout must be positive")
        self.imsg_path = imsg_path
        self.on_notification = on_notification
        self.on_diagnostic = on_diagnostic
        self.default_timeout = default_timeout
        self._popen_factory = popen_factory
        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._stopping = False
        self._close_error: BaseException | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            process = self._process
            return (
                process is not None
                and process.poll() is None
                and not self._closed.is_set()
            )

    @property
    def close_error(self) -> BaseException | None:
        return self._close_error

    def start(self) -> None:
        with self._state_lock:
            if self.is_running:
                return
            if self._process is not None or self._closed.is_set():
                raise ImsgClosed("an imsg RPC client instance cannot be restarted")
            try:
                process = self._popen_factory(
                    [self.imsg_path, "rpc", "--json"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    bufsize=1,
                    shell=False,
                )
            except (OSError, ValueError) as error:
                raise ImsgClosed("unable to start imsg rpc") from error
            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                try:
                    process.terminate()
                except OSError:
                    pass
                raise ImsgClosed("imsg rpc stdio pipes are unavailable")
            self._process = process
            self._reader = threading.Thread(
                target=self._read_stdout,
                name="rappter-imsg-stdout",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                name="rappter-imsg-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()

    def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> object:
        if not isinstance(method, str) or not method:
            raise ValueError("JSON-RPC method is required")
        if params is not None and not isinstance(params, Mapping):
            raise ValueError("JSON-RPC params must be an object")
        wait_timeout = self.default_timeout if timeout is None else timeout
        if wait_timeout <= 0:
            raise ValueError("timeout must be positive")

        with self._state_lock:
            process = self._process
            if (
                process is None
                or process.stdin is None
                or process.poll() is not None
                or self._closed.is_set()
            ):
                if method == "send":
                    raise RetryableSend("imsg send was definitely not written")
                raise ImsgClosed("imsg rpc is not running")
            request_id = self._next_id
            self._next_id += 1
            pending = _Pending(method=method)
            self._pending[request_id] = pending

        try:
            line = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": dict(params or {}),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        except (TypeError, ValueError) as error:
            self._remove_pending(request_id)
            if method == "send":
                raise RetryableSend(
                    "imsg send was definitely not written"
                ) from error
            raise TransportError("imsg request could not be encoded") from error
        if len(line) > MAX_RPC_LINE_CHARS:
            self._remove_pending(request_id)
            if method == "send":
                raise RetryableSend("imsg send was definitely not written")
            raise TransportError("imsg request exceeds its framing bound")

        try:
            with self._write_lock:
                with self._state_lock:
                    if (
                        self._process is not process
                        or process.poll() is not None
                        or self._closed.is_set()
                    ):
                        self._pending.pop(request_id, None)
                        if method == "send":
                            raise RetryableSend(
                                "imsg send was definitely not written"
                            )
                        raise ImsgClosed("imsg rpc closed before write")
                    pending.write_started = True
                written = process.stdin.write(line)
                if written is not None and written != len(line):
                    raise OSError("short imsg rpc write")
                pending.written = True
                process.stdin.flush()
                pending.flushed = True
        except RetryableSend:
            raise
        except (BrokenPipeError, OSError, ValueError) as error:
            self._remove_pending(request_id)
            self._terminate_process()
            terminal = ImsgClosed("imsg rpc stdin failed")
            self._fail_terminal(terminal)
            if method == "send":
                raise AmbiguousSend(
                    "imsg send write outcome is unknown"
                ) from error
            raise terminal from error

        if not pending.event.wait(wait_timeout):
            self._remove_pending(request_id)
            self._terminate_process()
            self._fail_terminal(
                ImsgClosed(f"imsg rpc request timed out ({method})")
            )
            if method == "send":
                if pending.flushed:
                    raise AmbiguousSend(
                        "imsg send timed out after request flush"
                    )
                if not pending.write_started:
                    raise RetryableSend(
                        "imsg send was definitely not written"
                    )
                raise AmbiguousSend("imsg send timeout outcome is unknown")
            raise TransportError(f"imsg rpc request timed out ({method})")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def wait_closed(
        self,
        timeout: float | None = None,
    ) -> BaseException | None:
        if not self._closed.wait(timeout):
            return None
        return self._close_error

    def stop(self) -> None:
        with self._state_lock:
            if self._stopping:
                return
            self._stopping = True
            process = self._process
        if process is None:
            self._fail_terminal(ImsgClosed("imsg rpc stopped"))
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        if process.poll() is None:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired, TypeError):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        self._fail_terminal(ImsgClosed("imsg rpc stopped"))
        current = threading.current_thread()
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not current:
                thread.join(timeout=0.5)

    close = stop

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while not self._closed.is_set():
                line = process.stdout.readline()
                if line == "":
                    break
                if (
                    len(line) > MAX_RPC_LINE_CHARS
                    or not line.endswith("\n")
                ):
                    self._protocol_failure(
                        "imsg rpc emitted an invalid JSON line"
                    )
                    return
                if line.strip():
                    self._handle_line(line)
        except (OSError, UnicodeError, ValueError) as error:
            self._protocol_failure(
                "imsg rpc stdout framing failed",
                error,
            )
            return
        if not self._closed.is_set():
            self._fail_terminal(ImsgClosed("imsg rpc child closed"))

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = process.stderr.readline()
                if line == "":
                    return
                if line.strip() and self.on_diagnostic:
                    self.on_diagnostic("imsg rpc diagnostic")
        except (OSError, UnicodeError, ValueError):
            if self.on_diagnostic:
                self.on_diagnostic("imsg rpc stderr unavailable")

    def _handle_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            self._protocol_failure("imsg rpc emitted malformed JSON", error)
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._protocol_failure(
                "imsg rpc emitted an invalid JSON-RPC envelope"
            )
            return

        if "id" in message and message["id"] is not None:
            response_id = message["id"]
            if isinstance(response_id, str) and response_id.isdigit():
                response_id = int(response_id)
            if type(response_id) is not int:
                self._protocol_failure("imsg rpc response id is invalid")
                return
            with self._state_lock:
                pending = self._pending.get(response_id)
            if pending is None:
                return
            has_result = "result" in message
            has_error = "error" in message and message["error"] is not None
            if has_result == has_error:
                self._protocol_failure(
                    "imsg rpc response must contain exactly one result or error"
                )
                return
            self._remove_pending(response_id)
            if has_error:
                error = TransportError("imsg rpc returned a remote error")
                pending.error = (
                    AmbiguousSend(
                        "imsg send returned no durable success evidence"
                    )
                    if pending.method == "send"
                    else error
                )
            else:
                pending.result = message["result"]
            pending.event.set()
            return

        method = message.get("method")
        if not isinstance(method, str) or not method:
            self._protocol_failure("imsg rpc notification has no method")
            return
        if self.on_notification:
            try:
                self.on_notification(method, message.get("params"))
            except Exception:
                if self.on_diagnostic:
                    self.on_diagnostic("imsg notification handler failed")

    def _protocol_failure(
        self,
        message: str,
        cause: BaseException | None = None,
    ) -> None:
        error = ImsgProtocolError(message)
        if cause is not None:
            error.__cause__ = cause
        self._terminate_process()
        self._fail_terminal(error)

    def _remove_pending(self, request_id: int) -> _Pending | None:
        with self._state_lock:
            return self._pending.pop(request_id, None)

    def _fail_terminal(self, error: BaseException) -> None:
        with self._state_lock:
            if self._closed.is_set():
                return
            self._close_error = error
            pending = list(self._pending.values())
            self._pending.clear()
            self._closed.set()
        for item in pending:
            if item.method == "send":
                if item.flushed or item.write_started:
                    item.error = AmbiguousSend(
                        "imsg send outcome is unknown after child failure"
                    )
                else:
                    item.error = RetryableSend(
                        "imsg send was definitely not written"
                    )
            else:
                item.error = error
            item.event.set()

    def _terminate_process(self) -> None:
        with self._state_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass


def send_imessage(
    rpc: Any,
    reply_target: Mapping[str, Any],
    text: str,
    *,
    timeout: float | None = None,
) -> str:
    """Send only to an existing chat, force iMessage, and return its GUID."""

    if not isinstance(reply_target, Mapping):
        raise TransportError("iMessage reply target must be an object")
    if not isinstance(text, str) or not text.strip():
        raise TransportError("iMessage outbound text must be non-empty")
    if str(reply_target.get("service") or "").strip().casefold() != "imessage":
        raise TransportError("iMessage reply target lacks verified iMessage service")
    target = _reply_target(reply_target)
    params = {
        **target,
        "text": text,
        "service": "imessage",
    }
    result = rpc.request("send", params, timeout=timeout)
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise AmbiguousSend(
            "imsg send returned no durable success evidence"
        )
    returned_service = result.get("service")
    if (
        returned_service is not None
        and str(returned_service).strip().casefold() != "imessage"
    ):
        raise AmbiguousSend("imsg send returned non-iMessage service evidence")
    guid = result.get("guid")
    if not isinstance(guid, str) or not guid.strip():
        raise AmbiguousSend("imsg send returned no durable outbound GUID")
    return guid.strip()


class IMessageFifoWorker:
    """A single daemon worker preserving submission order."""

    _STOP = object()

    def __init__(
        self,
        handler: Callable[[object], None],
        *,
        worker_count: int = 1,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        if type(worker_count) is not int or worker_count != 1:
            raise IMessageConfigError("iMessage FIFO worker count must be 1")
        self._handler = handler
        self._on_error = on_error
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="rappter-imessage-fifo",
            daemon=True,
        )
        self._thread.start()

    def submit(self, item: object) -> None:
        if self._thread is None or not self._thread.is_alive():
            raise TransportError("iMessage FIFO worker is not running")
        self._queue.put(item)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._queue.put(self._STOP)
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                self._handler(item)
            except Exception:
                if self._on_error:
                    self._on_error("iMessage FIFO handler failed")
            finally:
                self._queue.task_done()


class IMessageAdapter:
    """Config-gated parser and sender with injectable process seams."""

    def __init__(
        self,
        cfg: Mapping[str, Any],
        *,
        platform: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        rpc_client: Any | None = None,
        rpc: Any | None = None,
        outbound_guid_exists: Callable[[str], bool] | None = None,
        remember_outbound_guid: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.platform = sys.platform if platform is None else platform
        self.settings = validate_imessage_config(
            cfg,
            platform=self.platform,
        )
        self._runner = runner
        if rpc_client is not None and rpc is not None:
            raise ValueError("provide only one of rpc_client or rpc")
        selected_rpc = rpc_client if rpc_client is not None else rpc
        self.rpc_client = (
            selected_rpc
            if selected_rpc is not None
            else ImsgRpcClient(self.settings.imsg_path)
        )
        self._outbound_guid_exists = outbound_guid_exists
        self._remember_outbound_guid = remember_outbound_guid
        state_dir = Path(
            str(
                cfg.get("imessage_state_dir")
                or (
                    Path.home()
                    / ".rappter-chrome"
                    / "voice-twin"
                    / "imessage"
                )
            )
        ).expanduser()
        if not state_dir.is_absolute():
            raise IMessageConfigError("imessage_state_dir must be absolute")
        self._guid_store_path = state_dir / "outbound-guids.json"
        self._outbound_guids = _load_guid_store(self._guid_store_path)

    @property
    def worker_count(self) -> int:
        return self.settings.worker_count

    @property
    def writer_count(self) -> int:
        return self.settings.writer_count

    def preflight(self) -> dict[str, Any]:
        return preflight_imessage(
            self.cfg,
            platform=self.platform,
            runner=self._runner,
        )

    def start(self) -> None:
        result = self.preflight()
        if not result["ok"]:
            raise TransportError("; ".join(result["errors"]))
        self.rpc_client.start()

    def stop(self) -> None:
        stop = getattr(self.rpc_client, "stop", None)
        if callable(stop):
            stop()

    def _is_outbound_guid(self, guid: str) -> bool:
        if _guid_digest(guid) in self._outbound_guids:
            return True
        if self._outbound_guid_exists is None:
            return False
        try:
            return bool(self._outbound_guid_exists(guid))
        except Exception as error:
            raise TransportError("outbound GUID lookup failed closed") from error

    def record_outbound_guid(self, guid: str) -> None:
        if not isinstance(guid, str) or not guid.strip():
            raise TransportError("outbound GUID is invalid")
        value = guid.strip()
        self._outbound_guids.add(_guid_digest(value))
        _save_guid_store(self._guid_store_path, self._outbound_guids)
        if self._remember_outbound_guid is not None:
            try:
                self._remember_outbound_guid(value)
            except Exception as error:
                raise TransportError(
                    "outbound GUID persistence failed"
                ) from error

    def is_outbound_echo(self, raw: Mapping[str, Any]) -> bool:
        guid = str(raw.get("guid") or "").strip()
        return bool(
            guid
            and raw.get("is_from_me") is True
            and self._is_outbound_guid(guid)
        )

    def inspect_message(self, raw: Mapping[str, Any]) -> ParsedIMessage:
        return inspect_imsg_message(
            raw,
            self.settings,
            outbound_guids=self._is_outbound_guid,
        )

    def parse_message(self, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.inspect_message(raw).envelope

    parse = parse_message

    def send(
        self,
        reply_target: Mapping[str, Any],
        text: str,
        *,
        attempt_id: str | None = None,
        timeout: float | None = None,
    ) -> str:
        del attempt_id
        guid = send_imessage(
            self.rpc_client,
            reply_target,
            text,
            timeout=timeout,
        )
        try:
            self.record_outbound_guid(guid)
        except TransportError as exc:
            raise AmbiguousSend(
                "iMessage sent but outbound GUID persistence failed"
            ) from exc
        return guid

    send_message = send
    send_reply = send


IMessageTransport = IMessageAdapter
IMessageTransportAdapter = IMessageAdapter
ImessageTransport = IMessageAdapter
parse_inbound = parse_imsg_message
preflight = preflight_imessage
send_message = send_imessage
validate_config = validate_imessage_config


__all__ = [
    "IMSG_PINNED_VERSION",
    "INBOUND_SCHEMA",
    "PROFILE_SCHEMA",
    "IMessageAdapter",
    "IMessageConfigError",
    "IMessageFifoWorker",
    "IMessageSettings",
    "IMessageTransport",
    "IMessageTransportAdapter",
    "ImessageTransport",
    "ImsgClosed",
    "ImsgProtocolError",
    "ImsgRpcClient",
    "ParsedIMessage",
    "inspect_imsg_message",
    "message_cursor",
    "parse_imsg_message",
    "parse_inbound",
    "preflight",
    "preflight_imessage",
    "send_message",
    "send_imessage",
    "validate_config",
    "validate_imessage_config",
]
