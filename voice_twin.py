#!/usr/bin/env python3
"""Durable owner-private Brainstem twin behind the Google Voice transport."""

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import gvoice
import rapp1

HERE = Path(__file__).resolve().parent
ROOT = Path.home() / ".rappter-chrome"
TWIN_ROOT = ROOT / "voice-twin"
AGENTS_DIR = TWIN_ROOT / "agents"
TURN_DIR = TWIN_ROOT / "turns"
FRAME_DIR = TWIN_ROOT / "frames"
IDENTITY_FILE = TWIN_ROOT / "rappid.json"
INSTALLATION_FILE = TWIN_ROOT / "installation.json"
TRANSPORT_FILE = TWIN_ROOT / "transport-binding.json"
SECRET_FILE = TWIN_ROOT / "transport-binding.key"
MEMORY_FILE = TWIN_ROOT / "memory.json"
LOCK_FILE = TWIN_ROOT / ".twin.lock"
CONFORMANCE_FILE = HERE / "VOICE_TWIN_CONFORMANCE.json"
SOUL_FILE = HERE / "voice_twin_soul.md"
AGENT_FILE = HERE / "voice_twin_agent.py"

CURATED_AGENT_FILES = (
    "base64_agent.py",
    "caseconvert_agent.py",
    "epochconvert_agent.py",
    "hacker_news_agent.py",
    "hashtext_agent.py",
    "hexconvert_agent.py",
    "rot13_agent.py",
    "slugifytext_agent.py",
    "textstats_agent.py",
    "urlcode_agent.py",
    "uuidgenerator_agent.py",
)
CURATED_TOOL_NAMES = (
    "Base64",
    "CaseConvert",
    "EpochConvert",
    "HackerNews",
    "HashText",
    "HexConvert",
    "Rot13",
    "SlugifyText",
    "TextStats",
    "UrlCode",
    "UUIDGenerator",
    "VoiceTwin",
)
ACTION_CLAIM = re.compile(
    r"\b(?:i\s+)?(?:ran|executed|changed|modified|edited|fixed|deleted|"
    r"created|committed|pushed|deployed|sent|opened|navigated|fetched|"
    r"retrieved|started|stopped|restarted|remembered)\b",
    re.I,
)
FETCH_CLAIM = re.compile(r"\b(?:fetched|retrieved)\b", re.I)
MEMORY_CLAIM = re.compile(r"\bremembered\b", re.I)
_RUNNER = r"""
import json
import os
import sys

source, request_path, result_path = sys.argv[1:4]
sys.path.insert(0, source)
import brainstem

with open(request_path, encoding="utf-8") as handle:
    payload = json.load(handle)
with brainstem.app.test_client() as client:
    response = client.post(
        "/chat",
        data=json.dumps(payload, ensure_ascii=False),
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    value = {
        "status": response.status_code,
        "body": response.get_json(silent=True),
    }
with open(result_path, "w", encoding="utf-8") as handle:
    json.dump(value, handle, ensure_ascii=False)
    handle.flush()
    os.fsync(handle.fileno())
"""


def safe_text(value, limit):
    normalized = unicodedata.normalize("NFC", str(value or ""))
    clean = "".join(
        char
        for char in normalized
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    return clean[:limit]


def _utc():
    value = datetime.now(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _fsync_directory(path):
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_atomic(path, payload, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path, value):
    _write_bytes_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _read_json(path):
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_size > 8 * 1024 * 1024:
        raise RuntimeError(f"Voice Twin state exceeds its limit: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Voice Twin state is unreadable: {path.name}") from exc


@contextmanager
def twin_lock():
    TWIN_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(TWIN_ROOT, 0o700)
    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    os.chmod(LOCK_FILE, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _brainstem_paths(cfg):
    root = Path(
        str(cfg.get("brainstem_root") or (Path.home() / ".brainstem"))
    ).expanduser()
    if not root.is_absolute():
        raise RuntimeError("brainstem_root must be absolute")
    source = root / "src" / "rapp_brainstem"
    python = root / "venv" / "bin" / "python"
    required = [python, source / "brainstem.py"]
    required.extend(source / "agents" / name for name in CURATED_AGENT_FILES)
    if not all(path.is_file() for path in required):
        raise RuntimeError(f"curated Brainstem runtime is unavailable under {root}")
    return source, python


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare_agents(cfg):
    source, python = _brainstem_paths(cfg)
    wanted = {
        name: _sha256(source / "agents" / name)
        for name in CURATED_AGENT_FILES
    }
    wanted["voice_twin_agent.py"] = _sha256(AGENT_FILE)
    manifest = {
        "schema": "rapp-voice-twin-agents/1.0",
        "agents": wanted,
        "excluded_capabilities": [
            "arbitrary shell",
            "dynamic agent creation",
            "filesystem editing",
            "package installation",
            "unrestricted browser control",
            "unrestricted messaging",
        ],
    }
    existing = _read_json(AGENTS_DIR / "manifest.json")
    if existing == manifest and all(
        (AGENTS_DIR / name).is_file() for name in wanted
    ):
        return source, python

    TWIN_ROOT.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".agents-stage-", dir=TWIN_ROOT))
    backup = TWIN_ROOT / f".agents-backup-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        for name in CURATED_AGENT_FILES:
            shutil.copy2(source / "agents" / name, stage / name)
            os.chmod(stage / name, 0o600)
        shutil.copy2(AGENT_FILE, stage / "voice_twin_agent.py")
        os.chmod(stage / "voice_twin_agent.py", 0o600)
        _write_json_atomic(stage / "manifest.json", manifest)
        _fsync_directory(stage)
        if AGENTS_DIR.exists():
            AGENTS_DIR.rename(backup)
        stage.rename(AGENTS_DIR)
        _fsync_directory(TWIN_ROOT)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if not AGENTS_DIR.exists() and backup.exists():
            backup.rename(AGENTS_DIR)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return source, python


def ensure_identity(cfg):
    owner = str(cfg.get("rapp_owner") or "kody-w").strip().lower()
    slug = str(cfg.get("voice_twin_slug") or "voice-twin").strip().lower()
    label = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    if not label.fullmatch(owner) or len(owner) > 39:
        raise RuntimeError("rapp_owner is not a canonical GitHub login")
    if not label.fullmatch(slug) or len(slug) > 100:
        raise RuntimeError("voice_twin_slug is not canonical")
    existing = _read_json(IDENTITY_FILE)
    prefix = f"rappid:@{owner}/{slug}:"
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or set(existing) != {"schema", "rappid"}
            or existing.get("schema") != "rapp/1"
            or not rapp1.rappid_valid(existing.get("rappid", ""))
            or not existing["rappid"].startswith(prefix)
        ):
            raise RuntimeError("Voice Twin rappid is invalid or belongs elsewhere")
        return existing["rappid"]
    rappid = rapp1.mint_rappid(owner, slug)
    identity = {"schema": "rapp/1", "rappid": rappid}
    _write_bytes_atomic(
        IDENTITY_FILE,
        rapp1.canonical(identity).encode("utf-8"),
    )
    return rappid


def _secret():
    if SECRET_FILE.exists():
        value = SECRET_FILE.read_bytes()
        if len(value) != 32:
            raise RuntimeError("Voice Twin transport key is invalid")
        os.chmod(SECRET_FILE, 0o600)
        return value
    value = secrets.token_bytes(32)
    _write_bytes_atomic(SECRET_FILE, value)
    return value


def _private_id(secret, label, value):
    digest = hmac.new(
        secret,
        f"{label}\n{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{label}:{digest}"


def transport_context(cfg, rappid, message_id):
    account = str(cfg["google_voice_account"]).strip().lower()
    peer = gvoice.canonical_peer_number(cfg["google_voice_peer"])
    secret = _secret()
    installation = _read_json(INSTALLATION_FILE)
    if installation is None:
        installation = {
            "schema": "rapp-installation/1.0",
            "installation_id": "installation:" + secrets.token_hex(32),
        }
        _write_json_atomic(INSTALLATION_FILE, installation)
    if (
        not isinstance(installation, dict)
        or installation.get("schema") != "rapp-installation/1.0"
        or not re.fullmatch(
            r"installation:[0-9a-f]{64}",
            str(installation.get("installation_id") or ""),
        )
    ):
        raise RuntimeError("Voice Twin installation identity is invalid")

    account_id = _private_id(secret, "account", account)
    principal_id = _private_id(secret, "principal", peer)
    conversation_id = _private_id(secret, "conversation", f"{account}\n{peer}")
    audience_id = _private_id(secret, "audience", conversation_id)
    binding_id = _private_id(secret, "binding", f"{account}\n{peer}\n{rappid}")
    binding = {
        "schema": "rapp-messaging-transport-binding/1.0",
        "installation_id": installation["installation_id"],
        "rappter_id": rappid,
        "transport": "google-voice-web",
        "account_id": account_id,
        "binding_id": binding_id,
        "role": "owner",
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    existing = _read_json(TRANSPORT_FILE)
    if existing is None:
        _write_json_atomic(TRANSPORT_FILE, binding)
    elif any(
        existing.get(key) != binding[key]
        for key in (
            "schema",
            "installation_id",
            "rappter_id",
            "transport",
            "account_id",
            "binding_id",
            "role",
            "active",
        )
    ):
        raise RuntimeError("Voice Twin transport binding changed; refusing")

    context = {
        "schema": "rapp-messaging-conversation/1.0",
        "conversation_id": conversation_id,
        "soul_id": rappid,
        "scope": "owner-private",
        "principal_id": principal_id,
        "audience_id": audience_id,
        "participant_ids": [principal_id],
        "roster_epoch": "owner-private-1",
        "source_event_id": message_id,
        "allowed_tools": list(CURATED_TOOL_NAMES),
    }
    return binding, context


def _history(conversation_state):
    output = []
    marker = re.compile(r"\s+\[#[A-F0-9]{20}\]$")
    for row in conversation_state.get("transcript", [])[-12:]:
        if not isinstance(row, dict):
            continue
        role = (
            "assistant"
            if row.get("role") in ("Copilot", "Voice Twin")
            else "user"
        )
        content = safe_text(row.get("text"), 2000).strip()
        if role == "assistant":
            content = marker.sub("", content)
        if content:
            output.append({"role": role, "content": content})
    return output


def _clean_env(cfg, source, context, rappid, soul_path):
    allowed = (
        "HOME",
        "PATH",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SHELL",
        "USER",
        "LOGNAME",
        "SSH_AUTH_SOCK",
        "GH_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "SSL_CERT_FILE",
    )
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", os.defpath)
    env.update({
        "AGENTS_PATH": str(AGENTS_DIR),
        "BRAINSTEM_LAN_MODE": "false",
        "GITHUB_MODEL": str(cfg.get("google_voice_model") or "gpt-5.6-sol"),
        "PYTHONNOUSERSITE": "1",
        "SOUL_PATH": str(soul_path),
        "VOICE_MODE": "false",
        "VOICE_TWIN_AUDIENCE_ID": context["audience_id"],
        "VOICE_TWIN_CONVERSATION_ID": context["conversation_id"],
        "VOICE_TWIN_EVENT_ID": context["source_event_id"],
        "VOICE_TWIN_MEMORY_FILE": str(MEMORY_FILE),
        "VOICE_TWIN_PRINCIPAL_ID": context["principal_id"],
        "VOICE_TWIN_RAPPID": rappid,
    })
    return env


def successful_agent_names(logs):
    if not isinstance(logs, str):
        return []
    starts = list(re.finditer(
        r"(?m)^\[([A-Za-z][A-Za-z0-9_-]{0,63})\]\s*",
        logs,
    ))
    successful = set()
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(logs)
        result = logs[match.end():end].strip()
        lowered = result.lower()
        if (
            not result
            or lowered.startswith(("error", "refused", "unavailable"))
        ):
            continue
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").lower()
            if payload.get("ok") is False or status in {
                "error",
                "failed",
                "refused",
                "unavailable",
            }:
                continue
        successful.add(match.group(1))
    return sorted(successful)


def action_claim_supported(reply, agent_names):
    if not ACTION_CLAIM.search(reply):
        return True
    available = set(agent_names)
    if MEMORY_CLAIM.search(reply):
        return "VoiceTwin" in available
    if FETCH_CLAIM.search(reply):
        return bool(available & {"HackerNews", "VoiceTwin"})
    return False


def _run_twin(message_id, text, conversation_state, cfg, rappid, context):
    source, python = prepare_agents(cfg)
    request = {
        "user_input": safe_text(text, 4000),
        "conversation_history": _history(conversation_state),
        "session_id": context["conversation_id"],
        "idempotency_key": message_id,
    }
    soul = SOUL_FILE.read_text(encoding="utf-8")
    soul += (
        "\n\n<conversation-context-json>\n"
        + json.dumps(context, ensure_ascii=True, separators=(",", ":"))
        + "\n</conversation-context-json>\n"
    )
    request_fd, request_name = tempfile.mkstemp(
        prefix=".request-",
        suffix=".json",
        dir=TWIN_ROOT,
    )
    result_fd, result_name = tempfile.mkstemp(
        prefix=".result-",
        suffix=".json",
        dir=TWIN_ROOT,
    )
    soul_fd, soul_name = tempfile.mkstemp(
        prefix=".soul-",
        suffix=".md",
        dir=TWIN_ROOT,
    )
    os.close(result_fd)
    try:
        os.fchmod(request_fd, 0o600)
        with os.fdopen(request_fd, "w", encoding="utf-8") as handle:
            json.dump(request, handle, ensure_ascii=False)
        os.fchmod(soul_fd, 0o600)
        with os.fdopen(soul_fd, "w", encoding="utf-8") as handle:
            handle.write(soul)
        result = subprocess.run(
            [
                str(python),
                "-c",
                _RUNNER,
                str(source),
                request_name,
                result_name,
            ],
            capture_output=True,
            text=True,
            timeout=int(cfg.get("voice_twin_timeout_seconds", 240)),
            cwd=TWIN_ROOT,
            env=_clean_env(cfg, source, context, rappid, soul_name),
        )
        if result.returncode != 0:
            raise RuntimeError("Brainstem twin process failed")
        value = _read_json(result_name)
        if (
            not isinstance(value, dict)
            or value.get("status") != 200
            or not isinstance(value.get("body"), dict)
            or not isinstance(value["body"].get("response"), str)
        ):
            raise RuntimeError("Brainstem twin returned no verified response")
        reply = safe_text(value["body"]["response"], 850).strip()
        if not reply:
            raise RuntimeError("Brainstem twin returned an empty response")
        logs = value["body"].get("agent_logs", "")
        if isinstance(logs, list):
            logs = "\n".join(str(item) for item in logs)
        if not isinstance(logs, str):
            logs = ""
        agent_names = successful_agent_names(logs)
        if not action_claim_supported(reply, agent_names):
            reply = (
                "The twin could not verify that computer action, so it was "
                "not reported as complete."
            )
        return reply, agent_names
    finally:
        Path(request_name).unlink(missing_ok=True)
        Path(result_name).unlink(missing_ok=True)
        Path(soul_name).unlink(missing_ok=True)


def _turn_path(message_id):
    if not re.fullmatch(r"[a-f0-9]{20}", str(message_id or "")):
        raise RuntimeError("Voice Twin message identity is invalid")
    return TURN_DIR / f"{message_id}.json"


def _load_turn(message_id):
    value = _read_json(_turn_path(message_id))
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != "rapp-voice-twin-turn/1.0"
        or value.get("message_id") != message_id
        or value.get("status") not in ("executing", "completed")
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("request_hash") or ""))
        or not isinstance(value.get("user_input"), str)
        or (
            value.get("status") == "completed"
            and (
                not isinstance(value.get("response"), str)
                or not isinstance(value.get("agent_names"), list)
            )
        )
    ):
        raise RuntimeError("Voice Twin turn record is invalid")
    return value


def _write_turn(record):
    TURN_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_turn_path(record["message_id"]), record)


def _load_frames(expected_stream_id):
    frames = []
    if not FRAME_DIR.exists():
        return frames
    head = None
    for path in sorted(FRAME_DIR.glob("*.json")):
        if not re.fullmatch(r"\d{20}\.json", path.name):
            raise RuntimeError("Voice Twin frame directory contains an invalid file")
        frame = _read_json(path)
        if not isinstance(frame, dict):
            raise RuntimeError("Voice Twin frame is invalid")
        ok, step, reason = rapp1.verify_frame(
            frame,
            head=head,
            stream_id_of_record=expected_stream_id,
        )
        if not ok or frame.get("seq") != len(frames):
            raise RuntimeError(
                f"Voice Twin frame failed RAPP/1 step {step}: {reason}"
            )
        frames.append(frame)
        head = frame
    return frames


def _ensure_frame(record, rappid):
    stream_id = f"{rappid}:google-voice"
    frames = _load_frames(stream_id)
    for frame in frames:
        if frame["payload"].get("message_id") == record["message_id"]:
            if frame["payload"].get("response") != record["response"]:
                raise RuntimeError("Voice Twin turn conflicts with its RAPP/1 frame")
            return frame["frame_hash"]
    head = frames[-1] if frames else None
    utc = _utc()
    if head and utc < head["utc"]:
        utc = head["utc"]
    frame = rapp1.build_frame(
        "memory.chat-turn",
        stream_id,
        len(frames),
        utc,
        {
            "agent_names": record["agent_names"],
            "message_id": record["message_id"],
            "response": record["response"],
            "scope": "owner-private",
            "user_input": record["user_input"],
        },
        prev=head["payload_hash"] if head else None,
    )
    ok, step, reason = rapp1.verify_frame(
        frame,
        head=head,
        stream_id_of_record=stream_id,
    )
    if not ok:
        raise RuntimeError(f"new Voice Twin frame failed step {step}: {reason}")
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(
        FRAME_DIR / f"{frame['seq']:020d}.json",
        rapp1.canonical(frame).encode("utf-8"),
    )
    return frame["frame_hash"]


def chat(message_id, text, conversation_state, cfg):
    """Return one durable twin result for one stable inbound message."""
    message_id = str(message_id or "")
    user_input = safe_text(text, 4000).strip()
    if not user_input:
        raise RuntimeError("Voice Twin input is empty")
    request_hash = hashlib.sha256(user_input.encode("utf-8")).hexdigest()

    with twin_lock():
        rappid = ensure_identity(cfg)
        _, context = transport_context(cfg, rappid, message_id)
        record = _load_turn(message_id)
        if record is not None:
            if record["request_hash"] != request_hash:
                raise RuntimeError("Voice Twin message identity was reused")
            if record["status"] == "executing":
                record.update({
                    "status": "completed",
                    "response": (
                        "The prior twin turn was interrupted before a verifiable "
                        "result. No action was retried. Please resend the request."
                    ),
                    "agent_names": [],
                    "completed_at": _utc(),
                    "outcome": "interrupted",
                })
                _write_turn(record)
            frame_hash = _ensure_frame(record, rappid)
            if record.get("frame_hash") != frame_hash:
                record["frame_hash"] = frame_hash
                _write_turn(record)
            return record["response"]
        record = {
            "schema": "rapp-voice-twin-turn/1.0",
            "message_id": message_id,
            "request_hash": request_hash,
            "user_input": user_input,
            "status": "executing",
            "created_at": _utc(),
        }
        _write_turn(record)

    try:
        response, agent_names = _run_twin(
            message_id,
            user_input,
            conversation_state,
            cfg,
            rappid,
            context,
        )
        outcome = "completed"
    except Exception:
        response = (
            "The twin was unavailable before a verifiable result. No action "
            "was retried. Please resend the request."
        )
        agent_names = []
        outcome = "failed"

    with twin_lock():
        current = _load_turn(message_id)
        if current is None or current["request_hash"] != request_hash:
            raise RuntimeError("Voice Twin turn reservation was lost")
        if current["status"] == "completed":
            return current["response"]
        current.update({
            "status": "completed",
            "response": response,
            "agent_names": agent_names,
            "completed_at": _utc(),
            "outcome": outcome,
        })
        _write_turn(current)
        frame_hash = _ensure_frame(current, rappid)
        current["frame_hash"] = frame_hash
        _write_turn(current)
        return response
