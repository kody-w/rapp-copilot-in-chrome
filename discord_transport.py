#!/usr/bin/env python3
"""Fail-closed Discord Gateway v10 and REST transport helpers."""

from __future__ import annotations

import json
import math
import re
import socket
from collections.abc import Mapping
from urllib import error, parse, request

from messaging_transport import (
    AmbiguousSend,
    RetryableSend,
    TransportError,
    validate_inbound_envelope,
)


DISCORD_API_ORIGIN = "https://discord.com/api/v10"
MAX_SNOWFLAKE = (1 << 64) - 1
MAX_RESPONSE_BYTES = 1024 * 1024
_SNOWFLAKE = re.compile(r"^[1-9][0-9]{0,19}$")
_FORBIDDEN_TOKEN_KEYS = (
    "discord_token",
    "discord_user_token",
    "user_token",
)


def discord_enabled(cfg):
    """Return true only for the transport's explicit boolean enable switch."""
    return isinstance(cfg, Mapping) and cfg.get("discord_enabled") is True


def validate_snowflake(value, field="Discord ID"):
    """Validate Discord's canonical unsigned decimal-string ID form."""
    if (
        not isinstance(value, str)
        or not _SNOWFLAKE.fullmatch(value)
        or int(value) > MAX_SNOWFLAKE
    ):
        raise ValueError(f"{field} must be an exact decimal-string Snowflake")
    return value


def _configured_identity(cfg):
    application_id = cfg.get("discord_application_id")
    if application_id is not None:
        application_id = validate_snowflake(
            application_id,
            "discord_application_id",
        )

    bot_ids = []
    for key in ("discord_bot_user_id", "discord_bot_id"):
        value = cfg.get(key)
        if value is not None:
            bot_ids.append(validate_snowflake(value, key))
    if len(set(bot_ids)) > 1:
        raise ValueError("configured Discord bot IDs disagree")
    bot_id = bot_ids[0] if bot_ids else None

    if application_id is None and bot_id is None:
        raise ValueError("a Discord application or bot ID is required")
    account_id = application_id or bot_id
    mention_id = bot_id or application_id
    return account_id, mention_id, {item for item in (application_id, bot_id) if item}


def _owner_ids(cfg):
    values = cfg.get("discord_owner_user_ids", ())
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("discord_owner_user_ids must be a collection")
    return {
        validate_snowflake(value, "discord_owner_user_ids entry")
        for value in values
    }


def _guild_mention_required(cfg):
    for key in (
        "discord_guild_mention_required",
        "discord_require_mention",
        "discord_mention_gate",
    ):
        if key in cfg:
            return cfg[key] is not False
    return True


def _mention_ids(message):
    mentions = message.get("mentions", [])
    if not isinstance(mentions, list):
        raise ValueError("Discord MESSAGE_CREATE mentions must be a list")
    result = set()
    for mention in mentions:
        if not isinstance(mention, Mapping):
            raise ValueError("Discord MESSAGE_CREATE mention is invalid")
        result.add(validate_snowflake(mention.get("id"), "Discord mention ID"))
    return result


def _validate_related_ids(message):
    webhook_id = message.get("webhook_id")
    if webhook_id is not None:
        validate_snowflake(webhook_id, "Discord webhook ID")

    role_mentions = message.get("mention_roles", [])
    if not isinstance(role_mentions, list):
        raise ValueError("Discord role mentions must be a list")
    for role_id in role_mentions:
        validate_snowflake(role_id, "Discord role mention ID")

    reference = message.get("message_reference")
    if reference is not None:
        if not isinstance(reference, Mapping):
            raise ValueError("Discord message reference is invalid")
        for key in ("message_id", "channel_id", "guild_id"):
            value = reference.get(key)
            if value is not None:
                validate_snowflake(value, f"Discord reference {key}")


def parse_gateway_message(payload, cfg):
    """Convert a Gateway v10 MESSAGE_CREATE dispatch to the inbound schema.

    Disabled, unrelated, bot-authored, self-authored, and unmentioned guild
    events return ``None``. Malformed or content-unavailable message events
    raise ``ValueError`` rather than guessing at identity or content.
    """
    if not discord_enabled(cfg):
        return None
    _reject_user_tokens(cfg)
    if not isinstance(payload, Mapping):
        raise ValueError("Discord Gateway payload must be an object")
    if type(payload.get("op")) is not int or payload.get("op") != 0:
        return None
    if payload.get("t") != "MESSAGE_CREATE":
        return None

    message = payload.get("d")
    if not isinstance(message, Mapping):
        raise ValueError("Discord MESSAGE_CREATE data must be an object")

    account_id, mention_id, self_ids = _configured_identity(cfg)
    message_id = validate_snowflake(message.get("id"), "Discord message ID")
    channel_id = validate_snowflake(message.get("channel_id"), "Discord channel ID")

    guild_id = message.get("guild_id")
    if guild_id is not None:
        guild_id = validate_snowflake(guild_id, "Discord guild ID")

    author = message.get("author")
    if not isinstance(author, Mapping):
        raise ValueError("Discord MESSAGE_CREATE author must be an object")
    author_id = validate_snowflake(author.get("id"), "Discord author ID")
    bot_marker = author.get("bot", False)
    if type(bot_marker) is not bool:
        raise ValueError("Discord author bot marker must be boolean")

    _validate_related_ids(message)
    if bot_marker or author_id in self_ids or message.get("webhook_id") is not None:
        return None
    mentions = _mention_ids(message)

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Discord message content is unavailable")
    if len(content) > 4000:
        raise ValueError("Discord message content exceeds the inbound limit")

    if guild_id is None:
        scope = (
            "owner-private"
            if author_id in _owner_ids(cfg)
            else "principal-private"
        )
        conversation_subject = channel_id
        roster_epoch = f"{scope}:{channel_id}:{author_id}"
    else:
        if _guild_mention_required(cfg) and mention_id not in mentions:
            return None
        scope = "public"
        conversation_subject = f"{guild_id}:{channel_id}"
        roster_epoch = f"public-unrostered:{guild_id}:{channel_id}"

    envelope = {
        "schema": "rapp-messaging-inbound/1.0",
        "transport": "discord",
        "remote_event_id": message_id,
        "account_subject": account_id,
        "principal_subject": author_id,
        "conversation_subject": conversation_subject,
        "scope": scope,
        "participant_subjects": [author_id],
        "roster_epoch": roster_epoch,
        "text": content,
        "reply_target": {
            "channel_id": channel_id,
            "message_id": message_id,
        },
    }
    return validate_inbound_envelope(envelope)


parse_gateway_event = parse_gateway_message
parse_gateway_payload = parse_gateway_message
parse_message_create = parse_gateway_message


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_user_tokens(cfg):
    for key in _FORBIDDEN_TOKEN_KEYS:
        if key in cfg:
            raise TransportError("Discord user or generic tokens are not accepted")


def _bot_token(cfg):
    _reject_user_tokens(cfg)
    token = cfg.get("discord_bot_token")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 512
        or token != token.strip()
        or any(character.isspace() or ord(character) < 33 for character in token)
        or token.startswith(("Bot ", "Bearer "))
    ):
        raise TransportError("a raw Discord bot token is required")
    return token


def _nonce(value):
    if isinstance(value, bool):
        raise ValueError("Discord nonce is invalid")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("Discord nonce is invalid")
        value = str(value)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 25
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("Discord nonce must contain 1 to 25 characters")
    return value


def _retry_after_from(headers, body):
    candidate = None
    try:
        value = json.loads(body.decode("utf-8"))
        if isinstance(value, Mapping):
            candidate = value.get("retry_after")
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if candidate is None and headers is not None:
        if hasattr(headers, "get"):
            candidate = headers.get("Retry-After")
        elif hasattr(headers, "getheader"):
            candidate = headers.getheader("Retry-After")
    if isinstance(candidate, bool):
        return None
    try:
        parsed = float(candidate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


class DiscordRestClient:
    """Minimal Discord API v10 client with explicit send ambiguity semantics."""

    origin = DISCORD_API_ORIGIN

    def __init__(self, cfg, opener=None, timeout=10):
        if not discord_enabled(cfg):
            raise TransportError("Discord transport is disabled")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("Discord timeout must be a positive number")
        if not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("Discord timeout must be a positive number")
        self._token = _bot_token(cfg)
        self.timeout = float(timeout)
        self._opener = opener or request.build_opener(_NoRedirect())
        if not callable(self._opener) and not callable(
            getattr(self._opener, "open", None)
        ):
            raise TypeError("Discord opener must be callable")

    def _request(self, method, path, payload=None, *, missing_ok=False):
        url = f"{DISCORD_API_ORIGIN}{path}"
        parsed = parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "discord.com"
            or not parsed.path.startswith("/api/v10/")
            or parsed.query
            or parsed.fragment
        ):
            raise TransportError("Discord API origin is invalid")

        data = None
        headers = {
            "Authorization": f"Bot {self._token}",
            "Accept": "application/json",
            "User-Agent": "rappter-chrome-discord/1.0",
        }
        if payload is not None:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        operation = request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )

        response = None
        try:
            open_request = (
                self._opener.open
                if hasattr(self._opener, "open")
                else self._opener
            )
            response = open_request(operation, timeout=self.timeout)
        except error.HTTPError as exc:
            response = exc
        except (TimeoutError, socket.timeout, error.URLError, OSError) as exc:
            raise AmbiguousSend("Discord request outcome is unknown") from None

        try:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if isinstance(status, bool) or not isinstance(status, int):
                raise AmbiguousSend("Discord response status is unavailable")

            final_url = response.geturl() if hasattr(response, "geturl") else url
            if final_url != url:
                raise TransportError("Discord API redirects are refused")

            if missing_ok and status == 404:
                return None
            if status == 429:
                try:
                    body = self._read(response)
                except AmbiguousSend:
                    body = b""
                retry_after = _retry_after_from(
                    getattr(response, "headers", None),
                    body,
                )
                raise RetryableSend(
                    "Discord rate limited the request",
                    retry_after=retry_after,
                )
            if 500 <= status <= 599:
                raise AmbiguousSend("Discord server error left the outcome unknown")
            if 400 <= status <= 499:
                raise TransportError(f"Discord API rejected the request ({status})")
            if status < 200 or status > 299:
                raise TransportError(f"Discord API returned unexpected status {status}")
            return self._read(response)
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()

    @staticmethod
    def _read(response):
        if not callable(getattr(response, "read", None)):
            raise AmbiguousSend("Discord response is invalid")
        try:
            try:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            except TypeError:
                body = response.read()
        except (TimeoutError, socket.timeout, error.URLError, OSError):
            raise AmbiguousSend("Discord response was interrupted") from None
        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise AmbiguousSend("Discord response is invalid")
        return body

    @staticmethod
    def _decode_message(body, *, ambiguous):
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error_type = AmbiguousSend if ambiguous else TransportError
            raise error_type("Discord returned an invalid Message response") from exc
        if not isinstance(message, dict):
            error_type = AmbiguousSend if ambiguous else TransportError
            raise error_type("Discord returned an invalid Message response")
        return message

    def send_message(
        self,
        channel_id,
        text,
        nonce,
        reply_message_id=None,
    ):
        channel_id = validate_snowflake(channel_id, "Discord channel ID")
        if not isinstance(text, str) or not text or len(text) > 2000:
            raise ValueError("Discord message text must contain 1 to 2000 characters")
        nonce = _nonce(nonce)

        payload = {
            "content": text,
            "allowed_mentions": {
                "parse": [],
                "replied_user": False,
            },
            "nonce": nonce,
            "enforce_nonce": True,
        }
        if reply_message_id is not None:
            reply_message_id = validate_snowflake(
                reply_message_id,
                "Discord reply message ID",
            )
            payload["message_reference"] = {
                "channel_id": channel_id,
                "message_id": reply_message_id,
            }

        body = self._request(
            "POST",
            f"/channels/{channel_id}/messages",
            payload,
        )
        message = self._decode_message(body, ambiguous=True)
        try:
            remote_id = validate_snowflake(
                message.get("id"),
                "Discord returned Message ID",
            )
            response_channel = message.get("channel_id")
            if response_channel is not None:
                response_channel = validate_snowflake(
                    response_channel,
                    "Discord returned channel ID",
                )
                if response_channel != channel_id:
                    raise ValueError("Discord returned a different channel ID")
        except ValueError as exc:
            raise AmbiguousSend("Discord returned invalid Message evidence") from exc
        return remote_id

    def send_reply(
        self,
        reply_target,
        text,
        nonce=None,
        *,
        attempt_id=None,
    ):
        if not isinstance(reply_target, Mapping):
            raise ValueError("Discord reply target must be an object")
        if set(reply_target) != {"channel_id", "message_id"}:
            raise ValueError("Discord reply target must contain channel and message IDs")
        if nonce is not None and attempt_id is not None:
            raise ValueError("provide one Discord nonce")
        nonce = attempt_id if attempt_id is not None else nonce
        return self.send_message(
            reply_target["channel_id"],
            text,
            nonce,
            reply_target["message_id"],
        )

    def reconcile_message(self, channel_id, message_id):
        channel_id = validate_snowflake(channel_id, "Discord channel ID")
        message_id = validate_snowflake(message_id, "Discord message ID")
        body = self._request(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}",
            missing_ok=True,
        )
        if body is None:
            return None
        message = self._decode_message(body, ambiguous=False)
        try:
            returned_id = validate_snowflake(
                message.get("id"),
                "Discord returned Message ID",
            )
            returned_channel = validate_snowflake(
                message.get("channel_id"),
                "Discord returned channel ID",
            )
        except ValueError as exc:
            raise TransportError("Discord reconciliation evidence is invalid") from exc
        if returned_id != message_id or returned_channel != channel_id:
            raise TransportError("Discord reconciliation evidence does not match")
        return message

    reconcile = reconcile_message
    get_message = reconcile_message
    send = send_message
