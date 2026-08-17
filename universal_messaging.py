#!/usr/bin/env python3
"""One twin identity and trust broker behind multiple messaging transports."""

from pathlib import Path

import messaging_transport
import voice_twin


class UniversalMessagingTwin:
    def __init__(self, cfg, root=None):
        self.cfg = dict(cfg)
        self.root = Path(root or (voice_twin.TWIN_ROOT / "messaging-journal"))
        self.journal = messaging_transport.MessagingJournal(self.root)

    def _sanitized_context(self, envelope):
        envelope = messaging_transport.validate_inbound_envelope(dict(envelope))
        with voice_twin.twin_lock():
            rappid = voice_twin.ensure_identity(self.cfg)
            binding, context, descriptor = voice_twin.channel_context(
                self.cfg,
                rappid,
                envelope,
            )
        return envelope, binding, context, descriptor

    def observe(self, envelope):
        envelope, binding, context, descriptor = self._sanitized_context(envelope)
        record = self.journal.observe(
            event_id=context["source_event_id"],
            transport=descriptor["transport"],
            conversation_id=context["conversation_id"],
            audience_id=context["audience_id"],
            scope=context["scope"],
            text=envelope["text"],
        )
        return {
            "event_id": context["source_event_id"],
            "binding_id": binding["binding_id"],
            "conversation_id": context["conversation_id"],
            "state": record["state"],
        }

    def process(self, envelope, sender, responder=None):
        if not callable(sender):
            raise ValueError("an enabled transport sender is required")
        envelope, binding, context, descriptor = self._sanitized_context(envelope)
        event_id = context["source_event_id"]
        observed = self.journal.inbound_record(event_id)
        if observed is None:
            raise RuntimeError(
                "inbound must be durably observed before processing"
            )
        with self.journal.transport_lease(binding["binding_id"]):
            observed = self.journal.inbound_record(event_id)
            if observed is None:
                raise RuntimeError("observed inbound disappeared")
            if observed["state"] == "processed":
                existing = self.journal.outbound_for_event(event_id)
                if existing is None:
                    raise RuntimeError("processed inbound has no outbound evidence")
                return existing
            oldest = self.journal.oldest_unresolved(context["conversation_id"])
            if oldest is None:
                raise RuntimeError("conversation has no unresolved inbound")
            if oldest["event_id"] != event_id:
                return {
                    "schema": "rapp-messaging-blocked/1.0",
                    "state": "blocked",
                    "event_id": event_id,
                    "blocked_by": oldest["event_id"],
                }
            inbox = self.journal.transition_inbound(event_id, "claimed")
            outbox = self.journal.outbound_for_event(event_id)
            if outbox is None:
                dispatch = responder or (
                    lambda value: voice_twin.chat_channel(value, {}, self.cfg)
                )
                try:
                    reply = dispatch(envelope)
                except Exception:
                    self.journal.transition_inbound(event_id, "retryable")
                    raise
                outbox = self.journal.prepare_outbound(
                    event_id=event_id,
                    conversation_id=context["conversation_id"],
                    text=reply,
                )
            else:
                reply = outbox["text"]
            if outbox["state"] in {
                "submitted",
                "unknown",
                "delivered",
                "read",
            }:
                if inbox["state"] != "processed":
                    self.journal.transition_inbound(event_id, "processed")
                return outbox
            if outbox["state"] == "attempted":
                record = self.journal.transition_outbound(
                    outbox["outbox_id"],
                    "unknown",
                )
                self.journal.transition_inbound(event_id, "processed")
                return record
            if (
                outbox["state"] == "failed"
                and outbox.get("failure_disposition") == "terminal"
            ):
                self.journal.transition_inbound(event_id, "processed")
                return outbox

            self.journal.transition_outbound(outbox["outbox_id"], "attempted")
            attempt_id = outbox["outbox_id"].split(":", 1)[1][:25]
            try:
                result = sender(
                    envelope["reply_target"],
                    reply,
                    attempt_id=attempt_id,
                )
            except messaging_transport.AmbiguousSend:
                record = self.journal.transition_outbound(
                    outbox["outbox_id"],
                    "unknown",
                )
                self.journal.transition_inbound(event_id, "processed")
                return record
            except messaging_transport.RetryableSend:
                self.journal.transition_outbound(
                    outbox["outbox_id"],
                    "failed",
                    failure_disposition="retryable",
                )
                self.journal.transition_inbound(event_id, "retryable")
                raise
            except messaging_transport.TransportError:
                record = self.journal.transition_outbound(
                    outbox["outbox_id"],
                    "failed",
                    failure_disposition="terminal",
                )
                self.journal.transition_inbound(event_id, "processed")
                return record

            if isinstance(result, str) and result:
                result = {
                    "state": "submitted",
                    "remote_message_id": result,
                }
            if (
                not isinstance(result, dict)
                or result.get("state") != "submitted"
                or not isinstance(result.get("remote_message_id"), str)
                or not result["remote_message_id"]
            ):
                record = self.journal.transition_outbound(
                    outbox["outbox_id"],
                    "unknown",
                )
                self.journal.transition_inbound(event_id, "processed")
                return record
            remote_id = messaging_transport.private_id(
                voice_twin._secret(),
                "remote",
                (
                    f"{descriptor['transport']}\n"
                    f"{result['remote_message_id']}"
                ),
            )
            record = self.journal.transition_outbound(
                outbox["outbox_id"],
                "submitted",
                remote_id=remote_id,
            )
            self.journal.transition_inbound(event_id, "processed")
            return record

    def process_batch(self, envelopes, sender, responder=None):
        """Persist a provider batch completely before draining it in order."""
        prepared = []
        for index, envelope in enumerate(envelopes):
            info = self.observe(envelope)
            prepared.append((index, info["event_id"], envelope))
        return [
            self.process(envelope, sender, responder=responder)
            for _, _, envelope in sorted(prepared)
        ]

    def reconcile_provider_status(
        self,
        *,
        transport,
        attempt_id,
        remote_message_id,
        status,
        observed_at=None,
    ):
        if not messaging_transport.TRANSPORT.fullmatch(str(transport or "")):
            raise ValueError("transport is invalid")
        outbox = self.journal.outbox_for_attempt(attempt_id)
        if outbox is None:
            raise RuntimeError("provider status has no matching attempt")
        remote_id = messaging_transport.private_id(
            voice_twin._secret(),
            "remote",
            f"{transport}\n{remote_message_id}",
        )
        return self.journal.record_provider_status(
            outbox["outbox_id"],
            status,
            remote_id=remote_id,
            observed_at=observed_at,
        )

    def reconcile_whatsapp_status(self, status):
        if not isinstance(status, dict):
            raise ValueError("WhatsApp status must be an object")
        attempt_id = status.get("biz_opaque_callback_data")
        if not isinstance(attempt_id, str):
            raise RuntimeError("WhatsApp status lacks local attempt correlation")
        return self.reconcile_provider_status(
            transport="whatsapp-cloud",
            attempt_id=attempt_id,
            remote_message_id=status.get("id"),
            status=status.get("status"),
            observed_at=str(status.get("timestamp")),
        )
