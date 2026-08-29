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

## What a channel is not

Read-only in the other direction: nothing here reads *from* a channel. A driver that offered to
would be an ingest source declaring its way into a read path, which is the mirror of the rule
`ingest/sources/README.md` already states.
