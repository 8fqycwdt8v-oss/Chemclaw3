# D-2026-10-23-a-chat-room-reaches-a-shared-session-only-as-its-senders — which chat room, if any, and whose token carries a message

**Status:** proposed · **Date:** 2026-10-23

## Context

The backlog row *"A chat-room connector over a shared session"* waited on the shared session's
queue, fan-out and members (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`,
`D-2026-10-17-a-hand-over-is-one-decision-and-the-line-names-its-turn`). Those have shipped, and
`Chemclaw3_ui` already renders the queue, withdrawal and a live view of another member's turn, so
the row's "queue/stream UI" half is done. What is left is putting ChemClaw into a chat room
people already use, and that is a choice, not an implementation step:

- **The API is sufficient for a bridge that holds each sender's own token.** A message is
  `POST /sessions/{id}/messages` as its sender, the answer is that request's stream, other
  members' turns are `GET /sessions/{id}/turn/stream`, and the channel's people are the session's
  members (`PUT /sessions/{id}/members/{actor}`). Nothing new is needed in core for that shape.
- **It is not sufficient for a bridge that posts as itself.** In a shared session the sender
  governs (`D-2026-09-27-in-a-shared-session-the-sender-governs`): roles, gates, spend and memory
  are the sender's, read from the token that authenticated the request. A bot holding one service
  credential would run every person's message on the bot's roles, which is the shape
  `D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned` refuses.
- **Every chat platform is an egress and an identity decision.** The front door has no outbound
  path to a vendor today (`CLAUDE.md`, "Not in this system": no egress), and only Entra identities
  are principals here.

## Options

1. **A Teams bot, each message on its sender's Entra token.** Teams single sign-on gives the bot
   the sender's token, exchanged on-behalf-of for core's audience; the bot is a bridge, not a
   principal. Costs an Entra app registration with on-behalf-of consent, a Bot Framework egress
   rule, a channel-to-session mapping owned by a person, and a deployment that runs the bridge.
   No core route changes.
2. **A webhook bridge for a chat system without Entra identities (Slack, Mattermost).** Needs a
   way to map a chat user to an Entra principal and mint that principal's token, which core does
   not have and which would be an impersonation service. Not buildable without a new identity
   subsystem.
3. **Decline for now.** The browser's shared conversation is the chat room; people who want a
   colleague in a conversation add them as a member.

## Decision

Proposed, for the owner to decide. Recommendation: **3 now, 1 when a team asks**, because option 1
costs a tenant-level registration and an egress rule for a demand nobody has stated, and option 2
weakens the sender-governs rule the shared session is built on.

## Consequences

Under 3 the backlog row is closed with this record and the bridge is not built. Under 1 the work
is a separate deployable (not a connector bundle: it serves no tools), and its acceptance test is
a message from two Teams users in one channel running under each user's own roles, read from
inside the turn as `tests/test_session_turn_queue.py` reads them.

Revisit when: a named team asks for ChemClaw in a Teams channel, or the deployment gains an
outbound egress rule to a chat vendor.
