#!/usr/bin/env python3
"""Fail-closed WhatsApp Cloud API webhook and outbound transport helpers."""

from __future__ import annotations

import hmac
import json
import math
import re
import socket
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib import error, parse, request

from messaging_transport import (
    AmbiguousSend,
    RetryableSend,
    TransportError,
    validate_inbound_envelope,
)


TRANSPORT = "whatsapp-cloud"
GRAPH_API_ORIGIN = "https://graph.facebook.com"
MAX_WEBHOOK_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

_GRAPH_VERSION = re.compile(r"^v[1-9][0-9]*\.[0-9]+$")
_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WAMID = re.compile(r"^wamid\.[!-~]{1,500}$")
_BSUID = re.compile(r"^[A-Z]{2}\.(?:ENT\.)?[A-Za-z0-9]{1,128}$")
_RATE_LIMIT_CODES = frozenset(
    {
        4,
        17,
        32,
        613,
        80007,
        130429,
        131048,
        131056,
    }
)


def whatsapp_enabled(cfg):
    """Return true only for the transport's explicit boolean enable switch."""
    return isinstance(cfg, Mapping) and cfg.get("whatsapp_enabled") is True


def _require_enabled(cfg):
    if not whatsapp_enabled(cfg):
        raise TransportError("WhatsApp transport is disabled")


def _bounded_string(value, field, *, limit=512):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _optional_string(value, field, *, limit=512):
    if value is None:
        return None
    return _bounded_string(value, field, limit=limit)


def validate_wamid(value):
    if not isinstance(value, str) or not _WAMID.fullmatch(value):
        raise ValueError("WhatsApp message ID is invalid")
    return value


def _query_value(query, name):
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise TransportError(f"{name} must occur exactly once")
        value = value[0]
    return value


def verify_get(query, cfg):
    """Verify Meta's GET subscription handshake and return the raw challenge."""
    _require_enabled(cfg)
    if not isinstance(query, Mapping):
        raise TransportError("WhatsApp verification query is invalid")
    if _query_value(query, "hub.mode") != "subscribe":
        raise TransportError("WhatsApp verification mode is invalid")

    supplied = _query_value(query, "hub.verify_token")
    expected = cfg.get("whatsapp_verify_token")
    if (
        not isinstance(supplied, str)
        or not isinstance(expected, str)
        or not expected
        or len(supplied) > 4096
        or len(expected) > 4096
        or not hmac.compare_digest(
            supplied.encode("utf-8"),
            expected.encode("utf-8"),
        )
    ):
        raise TransportError("WhatsApp verification token did not match")

    challenge = _query_value(query, "hub.challenge")
    if not isinstance(challenge, str) or not challenge or len(challenge) > 4096:
        raise TransportError("WhatsApp verification challenge is invalid")
    return challenge


def _header(headers, name):
    if not isinstance(headers, Mapping):
        raise TransportError("WhatsApp webhook headers are invalid")
    lowered = name.casefold()
    matches = [
        value
        for key, value in headers.items()
        if isinstance(key, str) and key.casefold() == lowered
    ]
    if len(matches) != 1:
        raise TransportError(f"{name} must occur exactly once")
    return matches[0]


def _app_secret(cfg):
    values = [
        cfg[key]
        for key in ("whatsapp_app_secret", "whatsapp_webhook_secret")
        if key in cfg
    ]
    if not values or any(value != values[0] for value in values[1:]):
        raise TransportError("a single WhatsApp app secret is required")
    value = values[0]
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4096
    ):
        raise TransportError("WhatsApp app secret is invalid")
    return value


def verify_post(raw_body, headers, cfg):
    """Verify the raw POST body signature and return the same bytes."""
    _require_enabled(cfg)
    if not isinstance(raw_body, bytes):
        raise TransportError("WhatsApp webhook body must be exact raw bytes")
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise TransportError("WhatsApp webhook body exceeds 3 MiB")

    supplied = _header(headers, "X-Hub-Signature-256")
    if not isinstance(supplied, str):
        raise TransportError("WhatsApp webhook signature is invalid")
    expected = "sha256=" + hmac.new(
        _app_secret(cfg).encode("utf-8"),
        raw_body,
        "sha256",
    ).hexdigest()
    if not hmac.compare_digest(
        supplied.encode("utf-8"),
        expected.encode("ascii"),
    ):
        raise TransportError("WhatsApp webhook signature did not match")
    return raw_body


def _owner_ids(cfg):
    values = cfg.get("whatsapp_owner_user_ids", ())
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("whatsapp_owner_user_ids must be a collection")
    return {
        _bounded_string(value, "whatsapp_owner_user_ids entry")
        for value in values
    }


def _contacts(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("WhatsApp contacts must be an array")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError("WhatsApp contact is invalid")
    return value


def _matching_contact(message, contacts):
    from_user_id = _optional_string(
        message.get("from_user_id"),
        "WhatsApp from_user_id",
    )
    phone = _optional_string(message.get("from"), "WhatsApp sender phone")
    selected = None
    if from_user_id:
        selected = next(
            (
                contact
                for contact in contacts
                if contact.get("user_id") == from_user_id
            ),
            None,
        )
    if selected is None and phone:
        selected = next(
            (
                contact
                for contact in contacts
                if contact.get("wa_id") == phone
            ),
            None,
        )
    if selected is None and len(contacts) == 1:
        selected = contacts[0]
    if selected is not None:
        contact_user_id = selected.get("user_id")
        contact_phone = selected.get("wa_id")
        if (
            from_user_id is not None
            and contact_user_id is not None
            and contact_user_id != from_user_id
        ):
            return {}
        if phone is not None and contact_phone is not None and contact_phone != phone:
            return {}
        return selected
    return {}


def _principal(message, contacts):
    contact = _matching_contact(message, contacts)
    message_user_id = _optional_string(
        message.get("from_user_id"),
        "WhatsApp from_user_id",
    )
    contact_user_id = _optional_string(
        contact.get("user_id"),
        "WhatsApp contact user_id",
    )
    message_phone = _optional_string(
        message.get("from"),
        "WhatsApp sender phone",
    )
    contact_phone = _optional_string(
        contact.get("wa_id"),
        "WhatsApp contact phone",
    )
    principal = (
        message_user_id
        or contact_user_id
        or message_phone
        or contact_phone
    )
    if principal is None:
        raise ValueError("WhatsApp message has no stable sender identity")
    phone_alias = message_phone or contact_phone
    uses_user_id = message_user_id is not None or contact_user_id is not None
    return principal, phone_alias, uses_user_id


def _message_text(message):
    message_type = message.get("type")
    if message_type == "text":
        block = message.get("text")
        if not isinstance(block, Mapping):
            raise ValueError("WhatsApp text payload is invalid")
        value = block.get("body")
    elif message_type == "button":
        block = message.get("button")
        if not isinstance(block, Mapping):
            raise ValueError("WhatsApp button payload is invalid")
        value = block.get("text")
    elif message_type == "interactive":
        block = message.get("interactive")
        if not isinstance(block, Mapping):
            raise ValueError("WhatsApp interactive payload is invalid")
        reply_type = block.get("type")
        if reply_type not in {"button_reply", "list_reply"}:
            return None
        reply = block.get(reply_type)
        if not isinstance(reply, Mapping):
            raise ValueError("WhatsApp interactive reply is invalid")
        value = reply.get("title")
    else:
        return None

    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 4000
    ):
        raise ValueError("WhatsApp message text is invalid")
    return value


def parse_message(message, metadata, contacts, cfg):
    """Convert one supported inbound message to the canonical envelope."""
    _require_enabled(cfg)
    if not isinstance(message, Mapping) or not isinstance(metadata, Mapping):
        raise ValueError("WhatsApp message or metadata is invalid")
    text = _message_text(message)
    if text is None:
        return None

    remote_event_id = validate_wamid(message.get("id"))
    account_subject = _bounded_string(
        metadata.get("phone_number_id"),
        "WhatsApp phone_number_id",
    )
    contact_values = _contacts(contacts)
    principal_subject, phone_alias, uses_user_id = _principal(
        message,
        contact_values,
    )
    scope = (
        "owner-private"
        if principal_subject in _owner_ids(cfg)
        else "principal-private"
    )

    reply_target = {"phone_number_id": account_subject}
    reply_target["service_window_open"] = True
    if phone_alias is not None:
        reply_target["to"] = phone_alias
    if uses_user_id:
        reply_target["recipient"] = principal_subject
    if "to" not in reply_target and "recipient" not in reply_target:
        raise ValueError("WhatsApp message has no reply route")

    envelope = {
        "schema": "rapp-messaging-inbound/1.0",
        "transport": TRANSPORT,
        "remote_event_id": remote_event_id,
        "account_subject": account_subject,
        "principal_subject": principal_subject,
        "conversation_subject": f"{account_subject}\n{principal_subject}",
        "scope": scope,
        "participant_subjects": [principal_subject],
        "roster_epoch": "direct-v1",
        "text": text,
        "reply_target": reply_target,
    }
    return validate_inbound_envelope(envelope)


def parse_status(status):
    """Return only delivery fields consumed by the transport journal."""
    if not isinstance(status, Mapping):
        raise ValueError("WhatsApp status is invalid")
    identifier = validate_wamid(status.get("id"))
    state = _bounded_string(status.get("status"), "WhatsApp status", limit=64)
    timestamp = status.get("timestamp")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (str, int))
        or not str(timestamp)
        or len(str(timestamp)) > 64
    ):
        raise ValueError("WhatsApp status timestamp is invalid")
    errors = status.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("WhatsApp status errors must be an array")
    callback = status.get("biz_opaque_callback_data")
    if callback is not None:
        callback = _bounded_string(
            callback,
            "WhatsApp biz_opaque_callback_data",
        )
    return {
        "id": identifier,
        "status": state,
        "timestamp": timestamp,
        "errors": errors,
        "biz_opaque_callback_data": callback,
    }


def parse_webhook_payload(payload, cfg):
    """Walk every entry/change and return message and status batches."""
    _require_enabled(cfg)
    if not isinstance(payload, Mapping):
        raise ValueError("WhatsApp webhook JSON must be an object")
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        raise ValueError("WhatsApp webhook entries must be an array")

    messages = []
    statuses = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("WhatsApp webhook entry is invalid")
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            raise ValueError("WhatsApp webhook changes must be an array")
        for change in changes:
            if not isinstance(change, Mapping):
                raise ValueError("WhatsApp webhook change is invalid")
            value = change.get("value", {})
            if not isinstance(value, Mapping):
                raise ValueError("WhatsApp webhook change value is invalid")
            metadata = value.get("metadata", {})
            contacts = _contacts(value.get("contacts", []))

            raw_messages = value.get("messages", [])
            if not isinstance(raw_messages, list):
                raise ValueError("WhatsApp webhook messages must be an array")
            for raw_message in raw_messages:
                parsed_message = parse_message(
                    raw_message,
                    metadata,
                    contacts,
                    cfg,
                )
                if parsed_message is not None:
                    messages.append(parsed_message)

            raw_statuses = value.get("statuses", [])
            if not isinstance(raw_statuses, list):
                raise ValueError("WhatsApp webhook statuses must be an array")
            for raw_status in raw_statuses:
                statuses.append(parse_status(raw_status))

    return {"messages": messages, "statuses": statuses}


def parse_webhook(raw_body, headers, cfg):
    """Verify a POST over exact bytes, then decode and batch its JSON."""
    verified = verify_post(raw_body, headers, cfg)
    try:
        payload = json.loads(verified)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError("WhatsApp webhook JSON is invalid") from exc
    try:
        return parse_webhook_payload(payload, cfg)
    except ValueError as exc:
        raise TransportError(str(exc)) from exc


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _config_access_token(cfg):
    value = cfg.get("whatsapp_access_token")
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4096
        or any(character.isspace() or ord(character) < 33 for character in value)
        or value.startswith("Bearer ")
    ):
        raise TransportError("a raw WhatsApp access token is required")
    return value


def _config_graph_version(cfg):
    values = [
        cfg[key]
        for key in (
            "whatsapp_graph_version",
            "whatsapp_api_version",
            "whatsapp_graph_api_version",
        )
        if key in cfg
    ]
    if not values or any(value != values[0] for value in values[1:]):
        raise TransportError("one pinned WhatsApp Graph version is required")
    version = values[0]
    if not isinstance(version, str) or not _GRAPH_VERSION.fullmatch(version):
        raise TransportError("WhatsApp Graph version must use vN.N syntax")
    return version


def _path_component(value, field):
    if not isinstance(value, str) or not _PATH_COMPONENT.fullmatch(value):
        raise TransportError(f"{field} is invalid")
    return value


def _routing_value(value, field):
    try:
        return _bounded_string(value, field)
    except ValueError as exc:
        raise TransportError(str(exc)) from exc


def _approved_template(value):
    if not isinstance(value, Mapping):
        raise TransportError("an explicit approved WhatsApp template is required")
    template = dict(value)
    if template.pop("approved", None) is not True:
        raise TransportError("WhatsApp template is not explicitly approved")
    name = template.get("name")
    language = template.get("language")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z0-9_]{1,512}", name)
        or not isinstance(language, Mapping)
        or not isinstance(language.get("code"), str)
        or not language["code"]
        or len(language["code"]) > 35
    ):
        raise TransportError("WhatsApp approved template is invalid")
    try:
        encoded = json.dumps(
            template,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TransportError("WhatsApp approved template is invalid") from exc
    if not isinstance(decoded, dict):
        raise TransportError("WhatsApp approved template is invalid")
    return decoded


def _retry_after(headers, error_object):
    candidates = []
    if isinstance(error_object, Mapping):
        candidates.append(error_object.get("retry_after"))
        error_data = error_object.get("error_data")
        if isinstance(error_data, Mapping):
            candidates.extend(
                (
                    error_data.get("retry_after"),
                    error_data.get("retry_after_seconds"),
                )
            )
    if headers is not None:
        if hasattr(headers, "get"):
            candidates.append(headers.get("Retry-After"))
        elif hasattr(headers, "getheader"):
            candidates.append(headers.getheader("Retry-After"))

    for candidate in candidates:
        if candidate is None or isinstance(candidate, bool):
            continue
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(str(candidate))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = (
                    parsed.astimezone(timezone.utc)
                    - datetime.now(timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _decode_error(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(value, Mapping):
        return None, None
    error_object = value.get("error")
    if not isinstance(error_object, Mapping):
        return None, None
    code = error_object.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        code = None
    return code, error_object


class WhatsAppCloudClient:
    """Minimal Graph client with explicit no-retry send semantics."""

    origin = GRAPH_API_ORIGIN

    def __init__(self, cfg, *, opener=None, timeout=10):
        _require_enabled(cfg)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("WhatsApp timeout must be a positive number")
        if not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("WhatsApp timeout must be a positive number")
        self._token = _config_access_token(cfg)
        self._version = _config_graph_version(cfg)
        configured_phone = cfg.get("whatsapp_phone_number_id")
        self._phone_number_id = (
            _path_component(configured_phone, "whatsapp_phone_number_id")
            if configured_phone is not None
            else None
        )
        self.timeout = float(timeout)
        self._opener = opener or request.build_opener(_NoRedirect())

    def _route(self, reply_target):
        if not isinstance(reply_target, Mapping):
            raise TransportError("WhatsApp reply target must be an object")
        account = (
            reply_target.get("phone_number_id")
            or reply_target.get("account")
            or reply_target.get("account_subject")
            or self._phone_number_id
        )
        account = _path_component(account, "WhatsApp phone_number_id")
        if (
            self._phone_number_id is not None
            and account != self._phone_number_id
        ):
            raise TransportError("WhatsApp reply target account does not match")

        to = reply_target.get("to")
        recipient = reply_target.get("recipient")
        principal = (
            reply_target.get("principal")
            or reply_target.get("principal_subject")
        )
        if to is not None:
            to = _routing_value(to, "WhatsApp phone recipient")
        if recipient is not None:
            recipient = _routing_value(recipient, "WhatsApp BSUID recipient")
        if to is None and recipient is None and principal is not None:
            principal = _routing_value(principal, "WhatsApp principal")
            if _BSUID.fullmatch(principal):
                recipient = principal
            else:
                to = principal
        if to is None and recipient is None:
            raise TransportError("WhatsApp reply target has no recipient")
        return account, to, recipient

    @staticmethod
    def _read(response, *, ambiguous):
        try:
            try:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            except TypeError:
                body = response.read()
        except (TimeoutError, socket.timeout, error.URLError, OSError) as exc:
            error_type = AmbiguousSend if ambiguous else TransportError
            raise error_type("WhatsApp response was interrupted") from exc
        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            error_type = AmbiguousSend if ambiguous else TransportError
            raise error_type("WhatsApp response is invalid")
        return body

    def _post(self, phone_number_id, payload):
        path = f"/{self._version}/{phone_number_id}/messages"
        url = f"{GRAPH_API_ORIGIN}{path}"
        parsed = parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "graph.facebook.com"
            or parsed.path != path
            or parsed.query
            or parsed.fragment
        ):
            raise TransportError("WhatsApp Graph API origin is invalid")
        operation = request.Request(
            url,
            data=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "rappter-chrome-whatsapp/1.0",
            },
            method="POST",
        )

        response = None
        try:
            try:
                response = self._opener.open(operation, timeout=self.timeout)
            except error.HTTPError as exc:
                response = exc
            except (TimeoutError, socket.timeout, error.URLError, OSError) as exc:
                raise AmbiguousSend(
                    "WhatsApp send outcome is unknown"
                ) from exc

            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if type(status) is not int:
                raise AmbiguousSend("WhatsApp response status is unavailable")

            final_url = response.geturl() if hasattr(response, "geturl") else url
            if final_url != url:
                raise TransportError("WhatsApp Graph API redirects are refused")
            if 500 <= status <= 599:
                raise AmbiguousSend(
                    "WhatsApp server error left the send outcome unknown"
                )
            if status == 429:
                try:
                    body = self._read(response, ambiguous=False)
                    _, error_object = _decode_error(body)
                except TransportError:
                    error_object = None
                raise RetryableSend(
                    "WhatsApp synchronously rate limited the send",
                    retry_after=_retry_after(
                        getattr(response, "headers", None),
                        error_object,
                    ),
                )
            if 400 <= status <= 499:
                body = self._read(response, ambiguous=False)
                code, error_object = _decode_error(body)
                if code in _RATE_LIMIT_CODES:
                    raise RetryableSend(
                        "WhatsApp synchronously rate limited the send",
                        retry_after=_retry_after(
                            getattr(response, "headers", None),
                            error_object,
                        ),
                    )
                suffix = f", code {code}" if code is not None else ""
                raise TransportError(
                    f"WhatsApp API rejected the send ({status}{suffix})"
                )
            if status < 200 or status > 299:
                raise TransportError(
                    f"WhatsApp API returned unexpected status {status}"
                )
            return self._read(response, ambiguous=True)
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()

    def send_message(
        self,
        reply_target,
        text=None,
        *,
        attempt_id,
        service_window_open=False,
        approved_template=None,
    ):
        """Send free-form text in-window or one explicitly approved template."""
        phone_number_id, to, recipient = self._route(reply_target)
        attempt_id = _routing_value(
            attempt_id,
            "WhatsApp send attempt ID",
        )
        if approved_template is not None and text is not None:
            raise TransportError(
                "choose either WhatsApp free-form text or an approved template"
            )

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "biz_opaque_callback_data": attempt_id,
        }
        if to is not None:
            payload["to"] = to
        if recipient is not None:
            payload["recipient"] = recipient

        if approved_template is not None:
            payload["type"] = "template"
            payload["template"] = _approved_template(approved_template)
        else:
            if service_window_open is not True:
                raise TransportError(
                    "WhatsApp free-form sends require an open service window"
                )
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 4096
            ):
                raise TransportError(
                    "WhatsApp outbound text must contain 1 to 4096 characters"
                )
            payload["type"] = "text"
            payload["text"] = {"body": text}

        body = self._post(phone_number_id, payload)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AmbiguousSend(
                "WhatsApp returned invalid send evidence"
            ) from exc
        if not isinstance(value, Mapping):
            raise AmbiguousSend("WhatsApp returned invalid send evidence")
        messages = value.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or not isinstance(messages[0], Mapping)
        ):
            raise AmbiguousSend("WhatsApp returned no durable message evidence")
        try:
            return validate_wamid(messages[0].get("id"))
        except ValueError as exc:
            raise AmbiguousSend(
                "WhatsApp returned an invalid message ID"
            ) from exc

    send = send_message

    def send_reply(self, reply_target, text, *, attempt_id):
        if not isinstance(reply_target, Mapping):
            raise TransportError("WhatsApp reply target must be an object")
        return self.send_message(
            reply_target,
            text,
            attempt_id=attempt_id,
            service_window_open=(
                reply_target.get("service_window_open") is True
            ),
        )


verify_webhook_get = verify_get
verify_subscription = verify_get
verify_webhook_signature = verify_post
parse_webhook_post = parse_webhook
parse_webhook_request = parse_webhook
parse_inbound_message = parse_message
WhatsAppTransport = WhatsAppCloudClient


__all__ = [
    "GRAPH_API_ORIGIN",
    "MAX_WEBHOOK_BYTES",
    "TRANSPORT",
    "WhatsAppCloudClient",
    "WhatsAppTransport",
    "parse_inbound_message",
    "parse_message",
    "parse_status",
    "parse_webhook",
    "parse_webhook_payload",
    "parse_webhook_post",
    "parse_webhook_request",
    "validate_wamid",
    "verify_get",
    "verify_post",
    "verify_subscription",
    "verify_webhook_get",
    "verify_webhook_signature",
    "whatsapp_enabled",
]
