# Universal Messaging Twin

One hatched twin can speak through multiple transports without becoming
multiple identities or leaking one audience into another.

```text
Google Voice web ─┐
macOS iMessage ───┼─> verified adapter envelope
Discord bot ──────┤   -> HMAC trust broker
WhatsApp Cloud ───┘   -> FIFO inbox / one twin / durable outbox
                         -> provider evidence reconciliation
```

Google Voice is the only enabled transport after installation. The other
adapters are libraries that stay disabled until their official prerequisites
and explicit local identity enrollment are complete.

## Shared contract

Every adapter emits `rapp-messaging-inbound/1.0` in memory. Raw provider IDs
are used only long enough to derive local HMAC identifiers; journal files
contain no account, phone, user, channel, chat, or provider-message ID.

The broker determines one exact scope:

- `owner-private` — owner memory agent available;
- `principal-private` — no owner memory;
- `group-shared` — requires a complete verified roster epoch;
- `public` — no private memory.

Agent files are physically selected before the model runs. Prompt text cannot
grant a broader roster.

Inbound state is `observed -> claimed -> processed | retryable | dropped`.
Provider batches must be fully observed before processing. Outbound state is
`prepared -> attempted -> submitted | unknown | failed -> delivered/read`.
`attempted` after a crash becomes `unknown`; `unknown` is never retried.
Provider status evidence is append-only and monotonic: `read` cannot regress
to `delivered`, and a later `failed` cannot erase delivery evidence.

## Google Voice web

The live resident adapter remains account/peer locked in the real Edge/Chrome
tab. Account, origin, path, `itemId`, and rendered peer header are checked in
the same JavaScript snapshot. Sending is accepted only after the exact
correlated outgoing bubble is read back.

## iMessage — macOS only

The iMessage adapter:

- refuses every non-macOS platform;
- requires `imessage_enabled: true`;
- pins signed `imsg 0.12.3`;
- requires Full Disk Access and Messages Automation permission;
- requires explicit owner handles and exact enrolled self-chat GUIDs;
- allowlists every other DM/group and mention-gates groups;
- uses one FIFO worker and one writer lease;
- forces and verifies `service: "imessage"`;
- refuses SMS and never falls back to it;
- records exact outbound GUIDs for durable echo suppression.

This machine does not currently have `imsg` installed, so iMessage remains
disabled. Installation and macOS permission acceptance are human actions.

## Discord application

Use an official bot installation—never a user token. Configure the minimum
Gateway v10 intents and permissions needed for the chosen surfaces. DMs may be
linked to the owner explicitly; other DMs are principal-private. Guild
messages are mention-gated and remain `public` because a single
`MESSAGE_CREATE` event does not prove the complete guild/channel roster.

Channel sends use:

- `POST /channels/{channel.id}/messages`;
- restrictive `allowed_mentions`;
- one durable nonce no longer than 25 characters;
- `enforce_nonce: true`;
- the returned Message ID as acceptance evidence.

Rate-limit retries reuse the same nonce. A lost response without reconcilable
evidence becomes `unknown`.

## WhatsApp Cloud API

WhatsApp requires a public HTTPS webhook and official Meta credentials.
Verification GETs use a private verify token. Every POST validates
`X-Hub-Signature-256` over the exact raw body before JSON parsing. The adapter
processes every batched entry/change/message/status, keeps `wamid` and BSUID
opaque, and uses phone aliases only as fallback routing.

Free-form replies require the inbound-opened customer-service window.
Outside it, an explicitly `approved: true` template is required. The API's
`biz_opaque_callback_data` correlates statuses but is not idempotency.
Timeout, disconnect, 5xx, or lost success evidence becomes `unknown`.

## Adapter invocation

Adapters must durably observe all events in a provider batch before processing:

```python
messaging = UniversalMessagingTwin(config)
results = messaging.process_batch(
    envelopes,
    adapter.send_reply,
)
```

For a single sequential transport event:

```python
messaging.observe(envelope)
result = messaging.process(envelope, adapter.send_reply)
```

WhatsApp status webhooks reconcile through
`messaging.reconcile_whatsapp_status(status)`.

## Standards status

The shared behavior follows public draft `rapp-messaging/1.0`; iMessage also
cites `rapp-messaging-imessage/1.0`. Google Voice, Discord, and WhatsApp
profile registration remains open at:

- <https://github.com/kody-w/rapp-messaging/issues/2>
- <https://github.com/kody-w/rapp-1/issues/4>

Until those authorities rule, the system reports
`structural-pre-acceptance`, never full authenticated RAPP/1 conformance.
