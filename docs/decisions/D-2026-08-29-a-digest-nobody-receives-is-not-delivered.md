# D-2026-08-29-a-digest-nobody-receives-is-not-delivered — the delivery seam and the read-only face

**Status:** accepted · **Date:** 2026-08-29 · Fifth of the eight infrastructure findings from the
2026-08-28 audit (F7). Reverses a position `durable/digest.py` states in prose; supersedes no ADR,
because the position was never in one.

## Context

Two directions, both closed, and each defensible for a chat product.

**Outbound.** The only place anything this system produces lands is a mailbox inside the app.
`durable/digest.py` says so deliberately: *"through the existing session push-back channel (F3-T3) —
no new delivery mechanism, no email integration, no second notification system."* Right when it was
written, and it is why a project leader cannot be reached on a Monday morning: the one place a
digest lands is the one place somebody who is not already using the app will not look.
`grep -rn "smtp\|graph.microsoft.com" src/` finds nothing, and there is no outbound webhook either.

**Inbound.** ChemClaw3 is an MCP *client* and has never been an MCP server, and there is no A2A
agent card. The assistant in the chat client, the one in the portfolio tool and the one a partner
runs cannot ask the system that holds this programme's chemistry anything. The one durable advantage
here over a general assistant is a governed, cited, auditable record — worth far more if it can be
*reached* than if it can only be visited.

## Decision

### `chemclaw.deliver` — the fourth attachment seam

A folder holding a `channel.yaml`, found on a path list, enabled by name, driver resolved late,
built per delivery. Deliberately identical to the other three: an operator who has attached a data
source already knows how to attach a channel, and a fourth mechanism to learn would be the cost of a
seam that bought nothing.

**A channel is genuinely a fourth thing.** A connector *produces*, a source *supplies*, a sink
*consumes what this system produced* — and a sink takes a typed record to a database nobody reads.
A channel takes a *message to a person*. The difference is the audience rather than the transport,
which is exactly why it could not be a sink with a different driver.

Three properties are the decision:

1. **Delivery is off until a deployment names a channel.** `CHEMCLAW_DELIVERY_CHANNELS` is empty by
   default. This is deliberately *not* the connector registry's "discovery is enablement until you
   say otherwise": a discovered connector serves a tool, and a discovered channel sends something
   out of the building. A name with no folder is a startup error rather than a skip — an operator
   who spelled a channel wrong means to be delivering and is not, which is the one failure a
   delivery seam has to be loud about.
2. **Every message is redacted once, in the registry, before any driver sees it.** A scrub each
   driver has to remember is one the next driver forgets, and the one that forgets is the one that
   sends outside the cluster. It is `core/logging.py`'s filter, so a credential this process holds
   cannot ride out inside a body assembled from a tool result — and unlike a log line, a delivered
   message has already left.
3. **A failing channel does not stop the others,** and `deliver` returns which ones took the
   message. "Delivered" and "swallowed" are different facts, and `durable/digest.py` is precisely
   the caller that must not conflate them.

The digest's watermark deliberately does **not** wait on outbound delivery. The mailbox write stays
the durable handover: a chemist who does open the app must see the digest whether or not a channel
took it, and a webhook outage must not re-report matches the mailbox already holds. Outbound is a
courtesy on top of a delivered digest.

Two drivers ship and neither holds a mail client. `share` writes into a mounted directory — no
credential, no egress, the same argument `chemclaw.ingest.documents` makes in the other direction —
and `webhook` POSTs JSON with a bearer token read from the environment by name. A site that wants
Exchange, Teams or Slack writes a `module:callable`, exactly as it would for a warehouse ELN.
Shipping a credentialed client for one vendor would make that vendor's shape the seam's shape, which
is the mistake `D-2026-08-26-the-driver-s-signature-is-the-schema` records.

### `chemclaw.api.mcp_face` — read-only first, and that is the whole trick

The advertised set is **derived**: registered in-process tools ∩ `agent.authz.READ_ONLY_TOOLS`,
minus `TURN_SCOPED`. Nothing here can launch a job, propose a note, write a preference or settle a
wait, so this exports the value with none of an effector's blast radius, and a caller reaching it
holds strictly less authority than one talking to the front door.

**Read-only is necessary and not sufficient**, which is the finding worth carrying. Five read-only
tools read the *turn's own* state — an attachment, a watch, a preference, a clarifying question —
and an external caller has no turn. Four of them would answer emptily. `read_attachment` would not:
it returns the contents of a file somebody uploaded to a conversation, and serving that to whatever
holds a bearer token is a disclosure surface rather than a capability. `TURN_SCOPED` is a hand-kept
deny-list because nothing in the tree classifies "turn-scoped", and what makes it safe is that it is
a **partition** — `tests/test_mcp_face.py` asserts every read-only tool is either advertised or
named there, in both directions.

**The face states its own credential rather than inheriting an absence**, and this was a real defect
in the first draft. `connector_app` derives its bearer requirement from a discovered
`connector.yaml`; an app with no manifest beside the module is treated as *synthetic* and left
**open** — right for the bare apps the transport tests build, and catastrophically wrong for a
surface exposing the whole knowledge graph. `connector_app` now takes an explicit `token_env`, which
the face passes and which skips the manifest lookup entirely. An explicitly supplied but unset
variable fails closed on the existing `not expected` branch.

It is **not** addressable as a connector, and a test holds that: a `chemclaw-read` entry in
`CHEMCLAW_CONNECTOR_URLS` would make this deployment dial itself over HTTP for a narrower copy of
tools it already holds in process, and put the audit trail's own reader behind a network hop.

## Consequences

- `mcp-face` is a fifth process role in the one image. Its own role rather than a flag on `service`,
  because it serves a different surface on a different credential and must be scalable and
  revocable independently — a front door that also spoke MCP would have one token standing in for
  two very different authorities.
- A deployment enabling `webhook` owes the chart's `networkPolicy.egressDestinations` an entry. A
  host the policy drops fails every delivery with a timeout that reads as the destination being
  down, which is the failure mode `D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` is about.
- `make channel-validate` joins the validator set, checking the same three things the sink validator
  does — over *discovered* rather than enabled manifests, for the reason recorded there.
- Nothing reads *from* a channel, and an absence test holds it. A `fetch` or `poll` on the driver
  Protocol would make this a second, ungoverned way into the corpus — the mirror of the rule
  `ingest/sources/README.md` states in the other direction.
