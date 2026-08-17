#!/usr/bin/env python3
"""Persistent Google Voice channel for one hatched twin, locked account-to-peer.

The safe state transition is:

  read inbound -> call Copilot -> send -> verify in thread -> mark handled

If model generation or delivery fails, the inbound ID remains unhandled and a
later tick retries it. The assistant never marks an intention as a delivery.
"""

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bridge import BridgeError, Chrome  # noqa: E402
import gvoice  # noqa: E402
import voice_twin  # noqa: E402

CONFIG_FILE = Path.home() / ".rappter-chrome" / "config.json"
STATE_FILE = Path.home() / ".rappter-chrome" / "voice-assistant-state.json"
LOG_FILE = Path.home() / ".rappter-chrome" / "voice-assistant.log"
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3


def state_backup_path():
    return STATE_FILE.with_suffix(".json.bak")


def state_lock_path():
    return STATE_FILE.with_suffix(".json.lock")


def now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or now()).isoformat(timespec="seconds")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def default_state():
    return {
        "handled": [],
        "transcript": [],
        "conversation_binding": None,
        "replies": [],
        "initialized_at": None,
        "migration_notices": [],
        "message_absent": [],
        "message_rows": {},
        "message_rows_initialized": False,
        "message_sequence": 0,
        "message_truncated": [],
        "message_windows": {},
        "pending": None,
    }


def valid_conversation_binding(value):
    return (
        isinstance(value, dict)
        and set(value) == {"schema", "conversation_id", "audience_id"}
        and value.get("schema") == "rapp-messaging-bound-conversation/1.0"
        and re.fullmatch(
            r"conversation:[a-f0-9]{64}",
            str(value.get("conversation_id") or ""),
        )
        and re.fullmatch(
            r"audience:[a-f0-9]{64}",
            str(value.get("audience_id") or ""),
        )
    )


def fsync_directory(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def valid_state(data):
    pending = data.get("pending")
    pending_valid = pending is None or (
        isinstance(pending, dict)
        and re.fullmatch(r"[a-f0-9]{20}", str(pending.get("message_id") or ""))
        and isinstance(pending.get("inbound_text"), str)
        and len(pending["inbound_text"]) <= 4000
        and isinstance(pending.get("reply"), str)
        and len(pending["reply"]) <= 900
        and isinstance(pending.get("baseline"), int)
        and not isinstance(pending.get("baseline"), bool)
        and pending["baseline"] >= 0
        and isinstance(pending.get("created_at"), str)
        and pending.get("delivery_state") in (
            None,
            "prepared",
            "attempted",
            "unknown",
        )
        and (
            pending.get("attempted_at") is None
            or isinstance(pending.get("attempted_at"), str)
        )
        and (
            pending.get("conversation_binding") is None
            or valid_conversation_binding(pending.get("conversation_binding"))
        )
    )
    return (
        isinstance(data, dict)
        and isinstance(data.get("handled", []), list)
        and all(
            re.fullmatch(r"[a-f0-9]{20}", str(value or ""))
            for value in data.get("handled", [])
        )
        and isinstance(data.get("transcript", []), list)
        and all(
            isinstance(value, dict)
            and isinstance(value.get("role"), str)
            and len(value["role"]) <= 80
            and isinstance(value.get("text"), str)
            and len(value["text"]) <= 4000
            and isinstance(value.get("at"), str)
            for value in data.get("transcript", [])
        )
        and (
            data.get("conversation_binding") is None
            or valid_conversation_binding(data.get("conversation_binding"))
        )
        and isinstance(data.get("replies", []), list)
        and all(
            isinstance(value, dict)
            and re.fullmatch(
                r"[a-f0-9]{20}",
                str(value.get("message_id") or ""),
            )
            and isinstance(value.get("at"), str)
            for value in data.get("replies", [])
        )
        and isinstance(data.get("message_rows", {}), dict)
        and all(
            re.fullmatch(r"[a-f0-9]{24}", str(signature or ""))
            and isinstance(values, list)
            and all(
                re.fullmatch(r"[a-f0-9]{20}", str(value or ""))
                for value in values
            )
            for signature, values in data.get("message_rows", {}).items()
        )
        and isinstance(data.get("message_windows", {}), dict)
        and all(
            re.fullmatch(r"[a-f0-9]{24}", str(signature or ""))
            and isinstance(values, list)
            and all(
                re.fullmatch(r"[a-f0-9]{20}", str(value or ""))
                for value in values
            )
            for signature, values in data.get("message_windows", {}).items()
        )
        and isinstance(data.get("message_truncated", []), list)
        and all(
            re.fullmatch(r"[a-f0-9]{24}", str(value or ""))
            for value in data.get("message_truncated", [])
        )
        and isinstance(data.get("message_absent", []), list)
        and all(
            re.fullmatch(r"[a-f0-9]{24}", str(value or ""))
            for value in data.get("message_absent", [])
        )
        and isinstance(data.get("message_rows_initialized", False), bool)
        and isinstance(data.get("migration_notices", []), list)
        and all(
            re.fullmatch(r"[a-f0-9]{20}", str(value or ""))
            for value in data.get("migration_notices", [])
        )
        and isinstance(data.get("message_sequence", 0), int)
        and not isinstance(data.get("message_sequence", 0), bool)
        and data.get("message_sequence", 0) >= 0
        and pending_valid
    )


def _recover_state_from_backup(primary):
    try:
        backup_path = state_backup_path()
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        if not valid_state(backup):
            raise ValueError("backup has the wrong shape")
        recovered = {**default_state(), **backup}
        pending = recovered.get("pending")
        if pending:
            pending["delivery_state"] = "unknown"
            pending["attempted_at"] = pending.get("attempted_at") or iso()
        write_json_atomic(backup_path, recovered)
        write_json_atomic(STATE_FILE, recovered)
        log(f"recovered corrupt state from {backup_path.name}")
        return recovered
    except Exception as secondary:
        raise RuntimeError(
            f"state is unreadable and no valid backup exists: "
            f"{primary}; backup: {secondary}"
        ) from primary


def load_state():
    if not STATE_FILE.exists():
        if state_backup_path().exists():
            return _recover_state_from_backup(FileNotFoundError(STATE_FILE))
        return default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not valid_state(data):
            raise ValueError("state has the wrong shape")
        return {**default_state(), **data}
    except Exception as primary:
        return _recover_state_from_backup(primary)


def save_state(data):
    if not valid_state(data):
        raise RuntimeError("refusing to save invalid Voice state")
    if STATE_FILE.exists():
        try:
            current = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if valid_state(current):
                write_json_atomic(state_backup_path(), current)
        except Exception:
            # Preserve the last known-good backup rather than replacing it
            # with a corrupt current file.
            pass
    write_json_atomic(STATE_FILE, data)


@contextmanager
def tick_lock():
    lock_path = state_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def config():
    cfg = load_json(CONFIG_FILE, {})
    required = ("google_voice_account", "google_voice_peer")
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise RuntimeError(f"missing {', '.join(missing)} in {CONFIG_FILE}")
    account = str(cfg["google_voice_account"]).strip().lower()
    if not re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", account):
        raise RuntimeError("google_voice_account must be an email address")
    legacy_peer = str(cfg["google_voice_peer"]).strip()
    try:
        peer = gvoice.canonical_peer_number(legacy_peer)
    except BridgeError as exc:
        raise RuntimeError(f"invalid google_voice_peer: {exc}") from exc
    return {
        **cfg,
        "google_voice_account": account,
        "google_voice_peer": peer,
        "google_voice_peer_legacy": legacy_peer,
        "google_voice_owner": cfg.get("google_voice_owner", "the owner"),
        "google_voice_model": cfg.get("google_voice_model", "gpt-5.6-sol"),
        "max_replies_per_hour": int(cfg.get("max_replies_per_hour", 6)),
    }


def log(line):
    text = f"{iso()} {line}"
    print(text, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size >= MAX_LOG_BYTES:
            for index in range(LOG_BACKUPS, 1, -1):
                older = LOG_FILE.with_name(f"{LOG_FILE.name}.{index - 1}")
                newer = LOG_FILE.with_name(f"{LOG_FILE.name}.{index}")
                if older.exists():
                    os.replace(older, newer)
            os.replace(LOG_FILE, LOG_FILE.with_name(f"{LOG_FILE.name}.1"))
    except OSError:
        pass
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def normalize_number(value):
    try:
        return gvoice.canonical_peer_number(value)
    except BridgeError:
        return ""


def message_id(item):
    # innerText's first line changes from "10:30 PM" to "Aug 14" as a row
    # ages. The accessibility label carries the full absolute date and stays
    # stable, preventing yesterday's handled text from becoming new tomorrow.
    material = (
        f"{item['direction']}|{item['from']}|"
        f"{canonical_identity(item)}|{item.get('occurrence', 1)}"
    )
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def message_signature(item):
    material = (
        f"{item['direction']}|{item['from']}|"
        f"{canonical_identity(item)}|{safe_text(item.get('body'), 4000)}"
    )
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def assign_message_ids(state, items):
    rows = state.setdefault("message_rows", {})
    windows = state.setdefault("message_windows", {})
    truncated = set(state.get("message_truncated", []))
    absent = set(state.get("message_absent", []))
    notices = set(state.get("migration_notices", []))
    sequence = int(state.get("message_sequence", 0))
    grouped = {}
    for index, item in enumerate(items):
        grouped.setdefault(message_signature(item), []).append(index)
    assigned = [None] * len(items)
    changed = False
    absent.update(set(windows) - set(grouped))
    for signature, indexes in grouped.items():
        ledger = [
            value for value in rows.get(signature, [])
            if re.fullmatch(r"[a-f0-9]{20}", str(value or ""))
        ]
        previous = [
            value for value in windows.get(signature, [])
            if value in ledger
        ]
        if not previous and ledger:
            previous = ledger[-min(len(indexes), len(ledger)):]

        def allocate(count):
            nonlocal sequence
            values = []
            for _ in range(count):
                sequence += 1
                values.append(
                    hashlib.sha256(
                        f"{signature}|{sequence}".encode()
                    ).hexdigest()[:20]
                )
            ledger.extend(values)
            return values

        if signature in absent:
            current_ids = allocate(len(indexes))
            notices.update(current_ids)
            absent.remove(signature)
            truncated.discard(signature)
        elif not previous:
            current_ids = allocate(len(indexes))
        elif len(indexes) < len(previous):
            pending_id = (
                state.get("pending", {}).get("message_id")
                if isinstance(state.get("pending"), dict)
                else None
            )

            def disposition(stable_id):
                if stable_id in set(state.get("handled", [])):
                    return "handled"
                if stable_id == pending_id:
                    return "pending"
                if stable_id in notices:
                    return "notice"
                return "new"

            if len({disposition(value) for value in previous}) > 1:
                current_ids = allocate(len(indexes))
                notices.update(current_ids)
            else:
                current_ids = previous[-len(indexes):] if indexes else []
            truncated.add(signature)
        elif len(indexes) == len(previous):
            current_ids = previous
        elif signature in truncated:
            new_ids = allocate(len(indexes) - len(previous))
            current_ids = previous + new_ids
            notices.update(new_ids)
            truncated.remove(signature)
        else:
            current_ids = previous + allocate(len(indexes) - len(previous))
        if rows.get(signature) != ledger:
            rows[signature] = ledger
            changed = True
        if windows.get(signature) != current_ids:
            windows[signature] = current_ids
            changed = True
        for index, stable_id in zip(indexes, current_ids):
            assigned[index] = stable_id
    if state.get("message_sequence") != sequence:
        state["message_sequence"] = sequence
        changed = True
    for key, values in (
        ("message_truncated", sorted(truncated)),
        ("message_absent", sorted(absent)),
        ("migration_notices", sorted(notices)),
    ):
        if state.get(key, []) != values:
            state[key] = values
            changed = True
    return list(zip(assigned, items)), changed


def legacy_message_ids(item, cfg):
    values = {message_id(item)}
    legacy_from = item.get("_legacy_from") or cfg.get("google_voice_peer_legacy")
    if legacy_from and legacy_from != item.get("from"):
        values.add(message_id({**item, "from": legacy_from}))
    return values


def migrate_pending_message_id(state, inbound, cfg):
    """Map a pre-ledger pending ID before any delivery recovery side effect."""
    pending = state.get("pending")
    if not pending:
        return False, None
    pending_id = pending["message_id"]
    known_stable_ids = {
        stable_id
        for values in state.get("message_rows", {}).values()
        for stable_id in values
    }
    if pending_id in known_stable_ids:
        return False, None
    candidates = [
        stable_id
        for stable_id, item in inbound
        if pending_id in legacy_message_ids(item, cfg)
        and safe_text(item.get("body"), 4000) == pending["inbound_text"]
    ]
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        return False, (
            "legacy pending delivery could not be mapped uniquely; "
            "refusing recovery"
        )
    pending["message_id"] = candidates[0]
    return True, None


def canonical_identity(item):
    if item.get("identity"):
        return item["identity"]
    if item.get("label"):
        return item["label"]
    for line in str(item.get("raw") or "").splitlines():
        line = line.strip()
        if line.startswith("Message from "):
            return line
    return str(item.get("raw") or "")


def eligible(item, cfg):
    if item.get("direction") != "inbound":
        return False
    sender = normalize_number(item.get("from"))
    peer = normalize_number(cfg["google_voice_peer"])
    if not sender or not peer or sender != peer:
        return False
    text = item.get("body", "")
    return not re.search(
        r"verification code|security code|one[- ]time|do not share|\b2fa\b",
        text,
        re.I,
    )


def recent_reply_count(state):
    current = now()
    cutoff = current - timedelta(hours=1)
    future_limit = current + timedelta(minutes=5)
    count = 0
    for record in state.get("replies", []):
        try:
            recorded = datetime.fromisoformat(record["at"])
            if cutoff < recorded <= future_limit:
                count += 1
        except Exception:
            continue
    return count


def safe_text(value, limit):
    """Normalize untrusted SMS/model text and remove format controls."""
    normalized = unicodedata.normalize("NFC", str(value or ""))
    clean = "".join(
        char
        for char in normalized
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    return clean[:limit]


def respond(item, state, cfg):
    expected_binding = voice_twin.google_voice_conversation_binding(cfg)
    current_binding = state.get("conversation_binding")
    if current_binding is None:
        if state.get("transcript"):
            state["transcript"] = []
        state["conversation_binding"] = expected_binding
    elif current_binding != expected_binding:
        raise RuntimeError(
            "Google Voice conversation binding changed; explicit migration required"
        )
    return voice_twin.chat(
        item.get("_stable_message_id") or message_id(item),
        item.get("body", ""),
        state,
        cfg,
    )


def delivery_text(reply, stable_message_id):
    marker = f" [#{stable_message_id.upper()}]"
    clean = safe_text(reply, 900).strip()
    if clean.endswith(marker):
        return clean
    return clean[: 900 - len(marker)].rstrip() + marker


def collect(cfg):
    try:
        with Chrome() as chrome:
            tab = gvoice.open_voice(chrome)
            gvoice.open_thread(chrome, tab, cfg["google_voice_peer"])
            items = gvoice.messages_locked(
                chrome,
                tab,
                cfg["google_voice_peer"],
                cfg["google_voice_account"],
            )
            # Inside a directly addressed thread Voice abbreviates inbound
            # rows to "Message from ," and omits the number that was visible
            # in the list. The configured thread is authoritative here.
            for item in items:
                if item.get("direction") == "inbound" and not item.get("from"):
                    item["_legacy_from"] = cfg.get(
                        "google_voice_peer_legacy",
                        cfg["google_voice_peer"],
                    )
                    item["from"] = cfg["google_voice_peer"]
            return items
    except SystemExit as exc:
        raise RuntimeError(f"Google Voice read failed: {exc}") from exc


def deliver(cfg, text):
    try:
        with Chrome() as chrome:
            tab = gvoice.open_voice(chrome)
            result = gvoice.send(
                chrome,
                tab,
                cfg["google_voice_peer"],
                text,
                confirm=True,
            )
            if not result.get("verified"):
                raise RuntimeError("Google Voice did not confirm the reply")
    except SystemExit as exc:
        raise RuntimeError(f"Google Voice send failed: {exc}") from exc


def outbound_count(items, text):
    wanted = " ".join(safe_text(text, 900).split())
    return sum(
        1
        for item in items
        if item.get("direction") == "outbound"
        and " ".join(safe_text(item.get("body"), 900).split()) == wanted
    )


def legacy_outbound_count(items, text):
    return sum(
        1
        for item in items
        if item.get("direction") == "outbound"
        and item.get("body") == text
    )


def finalize_delivery(state, pending, handled_order, handled, cfg):
    mid = pending["message_id"]
    next_state = copy.deepcopy(state)
    next_handled_order = list(handled_order)
    next_handled = set(handled)
    if mid not in next_handled:
        next_handled_order.append(mid)
        next_handled.add(mid)
    next_state["handled"] = next_handled_order
    next_state.setdefault("transcript", []).extend(
        [
            {
                "role": cfg["google_voice_owner"],
                "text": safe_text(pending["inbound_text"], 4000),
                "at": pending["created_at"],
            },
            {
                "role": "Voice Twin",
                "text": safe_text(pending["reply"], 900),
                "at": iso(),
            },
        ]
    )
    next_state["transcript"] = next_state["transcript"][-40:]
    next_state.setdefault("replies", []).append({"at": iso(), "message_id": mid})
    next_state["replies"] = next_state["replies"][-100:]
    next_state["migration_notices"] = [
        value
        for value in next_state.get("migration_notices", [])
        if value != mid
    ]
    next_state["pending"] = None
    save_state(next_state)
    state.clear()
    state.update(next_state)
    handled_order[:] = next_handled_order
    handled.clear()
    handled.update(next_handled)


def _tick(*, reply_latest=False, responder=None, sender=deliver):
    responder = responder or respond
    cfg = config()
    items = collect(cfg)
    state = load_state()
    expected_binding = voice_twin.google_voice_conversation_binding(cfg)
    state_binding = state.get("conversation_binding")
    if state_binding is not None and state_binding != expected_binding:
        log("Google Voice conversation binding changed; refusing all processing")
        return 0
    if (
        state_binding is None
        and not state.get("pending")
    ):
        state["transcript"] = []
        state["conversation_binding"] = expected_binding
        state_binding = expected_binding
        save_state(state)
    handled_order = list(dict.fromkeys(state.get("handled", [])))
    handled = set(handled_order)
    inbound_items = [item for item in items if eligible(item, cfg)]
    rows_initialized = state.get("message_rows_initialized", False)
    inbound, rows_changed = assign_message_ids(state, inbound_items)
    pending_changed, pending_problem = migrate_pending_message_id(
        state,
        inbound,
        cfg,
    )
    if pending_problem:
        if rows_changed:
            save_state(state)
        log(pending_problem)
        return 0
    rows_changed = rows_changed or pending_changed
    migration_notices = set(state.get("migration_notices", []))
    if not rows_initialized:
        state["message_rows_initialized"] = True
        if state.get("initialized_at"):
            ambiguous = []
            for mid, item in inbound:
                if legacy_message_ids(item, cfg) & handled:
                    ambiguous.append((mid, item))
            for mid, _ in ambiguous[:-1]:
                if mid not in handled:
                    handled_order.append(mid)
                    handled.add(mid)
            if ambiguous:
                migration_notices.add(ambiguous[-1][0])
            state["migration_notices"] = sorted(migration_notices)
            state["handled"] = handled_order
            save_state(state)
            log(
                f"migrated stable message IDs: mapped "
                f"{len(handled)} handled inbound messages"
            )
    elif rows_changed:
        # Persist new stable IDs before generation or side effects so a crash
        # cannot assign different identities to the same visible rows.
        save_state(state)

    pending = state.get("pending")
    if pending:
        if (
            state_binding != expected_binding
            or pending.get("conversation_binding") != expected_binding
        ):
            pending["delivery_state"] = "unknown"
            pending["attempted_at"] = pending.get("attempted_at") or iso()
            save_state(state)
            log(
                "pending delivery has no matching conversation binding; "
                "refusing readback or send"
            )
            return 0
        count_delivery = (
            legacy_outbound_count
            if pending.get("delivery_state") is None
            else outbound_count
        )
        already_landed = (
            count_delivery(items, pending["reply"]) > pending["baseline"]
        )
        if already_landed:
            finalize_delivery(state, pending, handled_order, handled, cfg)
            log(f"recovered confirmed delivery: {pending['message_id']}")
        elif pending.get("delivery_state") in ("attempted", "unknown", None):
            if pending.get("delivery_state") != "unknown":
                pending["delivery_state"] = "unknown"
                save_state(state)
            log(
                f"pending delivery remains ambiguous for "
                f"{pending['message_id']}; refusing automatic resend"
            )
            return 0
        else:
            try:
                pending["delivery_state"] = "attempted"
                pending["attempted_at"] = iso()
                save_state(state)
                sender(cfg, pending["reply"])
                finalize_delivery(state, pending, handled_order, handled, cfg)
                log(f"verified prepared delivery: {pending['message_id']}")
            except Exception as exc:
                pending["delivery_state"] = "unknown"
                save_state(state)
                log(
                    f"pending delivery became ambiguous for "
                    f"{pending['message_id']}: {type(exc).__name__}: {exc}"
                )
                return 0
        # Re-collect before considering other messages because the recovered
        # send changed the thread and therefore outbound baselines.
        items = collect(cfg)

    if not state.get("initialized_at"):
        state["initialized_at"] = iso()
        if reply_latest and inbound:
            for mid, _ in inbound[:-1]:
                if mid not in handled:
                    handled_order.append(mid)
                    handled.add(mid)
        else:
            for mid, _ in inbound:
                if mid not in handled:
                    handled_order.append(mid)
                    handled.add(mid)
        # Do not truncate this watermark. A thread with >500 historical
        # messages otherwise reclassifies its oldest rows as new on tick two.
        # Twenty-character IDs remain small even for years of conversation.
        state["handled"] = handled_order
        save_state(state)
        if not reply_latest:
            log(f"initialized: watermarked {len(inbound)} existing inbound messages")
            return 0

    candidates = [(mid, item) for mid, item in inbound if mid not in handled]
    if not candidates:
        log("no new inbound messages")
        return 0

    budget = cfg["max_replies_per_hour"] - recent_reply_count(state)
    if budget <= 0:
        log("reply rate limit reached; leaving messages unhandled")
        return 0

    replied = 0
    for mid, item in candidates[:budget]:
        try:
            if mid in migration_notices:
                reply = (
                    "Upgrade safety check: I could not prove whether your "
                    "latest visible message was already handled. No command "
                    "was run. Please resend anything still pending."
                )
            else:
                reply = responder(
                    {**item, "_stable_message_id": mid},
                    state,
                    cfg,
                )
            reply = delivery_text(reply, mid)
        except Exception as exc:
            log(f"reply generation failed for {mid}: {type(exc).__name__}: {exc}")
            break

        pending = {
            "message_id": mid,
            "inbound_text": safe_text(item["body"], 4000),
            "reply": safe_text(reply, 900),
            "baseline": outbound_count(items, reply),
            "created_at": iso(),
            "delivery_state": "prepared",
            "attempted_at": None,
            "conversation_binding": state["conversation_binding"],
        }
        state["pending"] = pending
        # The intent is durable BEFORE the irreversible send. A crash after
        # delivery but before finalization is recovered by readback.
        save_state(state)
        try:
            pending["delivery_state"] = "attempted"
            pending["attempted_at"] = iso()
            save_state(state)
            sender(cfg, pending["reply"])
            finalize_delivery(state, pending, handled_order, handled, cfg)
        except Exception as exc:
            pending["delivery_state"] = "unknown"
            save_state(state)
            log(
                f"delivery unconfirmed for {mid}: {type(exc).__name__}: {exc}; "
                "durable unknown delivery retained without automatic resend"
            )
            break
        replied += 1
        log(f"replied and verified: {mid}")
        # The verified send changed the outbound baseline. Re-read before
        # another identical reply can reserve a stale count and later be
        # mistaken for a delivery that never happened.
        items = collect(cfg)
    return replied


def tick(*, reply_latest=False, responder=None, sender=deliver):
    with tick_lock() as acquired:
        if not acquired:
            log("another Voice tick owns the state lock — skipping")
            return 0
        return _tick(
            reply_latest=reply_latest,
            responder=responder,
            sender=sender,
        )


def run_loop(interval):
    log(f"voice assistant started; interval={interval}s")
    while True:
        try:
            tick()
        except (BridgeError, RuntimeError) as exc:
            log(f"tick unavailable: {type(exc).__name__}: {exc}")
        except Exception as exc:
            log(f"tick error: {type(exc).__name__}: {exc}")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--reply-latest", action="store_true")
    args = parser.parse_args()
    try:
        # Validate once before entering a resident loop. A malformed fresh
        # setup should print one useful error and exit, not traceback or log
        # the same missing key forever.
        config()
        if args.loop:
            run_loop(max(30, args.interval))
            return 0
        return 0 if tick(reply_latest=args.reply_latest) >= 0 else 1
    except (BridgeError, RuntimeError) as exc:
        print(f"voice assistant: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
