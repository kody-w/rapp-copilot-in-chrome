#!/usr/bin/env python3
"""Deterministic stdlib-only tests for the iMessage transport adapter."""

import json
import queue
import subprocess
import tempfile
import threading
import unittest

from messaging_transport import AmbiguousSend, RetryableSend, TransportError
from imessage_transport import (
    IMSG_PINNED_VERSION,
    IMessageAdapter,
    IMessageFifoWorker,
    ImsgProtocolError,
    ImsgRpcClient,
    preflight_imessage,
    validate_imessage_config,
)


OWNER = "+15550000001"
FRIEND = "+15550000002"
SELF_GUID = "iMessage;-;owner-self"
GROUP_GUID = "iMessage;+;approved-group"


def config(**overrides):
    value = {
        "imessage_enabled": True,
        "imessage_path": "/synthetic/imsg",
        "imessage_version": IMSG_PINNED_VERSION,
        "imessage_account_id": "synthetic-account",
        "imessage_owner_handles": [OWNER],
        "imessage_owner_chat_guids": [SELF_GUID],
        "imessage_dm_allowlist": [FRIEND],
        "imessage_group_allowlist": [GROUP_GUID],
        "imessage_mention_tokens": ["@rappter"],
        "imessage_worker_count": 1,
        "imessage_writer_count": 1,
        "imessage_state_dir": tempfile.mkdtemp(
            prefix="imessage-transport-test-"
        ),
    }
    value.update(overrides)
    return value


def owner_message(
    *,
    guid="OWNER-GUID",
    rowid=1,
    text="hello",
    is_from_me=False,
    service="iMessage",
):
    return {
        "id": rowid,
        "guid": guid,
        "text": text,
        "sender": OWNER,
        "is_from_me": is_from_me,
        "is_group": False,
        "service": service,
        "chat_id": 7,
        "chat_guid": SELF_GUID,
        "participants": [OWNER],
    }


def dm_message(*, guid="DM-GUID", rowid=2, text="hello"):
    return {
        "id": rowid,
        "guid": guid,
        "text": text,
        "sender": FRIEND,
        "is_from_me": False,
        "is_group": False,
        "service": "iMessage",
        "chat_id": 8,
        "chat_guid": "iMessage;-;friend-dm",
        "participants": [FRIEND],
    }


def group_message(
    *,
    guid="GROUP-GUID",
    rowid=3,
    text="@rappter hello",
    participants=None,
):
    return {
        "id": rowid,
        "guid": guid,
        "text": text,
        "sender": FRIEND,
        "is_from_me": False,
        "is_group": True,
        "service": "iMessage",
        "chat_id": 9,
        "chat_guid": GROUP_GUID,
        "participants": participants or [OWNER, FRIEND],
    }


class _QueueReader:
    _EOF = object()

    def __init__(self):
        self._values = queue.Queue()

    def put(self, value):
        self._values.put(value)

    def close(self):
        self._values.put(self._EOF)

    def readline(self):
        value = self._values.get(timeout=2)
        return "" if value is self._EOF else value


class _FakeStdin:
    def __init__(self, process, behavior):
        self.process = process
        self.behavior = behavior
        self.lines = []
        self.closed = False

    def write(self, line):
        if self.closed:
            raise BrokenPipeError("closed")
        self.lines.append(line)
        return len(line)

    def flush(self):
        request = json.loads(self.lines[-1])
        if self.behavior == "success":
            self.process.stdout.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"ok": True, "guid": "RPC-GUID"},
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        elif self.behavior == "malformed":
            self.process.stdout.put("not-json\n")

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, behavior):
        self.returncode = None
        self.stdout = _QueueReader()
        self.stderr = _QueueReader()
        self.stdin = _FakeStdin(self, behavior)

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15
            self.stdout.close()
            self.stderr.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = 0
            self.stdout.close()
            self.stderr.close()
        return self.returncode


class _PopenFactory:
    def __init__(self, behavior):
        self.behavior = behavior
        self.process = None
        self.argv = None
        self.kwargs = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.process = _FakeProcess(self.behavior)
        return self.process


class _FakeRpc:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "guid": "OUTBOUND-GUID"}
        self.calls = []

    def request(self, method, params=None, timeout=None):
        self.calls.append((method, dict(params or {}), timeout))
        return self.result


class IMessageTransportTests(unittest.TestCase):
    def adapter(self, **kwargs):
        return IMessageAdapter(config(), platform="darwin", **kwargs)

    def test_non_mac_and_disabled_gate_refuse(self):
        with self.assertRaises(TransportError):
            IMessageAdapter(config(), platform="linux")
        for disabled in (False, 1, "true", None):
            with self.subTest(disabled=disabled):
                with self.assertRaises(TransportError):
                    IMessageAdapter(
                        config(imessage_enabled=disabled),
                        platform="darwin",
                    )

    def test_sms_and_empty_messages_fail_closed(self):
        adapter = self.adapter(rpc_client=_FakeRpc())
        with self.assertRaises(TransportError):
            adapter.parse_message(owner_message(service="SMS"))
        with self.assertRaises(TransportError):
            adapter.parse_message(owner_message(text=""))

    def test_owner_dm_and_group_route_to_exact_envelopes(self):
        adapter = self.adapter(rpc_client=_FakeRpc())

        owner = adapter.inspect_message(owner_message())
        self.assertEqual(owner.cursor, 1)
        self.assertEqual(owner.envelope["schema"], "rapp-messaging-inbound/1.0")
        self.assertEqual(owner.envelope["transport"], "imessage")
        self.assertEqual(owner.envelope["remote_event_id"], "OWNER-GUID")
        self.assertEqual(owner.envelope["scope"], "owner-private")
        self.assertEqual(
            owner.envelope["reply_target"],
            {
                "chat_id": 7,
                "chat_guid": SELF_GUID,
                "service": "imessage",
            },
        )
        self.assertNotIn("id", owner.envelope)
        self.assertNotIn("rowid", json.dumps(owner.envelope))

        direct = adapter.parse_message(dm_message())
        self.assertEqual(direct["scope"], "principal-private")
        self.assertNotEqual(
            direct["principal_subject"],
            owner.envelope["principal_subject"],
        )

        group = adapter.parse_message(group_message())
        self.assertEqual(group["scope"], "group-shared")
        self.assertEqual(len(group["participant_subjects"]), 2)
        reordered = adapter.parse_message(
            group_message(
                guid="GROUP-REORDERED",
                rowid=4,
                participants=[FRIEND, OWNER],
            )
        )
        changed = adapter.parse_message(
            group_message(
                guid="GROUP-CHANGED",
                rowid=5,
                participants=[OWNER, FRIEND, "+15550000003"],
            )
        )
        self.assertEqual(group["roster_epoch"], reordered["roster_epoch"])
        self.assertNotEqual(group["roster_epoch"], changed["roster_epoch"])
        self.assertEqual(len(changed["participant_subjects"]), 3)

    def test_group_mention_gate_uses_token_boundaries(self):
        adapter = self.adapter(rpc_client=_FakeRpc())
        for text in ("hello", "name@rappter.example", "@rappterish hello"):
            with self.subTest(text=text):
                with self.assertRaises(TransportError):
                    adapter.parse_message(group_message(text=text))
        self.assertEqual(
            adapter.parse_message(group_message(text="(@rappter) hello"))[
                "scope"
            ],
            "group-shared",
        )

    def test_exact_outbound_guid_is_exposed_as_suppressed_echo(self):
        adapter = self.adapter(rpc_client=_FakeRpc())
        adapter.record_outbound_guid("BOT-GUID")
        echo = adapter.inspect_message(
            owner_message(guid="BOT-GUID", rowid=10, is_from_me=True)
        )
        self.assertTrue(echo.is_bot_echo)
        self.assertEqual(echo.outbound_guid, "BOT-GUID")
        self.assertIsNone(echo.envelope)
        self.assertTrue(
            adapter.is_outbound_echo(
                owner_message(guid="BOT-GUID", rowid=11, is_from_me=True)
            )
        )
        local_owner = adapter.inspect_message(
            owner_message(guid="OWNER-LOCAL", rowid=12, is_from_me=True)
        )
        self.assertFalse(local_owner.is_bot_echo)
        self.assertEqual(local_owner.envelope["scope"], "owner-private")

    def test_outbound_guid_suppression_survives_adapter_restart(self):
        state_dir = tempfile.mkdtemp(prefix="imessage-guid-restart-")
        cfg = config(imessage_state_dir=state_dir)
        first = IMessageAdapter(
            cfg,
            platform="darwin",
            rpc_client=_FakeRpc(),
        )
        first.send_reply(
            {
                "chat_id": 7,
                "chat_guid": SELF_GUID,
                "service": "imessage",
            },
            "reply",
            attempt_id="b" * 25,
        )
        second = IMessageAdapter(
            cfg,
            platform="darwin",
            rpc_client=_FakeRpc(),
        )
        echo = second.inspect_message(
            owner_message(
                guid="OUTBOUND-GUID",
                rowid=21,
                text="reply",
                is_from_me=True,
            )
        )
        self.assertTrue(echo.is_bot_echo)
        self.assertIsNone(echo.envelope)

    def test_sender_forces_imessage_and_requires_success_guid(self):
        rpc = _FakeRpc()
        adapter = self.adapter(rpc_client=rpc)
        guid = adapter.send(
            {
                "chat_id": 7,
                "chat_guid": SELF_GUID,
                "service": "imessage",
                "recipient": "+15559999999",
            },
            "reply",
            attempt_id="a" * 25,
            timeout=4,
        )
        self.assertEqual(guid, "OUTBOUND-GUID")
        method, params, timeout = rpc.calls[0]
        self.assertEqual(method, "send")
        self.assertEqual(timeout, 4)
        self.assertEqual(
            params,
            {
                "chat_id": 7,
                "chat_guid": SELF_GUID,
                "text": "reply",
                "service": "imessage",
            },
        )
        self.assertTrue(
            adapter.is_outbound_echo(
                owner_message(
                    guid="OUTBOUND-GUID",
                    rowid=20,
                    is_from_me=True,
                )
            )
        )
        with self.assertRaises(AmbiguousSend):
            self.adapter(
                rpc_client=_FakeRpc({"ok": True})
            ).send({"chat_id": 7, "service": "imessage"}, "reply")
        with self.assertRaises(TransportError):
            adapter.send(
                {"chat_id": 7, "service": "SMS"},
                "must not downgrade",
            )
        with self.assertRaises(AmbiguousSend):
            self.adapter(
                rpc_client=_FakeRpc({
                    "ok": True,
                    "guid": "SMS-GUID",
                    "service": "SMS",
                })
            ).send(
                {"chat_id": 7, "service": "imessage"},
                "must reject SMS evidence",
            )

    def test_request_not_started_is_definitely_retryable(self):
        client = ImsgRpcClient("/synthetic/imsg")
        with self.assertRaises(RetryableSend):
            client.request("send", {"chat_id": 7, "text": "reply"})

    def test_rpc_ids_are_monotonic_and_stderr_is_never_exposed(self):
        diagnostics = []
        factory = _PopenFactory("success")
        client = ImsgRpcClient(
            "/synthetic/imsg",
            popen_factory=factory,
            on_diagnostic=diagnostics.append,
        )
        client.start()
        factory.process.stderr.put("private message content\n")
        self.assertEqual(client.request("probe", {}), {
            "ok": True,
            "guid": "RPC-GUID",
        })
        self.assertEqual(client.request("probe", {}), {
            "ok": True,
            "guid": "RPC-GUID",
        })
        client.stop()
        ids = [json.loads(line)["id"] for line in factory.process.stdin.lines]
        self.assertEqual(ids, [1, 2])
        self.assertEqual(diagnostics, ["imsg rpc diagnostic"])
        self.assertNotIn("private message content", " ".join(diagnostics))

    def test_flushed_send_timeout_is_ambiguous_and_line_is_exact(self):
        factory = _PopenFactory("timeout")
        client = ImsgRpcClient(
            "/synthetic/imsg",
            default_timeout=0.02,
            popen_factory=factory,
        )
        client.start()
        with self.assertRaises(AmbiguousSend):
            client.request("send", {"chat_id": 7, "text": "reply"})
        self.assertEqual(
            factory.argv,
            ["/synthetic/imsg", "rpc", "--json"],
        )
        self.assertEqual(
            factory.process.stdin.lines,
            [
                '{"jsonrpc":"2.0","id":1,"method":"send",'
                '"params":{"chat_id":7,"text":"reply"}}\n'
            ],
        )
        client.stop()

    def test_malformed_json_is_terminal_and_flushed_send_is_ambiguous(self):
        factory = _PopenFactory("malformed")
        client = ImsgRpcClient(
            "/synthetic/imsg",
            popen_factory=factory,
        )
        client.start()
        with self.assertRaises(AmbiguousSend):
            client.request(
                "send",
                {"chat_id": 7, "text": "reply"},
                timeout=1,
            )
        self.assertIsInstance(client.close_error, ImsgProtocolError)
        self.assertFalse(client.is_running)
        client.stop()

    def test_one_writer_and_fifo_worker_are_mandatory(self):
        with self.assertRaises(TransportError):
            validate_imessage_config(
                config(imessage_worker_count=2),
                platform="darwin",
            )
        with self.assertRaises(TransportError):
            validate_imessage_config(
                config(imessage_writer_count=2),
                platform="darwin",
            )
        with self.assertRaises(TransportError):
            IMessageFifoWorker(lambda _: None, worker_count=2)

        order = []
        complete = threading.Event()

        def handle(value):
            order.append(value)
            if len(order) == 3:
                complete.set()

        worker = IMessageFifoWorker(handle)
        worker.start()
        for value in (1, 2, 3):
            worker.submit(value)
        self.assertTrue(complete.wait(1))
        worker.stop()
        self.assertEqual(order, [1, 2, 3])

    def test_preflight_uses_pinned_version_and_read_probe(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout="imsg 0.12.3\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout='{"chats":[]}\n',
                stderr="",
            )

        result = preflight_imessage(
            config(),
            platform="darwin",
            runner=runner,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["read_ready"])
        self.assertEqual(result["imsg_version"], IMSG_PINNED_VERSION)
        self.assertEqual(
            [call[0] for call in calls],
            [
                ["/synthetic/imsg", "--version"],
                [
                    "/synthetic/imsg",
                    "chats",
                    "--limit",
                    "1",
                    "--json",
                ],
            ],
        )
        self.assertTrue(all(call[1]["shell"] is False for call in calls))


if __name__ == "__main__":
    unittest.main()
