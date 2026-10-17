# D-2026-10-17-a-served-connectors-sessions-are-bounded-by-settings — the MCP sessions a connector this release serves are bounded by two settings

**Status:** accepted · **Date:** 2026-10-17

## Context

GHSA-84m7-p3x7-pcfv (CVE-2026-59951): the `mcp` SDK before 1.30.0 accumulates Streamable HTTP
sessions without bound, because `FastMCP` passed no idle timeout and nothing capped the count.
Every connector this release serves (`connectors/server.py::connector_app`: the bundles with a
`server/` package, and the read-only face in `api/mcp_face.py`) was in that state — a session lived
until its client sent `DELETE`. 1.30.0 adds two `FastMCP` settings, `session_idle_timeout` (default
1800 s; a session with no HTTP request in flight, an open stream counting as one, is closed and its
id then answers 404) and `max_sessions` (default 10 000; past it a new session gets 503), and the
bump to 1.30.0 turns both on with those values.

Two constraints bear on the values. The pool in `connectors/session_pool.py` keeps an idle session
for `connector_session_ttl_seconds` before lending it to the same actor's next turn, so a served
reaper shorter than that keep time would turn every kept session into a replaced one. And the
sessions a connector serves are mostly this release's own pool's: up to `connector_session_pool_max`
per core process, so a cap near that number times the process count would refuse legitimate turns.

## Options

1. **Inherit upstream's defaults.** No code; but the bounds are upstream literals nobody here can
   read or override, and "0 means off" (this config's convention) is unavailable.
2. **Two settings, defaulting to upstream's values.** `connector_server_session_idle_timeout_seconds`
   and `connector_server_max_sessions`, ENV-overridable, 0 switching either off, applied to
   `server.settings` before `streamable_http_app()` builds the session manager; a startup check
   refuses an idle timeout that does not outlast the pool's keep time.
3. **Two settings, with a cap derived from the connector pod's memory**, as `Chemclaw3-mcp` derives
   `MCP_MAX_SESSIONS` from its smallest pod limit and a measured per-session cost. Needs that cost
   measured on this release's own connectors, and a cap that low collides with the pool's own peak.

## Decision

Option 2. It makes both bounds this deployment's to state and change, keeps the values the SDK
authors chose for the advisory, and holds the one ordering that matters (served idle timeout above
the pool's keep time) at startup. Option 3 is the better bound in principle and is not taken now:
it needs a measurement this change does not have, and a cap at the pool's scale would refuse turns
rather than an attacker.

## Consequences

- `tests/test_connector_transport.py` drives a real connector: a handshake past the ceiling gets
  503, a session idle past the timeout answers 404, and 0 reaches upstream as `None`.
- `tests/test_config.py::test_a_served_session_outlives_the_pool_that_keeps_it` holds the ordering.
- A connector served by `Chemclaw3-mcp` is bounded by that repository's own `MCP_MAX_SESSIONS` and
  `MCP_SESSION_IDLE_TIMEOUT_SECONDS`, not by these settings.

Revisit when: a per-session memory cost is measured on a connector this release serves, or the
connector pods' memory limit (`resources.connector` in `deploy/helm/chemclaw/values.yaml`) changes.
