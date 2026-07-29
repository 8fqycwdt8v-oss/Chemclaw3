# D-136 — The shipped defaults were never executed: three configurations that fail on first contact

An intense review of the agentic system, asked to find ways to make it faster *and* more reliable
at once. The performance leads it started from were mostly already fixed (D-119 pooling, D-121
multi-process, the gathered retriever fan-out), so what it found instead was a class of defect the
1176-test suite is structurally unable to see, and three live instances of it.

**The class: a value that is only wrong at a boundary no test crosses.** Every test injects a fake
chat client, so no test has ever sent a generation parameter to a real model endpoint. Every chart
test constructs `Settings(**helm_values)`, so no test has ever *executed* a production config
value. Both suites are green, thorough, and blind in the same direction — they validate shapes,
and these defects are all about what happens when a shape meets a real system.

**Instance 1 — the default config could not complete a single turn.** `build_agent` always put
`temperature` on the wire from `llm_temperature` (default `0.0`). The shipped `agent_model`,
claude-sonnet-5, rejects it: `400 invalid_request_error: temperature is deprecated for this
model`. Every turn on the default Anthropic path failed on first contact. Found by capturing the
real outgoing request for one turn. `llm_temperature` is now `float | None`, unset by default, and
the key is omitted from `ChatOptions` entirely when None — omitting is not the same as sending
null, which the API also rejects.

**Instance 2 — the shipped chart could not start a pod.** `values.yaml` sets
`CHEMCLAW_OTEL_ENABLED: "true"`; no OpenTelemetry SDK or OTLP exporter was declared in
`pyproject.toml`. `configure_telemetry()` runs unconditionally at process start in the front door,
the background worker and every connector worker, so all of them raised and the ASGI lifespan
returned `lifespan.startup.failed`. On a real cluster the whole deployment CrashLoopBackOffs;
only the six connector MCP servers stay up, serving tools no agent can reach. The dependencies are
now declared, and the new test *executes* `configure_telemetry()` under the shipped value.

**Instance 3 — a raising agent factory deadlocked the pod permanently.** `AgentPool._checkout`
incremented `_built` before calling the factory, so a factory that raised burned a slot: the pool
counted an agent that never existed and never reached the free queue. After `size` such failures
it could neither build nor hand one out, and every later turn blocked for the full
`service_turn_timeout_seconds`, forever, on a pod still reporting healthy. Reachable: the factory
reads the TLS CA bundle from disk and requires a credential, so a cold pod taking its first turns
before its secret volume is populated hits exactly this. The count is now committed only after the
agent exists.

**Also landed, from the same review — the per-turn connector seam, three consequences of one
design decision.** A connector tool is built fresh per turn, which is a correctness requirement
(D-118). Three things followed from it that were not intended:

- *Every connector call was capped at 5 s.* The per-turn `httpx.AsyncClient` was constructed with
  no `timeout=`, so httpx's 5 s default applied to every phase, while `request_timeout` bounded
  only the MCP application-level wait. Measured against a real server: an 8 s tool call had its
  HTTP stream torn down at 5 s, the MCP response never arrived, and the caller then blocked for the
  *full* `request_timeout` before failing — 60 s for calc, holding an admission permit and an agent
  lease throughout. `request_timeout` was not preventing a hang; it was setting its length. A tool
  slower than 5 s is ordinary here: an uncached `predict_pka` runs xTB inline.
- *Six `httpx.AsyncClient`s leaked per turn.* Neither layer below takes ownership of a
  caller-supplied client — MCP enters it into an exit stack only when it created it, and MAF's
  `close()` never touches it. The same leak class D-119 fixed for Postgres, on the connector side.
  `DegradingHttpConnector` now closes what it was handed.
- *The six connects were serial.* `connectors.health.probe_connectors` already gathers its probes
  with the rationale "the sum of the timeouts rather than the slowest one"; the path every turn
  actually takes did not. Gathering is safe for the per-turn-instance rule, which is about object
  lifetime rather than connect ordering, and MAF runs each connector's lifecycle on its own task.

**Measured, and the reason caching is the next thing worth doing.** Capturing the real request for
one turn: the fixed prefix is **14,595 tokens** before the chemist says anything — 3,463 of system
instructions plus skills manifest, 11,132 of tool schemas — rising to ~20.5 k once the connector
MCP tools are attached. There is no prompt caching anywhere in first-party code (`cache_control`
appears zero times), so that prefix is re-paid on every model call, and up to 25 times per turn in
harness mode. This is recorded rather than fixed: MAF's Anthropic client exposes structured
instruction blocks, which reaches the system half, but offers no `cache_control` hook for `tools`
— the 11 k that dominates. See `BACKLOG.md`.

**What this changes about how to test this system.** A green suite proved these paths were
*shaped* correctly. The gap is that a shipped default is a claim about the world, and the only way
to check it is to run it. The new tests execute production values rather than validating them; the
chart parity test should grow the same property.
