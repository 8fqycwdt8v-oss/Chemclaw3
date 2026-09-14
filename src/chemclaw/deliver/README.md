# `deliver/` — the outbound delivery seam

**Where a message leaves for a person.** The fourth attachment seam, beside `connector.yaml`
(a capability *produces*), `datasource.yaml` (a source *supplies*) and `sink.yaml` (a sink
*consumes what this system produced*).

## Why it is a fourth thing

A sink takes a typed scientific record to a database; nobody reads it. A channel takes a *message
to a person* — a digest, a report, an escalation — and the difference is the audience rather than
the transport.

`durable/digest.py` states the position this replaces, in as many words: *"no new delivery
mechanism, no email integration, no second notification system."* That was right while the product
was a chat window, and it is the reason a project leader could not be reached on a Monday morning:
the only place a digest landed was a mailbox inside the app, which is the one place somebody who is
not already using the app will not look.

## The shape

A folder holding a `channel.yaml`, found on `CHEMCLAW_DELIVERY_CHANNELS_DIR`, enabled by name in
`CHEMCLAW_DELIVERY_CHANNELS`, with a `module:callable` driver resolved late and built per delivery.
Identical to the other three seams on purpose — an operator who has attached a data source already
knows how to attach a channel.

```
deliver/
├── manifest.py       # what a channel declares
├── registry.py       # discover, enable, build, deliver
├── driver.py         # the Protocol, and the two shipped drivers
├── message.py        # what leaves, and the redaction it passes through
└── channels/
    ├── share/        # write into a mounted directory (no credential, no egress)
    └── webhook/      # POST JSON to a URL (needs an egress rule and a token)
```

## Three rules

1. **Delivery is off until a deployment names a channel.** `CHEMCLAW_DELIVERY_CHANNELS` is empty by
   default. This is deliberately *not* the connector registry's "discovery is enablement": a
   discovered connector serves a tool, and a discovered channel sends something out of the building.
2. **Every message is redacted once, in the registry, before any driver sees it.** A scrub each
   driver has to remember is a scrub the next driver forgets, and the one that forgets is the one
   that sends outside the cluster. The filter is `core/logging.py`'s, so a credential this process
   holds cannot ride out in a body assembled from a tool result.
3. **A failing channel does not stop the others.** `deliver` returns the channels that took the
   message, because "delivered" and "swallowed" are different facts and the digest's watermark
   depends on the difference.

## A message may carry files

`Message.attachments` is how an artefact travels — the share writes each one as its own file beside
the message, the webhook POSTs it base64 in the payload. The rule that decides what goes where:
**`body` is the message and an attachment is the artefact.** A report reached a chemist who had
closed the tab as "recorded as `report-…`, open it beside its citations" — a note id, deliverable
only to somebody who can already reach the graph — and the document they asked for now rides with
it.

Three properties, each asserted in `tests/test_delivery.py`: a filename is bounded by a pattern
rather than by a docstring (the share joins it onto a directory, so `kind`'s argument applies one
field over); an attachment goes through the same redaction a body does, because typing a field
`bytes` is not a reason to put the half that leaves the cluster outside the guarantee; and the
idempotency key reads an attachment's *identity* and not its bytes, so a message and the same
message carrying a file are two deliveries while a redraft of one report stays one.

The encoding is base64 in both directions because `OutboundMessage` crosses a Temporal activity
boundary — pydantic's default for `bytes` is a utf-8 decode that raises on the first byte outside
it, so a text-only seam widened later would be changing a durable payload under open histories.

## What a channel is not

Read-only in the other direction: nothing here reads *from* a channel. A driver that offered to
would be an ingest source declaring its way into a read path, which is the mirror of the rule
`ingest/sources/README.md` already states.
