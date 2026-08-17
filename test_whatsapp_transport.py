#!/usr/bin/env python3
"""Offline tests for the fail-closed WhatsApp Cloud transport."""

import hmac
import json
import unittest

from messaging_transport import AmbiguousSend, RetryableSend, TransportError
from whatsapp_transport import (
    GRAPH_API_ORIGIN,
    MAX_WEBHOOK_BYTES,
    WhatsAppCloudClient,
    parse_webhook,
    parse_webhook_payload,
    verify_get,
)


PHONE_NUMBER_ID = "109876543210987"
OWNER_BSUID = "US.13491208655302741918"
USER_BSUID = "US.23491208655302741918"
PHONE_ALIAS = "16505551234"
WAMID_1 = "wamid.HBgL-one"
WAMID_2 = "wamid.HBgL-two"
WAMID_3 = "wamid.HBgL-three"


def config(**overrides):
    value = {
        "whatsapp_enabled": True,
        "whatsapp_verify_token": "verify-token",
        "whatsapp_app_secret": "app-secret",
        "whatsapp_access_token": "access-token",
        "whatsapp_graph_version": "v26.0",
        "whatsapp_phone_number_id": PHONE_NUMBER_ID,
        "whatsapp_owner_user_ids": [OWNER_BSUID],
    }
    value.update(overrides)
    return value


def signed_headers(body, cfg=None):
    cfg = config() if cfg is None else cfg
    digest = hmac.new(
        cfg["whatsapp_app_secret"].encode(),
        body,
        "sha256",
    ).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def text_message(identifier, *, sender=PHONE_ALIAS, user_id=None, text="hello"):
    value = {
        "id": identifier,
        "timestamp": "1786971600",
        "type": "text",
        "text": {"body": text},
    }
    if sender is not None:
        value["from"] = sender
    if user_id is not None:
        value["from_user_id"] = user_id
    return value


class FakeResponse:
    def __init__(self, status, value=b"", *, url=None, headers=None):
        self.status = status
        self.body = (
            json.dumps(value, separators=(",", ":")).encode()
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
            raise AssertionError("unexpected real or fake HTTP request")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if result.url is None:
            result.url = operation.full_url
        return result


class BrokenReadResponse(FakeResponse):
    def read(self, size=-1):
        raise OSError("interrupted body")


class VerificationTests(unittest.TestCase):
    def test_get_requires_subscribe_and_exact_token_and_returns_raw_challenge(self):
        query = {
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-token",
            "hub.challenge": "0000012345",
        }
        self.assertEqual(verify_get(query, config()), "0000012345")

        for changed in (
            {**query, "hub.mode": "SUBSCRIBE"},
            {**query, "hub.verify_token": "verify-token "},
        ):
            with self.subTest(changed=changed), self.assertRaises(TransportError):
                verify_get(changed, config())

    def test_signature_is_checked_before_json_and_body_is_bounded(self):
        malformed = b"{not-json"
        with self.assertRaisesRegex(TransportError, "signature"):
            parse_webhook(
                malformed,
                {"X-Hub-Signature-256": "sha256=" + "0" * 64},
                config(),
            )

        oversized = b"x" * (MAX_WEBHOOK_BYTES + 1)
        with self.assertRaisesRegex(TransportError, "3 MiB"):
            parse_webhook(oversized, signed_headers(b"different"), config())

    def test_disabled_unless_exact_true(self):
        query = {
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-token",
            "hub.challenge": "1",
        }
        for cfg in ({}, {"whatsapp_enabled": False}, {"whatsapp_enabled": 1}):
            with self.subTest(cfg=cfg), self.assertRaises(TransportError):
                verify_get(query, cfg)
            with self.assertRaises(TransportError):
                parse_webhook(b"{}", {}, cfg)
            opener = FakeOpener()
            with self.assertRaises(TransportError):
                WhatsAppCloudClient(cfg, opener=opener)
            self.assertEqual(opener.requests, [])


class InboundTests(unittest.TestCase):
    def test_all_entries_changes_messages_and_statuses_are_batched(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": PHONE_NUMBER_ID,
                                },
                                "contacts": [
                                    {
                                        "wa_id": PHONE_ALIAS,
                                        "user_id": USER_BSUID,
                                    }
                                ],
                                "messages": [
                                    text_message(
                                        WAMID_1,
                                        user_id=USER_BSUID,
                                        text="first",
                                    ),
                                    {
                                        "id": "wamid.unsupported",
                                        "type": "image",
                                        "image": {"id": "media-id"},
                                    },
                                ],
                                "statuses": [
                                    {
                                        "id": "wamid.outbound-one",
                                        "status": "sent",
                                        "timestamp": "1786971601",
                                        "errors": [],
                                        "biz_opaque_callback_data": "attempt-1",
                                    }
                                ],
                            }
                        },
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": PHONE_NUMBER_ID,
                                },
                                "contacts": [
                                    {
                                        "wa_id": "16505550002",
                                        "user_id": OWNER_BSUID,
                                    }
                                ],
                                "messages": [
                                    text_message(
                                        WAMID_2,
                                        sender="16505550002",
                                        user_id=OWNER_BSUID,
                                        text="second",
                                    )
                                ],
                            }
                        },
                    ]
                },
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": PHONE_NUMBER_ID,
                                },
                                "contacts": [{"wa_id": "16505550003"}],
                                "messages": [
                                    text_message(
                                        WAMID_3,
                                        sender="16505550003",
                                        text="third",
                                    )
                                ],
                                "statuses": [
                                    {
                                        "id": "wamid.outbound-two",
                                        "status": "failed",
                                        "timestamp": "1786971602",
                                        "errors": [{"code": 131026}],
                                        "biz_opaque_callback_data": "attempt-2",
                                        "recipient_id": "must-not-be-retained",
                                    }
                                ],
                            }
                        }
                    ]
                },
            ],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        batch = parse_webhook(body, signed_headers(body), config())
        self.assertEqual(
            [item["remote_event_id"] for item in batch["messages"]],
            [WAMID_1, WAMID_2, WAMID_3],
        )
        self.assertEqual(len(batch["statuses"]), 2)

    def test_bsuid_precedence_contact_lookup_and_phone_fallback(self):
        base = {"phone_number_id": PHONE_NUMBER_ID}

        direct = parse_webhook_payload(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": base,
                                    "contacts": [
                                        {
                                            "wa_id": PHONE_ALIAS,
                                            "user_id": "US.contact-id",
                                        }
                                    ],
                                    "messages": [
                                        text_message(
                                            WAMID_1,
                                            user_id=USER_BSUID,
                                        )
                                    ],
                                }
                            }
                        ]
                    }
                ]
            },
            config(),
        )["messages"][0]
        self.assertEqual(direct["principal_subject"], USER_BSUID)
        self.assertEqual(
            direct["reply_target"],
            {
                "phone_number_id": PHONE_NUMBER_ID,
                "service_window_open": True,
                "to": PHONE_ALIAS,
                "recipient": USER_BSUID,
            },
        )

        contact_only = parse_webhook_payload(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": base,
                                    "contacts": [
                                        {
                                            "wa_id": PHONE_ALIAS,
                                            "user_id": USER_BSUID,
                                        }
                                    ],
                                    "messages": [text_message(WAMID_2)],
                                }
                            }
                        ]
                    }
                ]
            },
            config(),
        )["messages"][0]
        self.assertEqual(contact_only["principal_subject"], USER_BSUID)

        phone_only = parse_webhook_payload(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": base,
                                    "contacts": [{"wa_id": PHONE_ALIAS}],
                                    "messages": [text_message(WAMID_3)],
                                }
                            }
                        ]
                    }
                ]
            },
            config(),
        )["messages"][0]
        self.assertEqual(phone_only["principal_subject"], PHONE_ALIAS)
        self.assertEqual(
            phone_only["reply_target"],
            {
                "phone_number_id": PHONE_NUMBER_ID,
                "service_window_open": True,
                "to": PHONE_ALIAS,
            },
        )

    def test_owner_scope_is_only_from_explicit_user_ids(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": PHONE_NUMBER_ID,
                                },
                                "contacts": [],
                                "messages": [
                                    text_message(
                                        WAMID_1,
                                        sender=None,
                                        user_id=OWNER_BSUID,
                                    ),
                                    text_message(
                                        WAMID_2,
                                        sender=None,
                                        user_id=USER_BSUID,
                                    ),
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        messages = parse_webhook_payload(payload, config())["messages"]
        self.assertEqual(messages[0]["scope"], "owner-private")
        self.assertEqual(messages[1]["scope"], "principal-private")
        self.assertEqual(messages[0]["participant_subjects"], [OWNER_BSUID])
        self.assertEqual(messages[0]["roster_epoch"], "direct-v1")
        self.assertEqual(
            messages[0]["conversation_subject"],
            f"{PHONE_NUMBER_ID}\n{OWNER_BSUID}",
        )

    def test_status_keeps_only_delivery_evidence_fields(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.outbound",
                                        "status": "read",
                                        "timestamp": "1786971603",
                                        "errors": [{"code": 7}],
                                        "biz_opaque_callback_data": "attempt-7",
                                        "recipient_id": "raw-recipient",
                                        "conversation": {"id": "raw-conversation"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        status = parse_webhook_payload(payload, config())["statuses"][0]
        self.assertEqual(
            status,
            {
                "id": "wamid.outbound",
                "status": "read",
                "timestamp": "1786971603",
                "errors": [{"code": 7}],
                "biz_opaque_callback_data": "attempt-7",
            },
        )


class ClientTests(unittest.TestCase):
    def test_free_form_requires_service_window_before_network(self):
        opener = FakeOpener()
        client = WhatsAppCloudClient(config(), opener=opener)
        with self.assertRaisesRegex(TransportError, "service window"):
            client.send(
                {
                    "phone_number_id": PHONE_NUMBER_ID,
                    "recipient": USER_BSUID,
                },
                "outside window",
                attempt_id="attempt-window",
            )
        self.assertEqual(opener.requests, [])

    def test_explicit_approved_template_is_allowed_outside_window(self):
        opener = FakeOpener(
            FakeResponse(200, {"messages": [{"id": "wamid.template-send"}]})
        )
        client = WhatsAppCloudClient(config(), opener=opener)
        result = client.send(
            {
                "phone_number_id": PHONE_NUMBER_ID,
                "recipient": USER_BSUID,
            },
            attempt_id="attempt-template",
            approved_template={
                "approved": True,
                "name": "shipping_update",
                "language": {"code": "en_US"},
            },
        )
        self.assertEqual(result, "wamid.template-send")
        payload = json.loads(opener.requests[0][0].data)
        self.assertEqual(payload["type"], "template")
        self.assertNotIn("approved", payload["template"])
        self.assertEqual(
            payload["biz_opaque_callback_data"],
            "attempt-template",
        )
        with self.assertRaisesRegex(TransportError, "explicitly approved"):
            client.send(
                {
                    "phone_number_id": PHONE_NUMBER_ID,
                    "recipient": USER_BSUID,
                },
                attempt_id="attempt-unapproved",
                approved_template={
                    "name": "shipping_update",
                    "language": {"code": "en_US"},
                },
            )

    def test_success_uses_fixed_origin_bearer_and_validates_wamid(self):
        opener = FakeOpener(
            FakeResponse(200, {"messages": [{"id": "wamid.success"}]})
        )
        client = WhatsAppCloudClient(
            config(whatsapp_graph_origin="https://attacker.example"),
            opener=opener,
        )
        result = client.send(
            {
                "phone_number_id": PHONE_NUMBER_ID,
                "to": PHONE_ALIAS,
                "recipient": USER_BSUID,
            },
            "hello",
            attempt_id="attempt-success",
            service_window_open=True,
        )
        self.assertEqual(result, "wamid.success")
        operation, timeout = opener.requests[0]
        self.assertEqual(operation.get_method(), "POST")
        self.assertEqual(
            operation.full_url,
            f"{GRAPH_API_ORIGIN}/v26.0/{PHONE_NUMBER_ID}/messages",
        )
        self.assertEqual(
            operation.get_header("Authorization"),
            "Bearer access-token",
        )
        self.assertEqual(timeout, 10.0)
        payload = json.loads(operation.data)
        self.assertEqual(payload["to"], PHONE_ALIAS)
        self.assertEqual(payload["recipient"], USER_BSUID)
        self.assertEqual(payload["text"], {"body": "hello"})
        self.assertEqual(
            payload["biz_opaque_callback_data"],
            "attempt-success",
        )

    def test_timeout_5xx_and_lost_success_response_are_ambiguous_once(self):
        cases = (
            TimeoutError("network timeout"),
            FakeResponse(503, {"error": {"code": 2}}),
            FakeResponse(200, {"messages": []}),
        )
        for index, result in enumerate(cases):
            with self.subTest(index=index):
                opener = FakeOpener(result)
                client = WhatsAppCloudClient(config(), opener=opener)
                with self.assertRaises(AmbiguousSend):
                    client.send(
                        {
                            "phone_number_id": PHONE_NUMBER_ID,
                            "recipient": USER_BSUID,
                        },
                        "hello",
                        attempt_id=f"attempt-{index}",
                        service_window_open=True,
                    )
                self.assertEqual(len(opener.requests), 1)

    def test_proven_rate_limit_is_retryable_with_guidance(self):
        opener = FakeOpener(
            FakeResponse(
                400,
                {
                    "error": {
                        "code": 130429,
                        "error_data": {"retry_after": "12.5"},
                    }
                },
            )
        )
        client = WhatsAppCloudClient(config(), opener=opener)
        with self.assertRaises(RetryableSend) as caught:
            client.send(
                {
                    "phone_number_id": PHONE_NUMBER_ID,
                    "recipient": USER_BSUID,
                },
                "hello",
                attempt_id="attempt-rate",
                service_window_open=True,
            )
        self.assertEqual(caught.exception.retry_after, 12.5)
        self.assertEqual(len(opener.requests), 1)

    def test_429_header_remains_retryable_when_body_read_is_interrupted(self):
        opener = FakeOpener(
            BrokenReadResponse(
                429,
                headers={"Retry-After": "2"},
            )
        )
        client = WhatsAppCloudClient(config(), opener=opener)
        with self.assertRaises(RetryableSend) as caught:
            client.send_reply(
                {
                    "phone_number_id": PHONE_NUMBER_ID,
                    "recipient": USER_BSUID,
                    "service_window_open": True,
                },
                "hello",
                attempt_id="a" * 25,
            )
        self.assertEqual(caught.exception.retry_after, 2.0)


if __name__ == "__main__":
    unittest.main()
