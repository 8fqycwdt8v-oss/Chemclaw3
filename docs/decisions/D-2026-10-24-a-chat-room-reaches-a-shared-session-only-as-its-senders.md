# D-2026-10-24-a-chat-room-reaches-a-shared-session-only-as-its-senders — a Teams bridge posts each message on its sender's own Entra token

**Status:** accepted · **Date:** 2026-10-24 · **Builds on**
`D-2026-09-27-in-a-shared-session-the-sender-governs`. Decided by the owner 2026-10-24, option 1.

## Context

The backlog row *A chat-room connector over a shared session* waited on the shared session's queue,
fan-out and members (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`,
`D-2026-10-17-a-hand-over-is-one-decision-and-the-line-names-its-turn`). Those shipped, and
`Chemclaw3_ui` renders the queue, withdrawal and a live view of another member's turn. What was left
was putting ChemClaw into a chat room people already use, which is a choice about identity:

- The API is sufficient for a bridge that holds each sender's own token: a message is
  `POST /sessions/{id}/messages` as its sender, the answer is that request's stream, the line is
  `GET /sessions/{id}/queue`, and the room's people are the session's members. No core route is missing.
- It is not sufficient for a bridge that posts as itself. In a shared session the sender governs:
  roles, gates, spend and memory are read from the token that authenticated the request. One service
  credential would run every person's message on the bot's rights, the shape
  `D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned` refuses.
- Any chat platform is an egress decision, and only Entra identities are principals here.

## Options

1. **A Teams bot, each message on its sender's Entra token (on-behalf-of).** Teams single sign-on
   gives the bot the sender's token for the bot's own API; the bot exchanges it on-behalf-of for
   ChemClaw's audience. The bridge is a transport, not a principal. Costs an Entra registration with
   consent, an Azure Bot resource, an egress rule and a channel-to-session binding.
2. **A webhook bridge for a chat system without Entra identities (Slack, Mattermost).** Needs a map
   from chat user to Entra principal and a way to mint that principal's token: an impersonation
   service core does not have and must not grow. Rejected.
3. **Decline until a team asks.** The browser's shared conversation is the room. Costs nothing and
   ships nothing; the owner chose to build the Teams bridge now, so it is not taken.

## Decision

Option 1, built in `src/chemclaw/rooms/`, off by default (`CHEMCLAW_TEAMS_BRIDGE_ENABLED`). Declaring
a capability and binding it into a deployment are separate decisions, and this is the first.

- **No impersonation path in core.** Core is unchanged. The bridge calls the public routes with the
  sender's bearer; the front door validates it as it validates a browser's.
- **The bridge holds no token that could post as another sender.** A token is cached under the
  (tenant, object id) it was issued to, in memory, bounded, dropped before expiry. The exchange
  needs the sender's own assertion beside the bot's secret; both the assertion and the returned token
  are checked to name the sender, so a token for somebody else is refused before it is held. The
  bot's other credential (client credentials, Bot Framework scope) reaches Teams and nothing at
  ChemClaw, and the bot app is granted no app role on ChemClaw's API.
- **An unverifiable sender is refused, never posted as the bot.** An activity is acted on only after
  its Bot Framework token verifies (signature, issuer, audience, `serviceurl`); the sender is the
  activity's Entra object id in the configured tenant; no sign-in yet is a sign-in card and no post.
- **Not a connector, not a delivery channel.** A connector serves tools to the model, and this
  serves none. `deliver/` is outbound and read-only by rule (nothing there reads from a channel),
  and the bridge is mostly inbound. It is a client of the front door, so it is a leaf package
  (`rooms` imports the kernel only), run as its own process, and the model reads nothing it adds:
  a message is posted verbatim, so no model-text evaluation applies.
- **Binding is a person's act, and not authority.** `!new` or `!bind` runs under the binder's token
  and `!bind` requires that they can reach the session; the file records who bound it. Every
  sender is still admitted by the session's members as themselves.
- **Not applied here:** the egress rule. The in-process guard derives the bridge's four destinations
  only when it is enabled; the NetworkPolicy and the tenant registration are the operator's, in
  `docs/guides/teams-room-bridge.md`.

## Consequences

`tests/test_room_bridge.py` runs the real front door against a fake Teams and Entra
(`tests/fakes_teams.py`) and reads the sender from inside the turn; each guard was watched failing
under its mutation. A message waiting in the line is reported to the room by place and when it
starts; another member's browser turn is not mirrored into the room, because that needs a member's
token held with no sender present. The silent single sign-on path was rehearsed against fakes only;
the runbook's verification section is the owner's first live check. One replica only (the bindings
file). Plan approvals stay in ChemClaw.

Revisit when: a chat system without Entra identities is asked for (option 2 stays declined until
core has a sanctioned way to mint a person's token), or when the owner wants a room to follow a
member's browser turn, which needs a decision on a held member credential.
