# `rooms/` — a chat room as a surface of a shared session

**Where a Teams channel talks to ChemClaw.** A client of the front door's public routes, not a
fifth seam: a delivery channel only sends (nothing in `deliver/` reads from a channel), and a
connector serves tools to the model, while the bridge serves no tool and the model reads nothing
it adds. It is a separate process (`python -m chemclaw.rooms`) off until
`CHEMCLAW_TEAMS_BRIDGE_ENABLED` is set. The setup is `docs/guides/teams-room-bridge.md`; the
reasoning is `D-2026-10-24-a-chat-room-reaches-a-shared-session-only-as-its-senders`.

```
rooms/
├── identity.py    # the sender's own token: on-behalf-of exchange, and the bounded per-person cache
├── teams.py       # activities, channel-token verification, the sender, and the bot's replies
├── frontdoor.py   # the five front-door routes, each called with one person's token
├── bindings.py    # room -> session, in a file on a mounted volume
├── bridge.py      # the flow: verify, name the sender, command or post, relay
└── serve.py       # settings -> collaborators, the ASGI app, the start-up refusals
```

## The rule

A message is posted only on a token issued to the person who sent it. There is no call in this
package without a person's `AccessToken`, and no credential of the bridge's own that ChemClaw
accepts: the bot's client secret signs the on-behalf-of exchange (which needs the sender's
assertion beside it) and the bot's reply token (Bot Framework scope, reaches Teams only).

## Refused, never posted as the bot

An activity whose channel token does not verify, a sender of another tenant or with no Entra
object id, and a sender with no token yet (who is offered the sign-in card) post nothing.
`tests/test_room_bridge.py` runs the real front door and reads the sender from inside the turn.

## What it does not do

It does not mirror another member's browser turn into the room: that needs a member's token held
with no sender present, which is the standing credential the rule above excludes. It does not
approve a plan or an approval; those stay in ChemClaw. One replica only (the bindings are a file).
