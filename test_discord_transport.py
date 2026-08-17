#!/usr/bin/env python3
"""Offline tests for the fail-closed Discord transport."""

import json
import unittest

from discord_transport import (
    DISCORD_API_ORIGIN,
    DiscordRestClient,
    parse_gateway_message,
    validate_snowflake,
)
from messaging_transport import AmbiguousSend, RetryableSend, TransportError


APP_ID = "100000000000000001"
BOT_ID = "100000000000000002"
OWNER_ID = "200000000000000001"
USER_ID = "200000000000000002"
GUILD_ID = "300000000000000001"
CHANNEL_ID = "400000000000000001"
MESSAGE_ID = "500000000000000001"
REPLY_ID = "500000000000000002"


def config(**overrides):
    value = {
        "discord_enabled": True,
        "discord_application_id": APP_ID,
        "discord_bot_user_id": BOT_ID,
        "discord_bot_token": "test.bot.token",
        "discord_owner_user_ids": [OWNER_ID],
    }
    value.update(overrides)
    return value


def gateway_message(
    *,
    author_id=USER_ID,
    content="hello",
    guild_id=None,
    mentions=None,
    author_bot=False,
):
    message = {
        "id": MESSAGE_ID,
        "channel_id": CHANNEL_ID,
        "author": {"id": author_id, "bot": author_bot},
        "content": content,
        "mentions": [] if mentions is None else mentions,
        "mention_roles": [],
    }
    if guild_id is not None:
        message["guild_id"] = guild_id
    return {"op": 0, "t": "MESSAGE_CREATE", "s": 7, "d": message}


class FakeResponse:
    def __init__(self, status, value=b"", *, url=None, headers=None):
        self.status = status
        self.body = (
            json.dumps(value, separators=(",", ":")).encode("utf-8")
            if not isinstance(value, bytes)
            else value
        )
        self.url = url
        self.headers = headers or {}
        self.closed = False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def open(self, operation, timeout=None):
        self.requests.append((operation, timeout))
        if not self.results:
            raise AssertionError("unexpected fake HTTP request")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if result.url is None:
            result.url = operation.full_url
        return result


class GatewayTests(unittest.TestCase):
    def test_disabled_unless_exact_true(self):
        payload = gateway_message()
        for disabled in ({}, {"discord_enabled": False}, {"discord_enabled": 1}):
            self.assertIsNone(parse_gateway_message(payload, disabled))
        with self.assertRaises(TransportError):
            DiscordRestClient(
                config(discord_enabled=1),
                opener=FakeOpener(),
            )

    def test_dm_scopes_are_explicit(self):
        owner = parse_gateway_message(
            gateway_message(author_id=OWNER_ID),
            config(),
        )
        self.assertEqual(owner["scope"], "owner-private")
        self.assertEqual(owner["conversation_subject"], CHANNEL_ID)
        self.assertEqual(owner["participant_subjects"], [OWNER_ID])

        principal = parse_gateway_message(gateway_message(), config())
        self.assertEqual(principal["scope"], "principal-private")
        self.assertEqual(principal["principal_subject"], USER_ID)
        self.assertEqual(principal["account_subject"], APP_ID)

    def test_guild_is_public_without_inventing_a_roster(self):
        envelope = parse_gateway_message(
            gateway_message(
                guild_id=GUILD_ID,
                mentions=[{"id": BOT_ID}],
            ),
            config(),
        )
        self.assertEqual(envelope["scope"], "public")
        self.assertEqual(
            envelope["conversation_subject"],
            f"{GUILD_ID}:{CHANNEL_ID}",
        )
        self.assertEqual(envelope["participant_subjects"], [USER_ID])
        self.assertIn("unrostered", envelope["roster_epoch"])
        self.assertEqual(
            envelope["reply_target"],
            {"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID},
        )

    def test_guild_mention_gate_uses_structured_ids(self):
        spoofed_text = f"<@{BOT_ID}> hello"
        self.assertIsNone(
            parse_gateway_message(
                gateway_message(guild_id=GUILD_ID, content=spoofed_text),
                config(),
            )
        )
        accepted = parse_gateway_message(
            gateway_message(guild_id=GUILD_ID),
            config(discord_guild_mention_required=False),
        )
        self.assertEqual(accepted["text"], "hello")

    def test_snowflakes_are_exact_decimal_strings(self):
        for invalid in (
            123,
            "0",
            "01",
            "+1",
            "1 ",
            "18446744073709551616",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_snowflake(invalid)

        for field, value in (("id", 123), ("channel_id", "0400000000000000001")):
            payload = gateway_message()
            payload["d"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_gateway_message(payload, config())

    def test_bot_and_self_echoes_are_ignored(self):
        self.assertIsNone(
            parse_gateway_message(gateway_message(author_bot=True), config())
        )
        self.assertIsNone(
            parse_gateway_message(
                gateway_message(author_id=BOT_ID),
                config(),
            )
        )

    def test_unavailable_content_fails_closed(self):
        for content in (None, "", "   "):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_gateway_message(
                    gateway_message(content=content),
                    config(),
                )


class RestTests(unittest.TestCase):
    def test_post_payload_authorization_reply_and_nonce(self):
        opener = FakeOpener(
            FakeResponse(
                200,
                {"id": MESSAGE_ID, "channel_id": CHANNEL_ID},
            )
        )
        client = DiscordRestClient(config(), opener=opener)
        nonce = "n" * 25
        self.assertEqual(
            client.send_message(
                CHANNEL_ID,
                "response",
                nonce=nonce,
                reply_message_id=REPLY_ID,
            ),
            MESSAGE_ID,
        )

        operation, timeout = opener.requests[0]
        self.assertEqual(operation.get_method(), "POST")
        self.assertEqual(
            operation.full_url,
            f"{DISCORD_API_ORIGIN}/channels/{CHANNEL_ID}/messages",
        )
        self.assertEqual(operation.get_header("Authorization"), "Bot test.bot.token")
        self.assertEqual(timeout, 10.0)
        body = json.loads(operation.data)
        self.assertEqual(
            body["allowed_mentions"],
            {"parse": [], "replied_user": False},
        )
        self.assertEqual(body["nonce"], nonce)
        self.assertIs(body["enforce_nonce"], True)
        self.assertEqual(
            body["message_reference"],
            {"channel_id": CHANNEL_ID, "message_id": REPLY_ID},
        )

    def test_nonce_over_25_is_rejected_before_open(self):
        opener = FakeOpener()
        client = DiscordRestClient(config(), opener=opener)
        with self.assertRaises(ValueError):
            client.send_message(CHANNEL_ID, "response", nonce="x" * 26)
        self.assertEqual(opener.requests, [])

    def test_429_is_retryable_with_retry_after(self):
        client = DiscordRestClient(
            config(),
            opener=FakeOpener(FakeResponse(429, {"retry_after": 1.75})),
        )
        with self.assertRaises(RetryableSend) as caught:
            client.send_message(CHANNEL_ID, "response", nonce="rate-limit")
        self.assertEqual(caught.exception.retry_after, 1.75)

    def test_timeout_is_ambiguous(self):
        client = DiscordRestClient(
            config(),
            opener=FakeOpener(TimeoutError("secret token must not appear")),
        )
        with self.assertRaises(AmbiguousSend) as caught:
            client.send_message(CHANNEL_ID, "response", nonce="timeout")
        self.assertNotIn("test.bot.token", str(caught.exception))

    def test_success_requires_a_valid_message_id(self):
        good = DiscordRestClient(
            config(),
            opener=FakeOpener(FakeResponse(200, {"id": MESSAGE_ID})),
        )
        self.assertEqual(
            good.send_message(CHANNEL_ID, "response", nonce="success"),
            MESSAGE_ID,
        )
        reply = DiscordRestClient(
            config(),
            opener=FakeOpener(FakeResponse(200, {"id": MESSAGE_ID})),
        )
        self.assertEqual(
            reply.send_reply(
                {"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID},
                "response",
                attempt_id="a" * 25,
            ),
            MESSAGE_ID,
        )

        bad = DiscordRestClient(
            config(),
            opener=FakeOpener(FakeResponse(200, {"id": "01"})),
        )
        with self.assertRaises(AmbiguousSend):
            bad.send_message(CHANNEL_ID, "response", nonce="bad-evidence")

    def test_exact_origin_and_explicit_reconcile_get(self):
        opener = FakeOpener(
            FakeResponse(200, {"id": MESSAGE_ID}),
            FakeResponse(
                200,
                {
                    "id": MESSAGE_ID,
                    "channel_id": CHANNEL_ID,
                    "content": "response",
                },
            ),
        )
        client = DiscordRestClient(
            config(discord_api_origin="https://attacker.example/api/v10"),
            opener=opener,
        )
        client.send_message(CHANNEL_ID, "response", nonce="origin")
        reconciled = client.reconcile_message(CHANNEL_ID, MESSAGE_ID)
        self.assertEqual(reconciled["id"], MESSAGE_ID)
        self.assertEqual(
            [operation.full_url for operation, _ in opener.requests],
            [
                f"{DISCORD_API_ORIGIN}/channels/{CHANNEL_ID}/messages",
                (
                    f"{DISCORD_API_ORIGIN}/channels/{CHANNEL_ID}"
                    f"/messages/{MESSAGE_ID}"
                ),
            ],
        )
        self.assertEqual(opener.requests[1][0].get_method(), "GET")

    def test_user_token_fields_are_never_accepted(self):
        with self.assertRaises(TransportError):
            DiscordRestClient(
                config(discord_user_token="not-a-bot-token"),
                opener=FakeOpener(),
            )


if __name__ == "__main__":
    unittest.main()
