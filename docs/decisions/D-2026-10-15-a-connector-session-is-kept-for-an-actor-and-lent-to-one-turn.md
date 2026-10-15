# D-2026-10-15-a-connector-session-is-kept-for-an-actor-and-lent-to-one-turn — an MCP session outlives the turn that opened it

## Status

Accepted.

## Context

A turn opened every bound connector's MCP session and closed it again: a dial, `initialize`, the
`initialized` notification, `tools/list`, the standalone SSE stream and a `DELETE`. Measured against
eight real fleet servers on loopback (`props`, `thermalsafety`, `kinetics`, `unitops`,
`suitability`, `safety`, `chem`, `pyexec`; 50 tools), 60 consecutive turns:

| Per turn, 8 bound bundles | Per-turn sessions | Kept sessions |
| --- | --- | --- |
| open before the first token, p50 / p95 | 112.3 / 238.7 ms | 22.5 / 29.5 ms |
| close after the turn, p50 | 33.2 ms | 0.1 ms |
| HTTP requests | 40 (24 POST, 8 GET, 8 DELETE) | 8 (one ping each) |
| request and response body bytes | 192,621 | 832 |

Almost all of the bytes are the eight `tools/list` answers, which carry the same schemas every turn.

Two facts about the code constrain the fix. The fleet's servers read `X-Chemclaw-Actor`,
`-Session`, `-Correlation-Id` and `-Dry-Run` from the request that carries each tool call, and treat
them as attribution only. And over streamable HTTP the request is issued by the transport's task,
which inherits the context of the task that opened the connection: `turn_identity_hook` reads
`ContextVar`s there, so a connection that outlived its turn would stamp the *opening* turn's
identity on every later call. The MCP SDK offers no per-call header seam (`ClientMessageMetadata`
carries only a resumption token).

## Options

1. **Keep one session per turn** — nothing changes, and 32 ms to 220 ms plus 190 KB stay on every
   turn's path to the first token.
2. **Share one session per (actor, connector) between concurrent turns, varying headers per
   request.** Not possible without patching the SDK's transport or rewriting each request body to
   find its caller; a mistake is a request carrying another turn's identity.
3. **Key by (actor, chat session) and share across that session's turns.** Safe only if a chat
   session never runs two turns at once, which nothing here enforces, and it reuses nothing across
   a chemist's sessions.
4. **Lend a session to one turn at a time, keyed by (actor, connector, endpoint)**: the holder task
   runs in an empty context and pins a mutable `IdentityPin` that the request hook reads; it is
   replaced at every checkout. A second concurrent turn of the same actor gets a second session.
   Costs a ping per reused session per turn.
5. **As 4, but detect a dead session by retrying the first failed call.** Free when healthy, but a
   retry is unsafe for a state-changing tool whose request may have reached the server.

For staleness of the cached `tools/list`: ask the server every turn (the cost being removed), trust
it until a `tools/list_changed` notification (the fleet advertises `listChanged: false`, so it would
never arrive), or key it by what the handshake already tells us and bound it by a TTL.

## Decision

**Option 4, with a ping at checkout.** The pin is what makes reuse safe: a session is used by one
turn at a time, its headers are written only at checkout, and its task reads nothing of the turn
that opened it. The key includes the actor as a second layer, so a bug in the pin could still never
cross actors.

- Before a kept session serves a turn its holder task must be alive, no request on it may have been
  abandoned (cancelled or timed out, which may still run at the server), and it must answer a
  `ping` within `connector_health_timeout_seconds`. Otherwise it is closed and one fresh session is
  opened in the same call, so a restarted server (404 on the old session id), a revoked bearer or a
  dead host costs the connector what it cost before. The bearer is read per request, so a rotation
  needs nothing.
- A connector's listing is kept for `(url, contract_version, server build)` and for at most
  `connector_session_ttl_seconds`. The contract version is the one `/healthz` last reported
  (`contract.remembered_version`), the build is `serverInfo.version` from the handshake, so a
  restart onto a new build or contract misses without a call of its own. A `tools/list_changed`
  notification, where a server sends one, discards the listing and the session. Tools are rebuilt
  from the listing by the adapter's own `convert_mcp_tool_to_langchain_tool`, so what the model sees
  is what a fresh listing returns, and the listing seeds the SDK's output-schema record so the
  first call validates exactly as before.
- Bounded: idle sessions expire after `connector_session_ttl_seconds` (300 s, under the fleet's
  1800 s reaper), at most `connector_session_pool_max` (256) are held, the least recently used idle
  one is closed first, and a turn that finds only busy sessions runs on an unpooled one.
  `connector_session_ttl_seconds: 0` restores a session per turn.
- Scope: HTTP connectors, in a process that entered `connector_sessions.pooling()` beside
  `backend_rpc.pooling()` (the front door and the background worker). Stdio connectors, queued
  calls, a process without the scope, and a turn with no connectors behave as before.

## Consequences

- A turn pays a ping per connector instead of a handshake, and the first token is not delayed by
  `tools/list`. The wire carries 8 requests where it carried 40.
- Fleet pods see a session per (core replica, actor, connector) for up to five idle minutes instead
  of one per turn; the ceiling per process is `connector_session_pool_max`, to be read against a
  fleet pod's `MCP_MAX_SESSIONS` (1024) times the replica count.
- A kept connection stamps its turn's `traceparent` as of checkout, not the active tool-call span's.
- A fleet server's MCP session lives in one pod's memory and its Service balances per TCP connection
  (no `sessionAffinity`, uvicorn closes an idle connection after 5 s). Within a turn one connection
  keeps the session on one pod; across turns a new connection may reach another replica, which
  answers 404 to the ping, and the session is replaced at the price of that one round trip. With
  `N` replicas the kept-session hit rate is therefore about `1/N` until the fleet's Services carry
  `sessionAffinity: ClientIP` (timeout above the TTL) or its servers run `stateless_http`. The
  measurements above are one pod per server.
- A server that dies *during* a turn fails that call exactly as it did (it waits out the call's
  read bound); only the next turn is repaired.

Revisit when: a fleet server advertises `tools.listChanged: true` (the TTL could then be raised),
or a fleet pod's session count (`/healthz` `bounds` against `MCP_MAX_SESSIONS`) nears its ceiling.

## What keeps it true

- `tests/test_connector_session_pool.py`: one `initialize` for five turns; every call carries its
  own turn's actor, session, correlation id and dry-run flag under four concurrent actors on one
  connector and under two concurrent turns of one actor; a restarted and a stopped server; TTL
  expiry in the sweep and at checkout; eviction at the ceiling and the all-busy overflow; shutdown
  closes every session and leaves no task; a cancelled call is not handed on; the listing is
  reused, invalidated by a contract version change and by the TTL; a rotated and a revoked bearer;
  a refusal reads the same pooled and per turn; the pooled tool surface equals a fresh listing's.
- `tests/test_upstream_surface.py`: the SDK still records output schemas where the pool seeds them.
