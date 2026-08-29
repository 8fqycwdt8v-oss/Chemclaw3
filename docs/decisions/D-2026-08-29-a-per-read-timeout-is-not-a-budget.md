# D-2026-08-29-a-per-read-timeout-is-not-a-budget — the readiness sweep's other three unbounded halves

**Status:** accepted

## Context

`D-2026-08-27-a-queue-with-no-poller-is-unreachable` gave the connector sweep its durable half, and
a later commit bounded that half's cost: the connect and the RPC used to carry
`connector_health_timeout_seconds` one each, so a broker reachable enough to accept a connection and
then blackhole the call spent twice the number the deployment's probe `timeoutSeconds` is derived
from. That fix is right and its own test drives it. What it did not do is finish the argument it
made, which was that **`/readyz` runs inside a kubelet probe, so the number a deployment reads must
bound the whole answer.** Three things still stood outside that bound, each measured here.

### 1. The HTTP half was bounded by a timeout that restarts

`_probe_endpoints` passed `connector_health_timeout_seconds` to `httpx.AsyncClient`, which is a
**per-operation** timeout rather than a budget: the read leg's deadline restarts on every socket
read. Measured against the shipped 2.0 s number, a `/healthz` trickling one byte every 1.5 s held
`_probe_endpoints` for **16.56 s** — and the verdict was worse than the latency. The response did
eventually arrive with a 200, so the connector was reported **`healthy`**: counted healthy by
`chemclaw_connectors_unhealthy`, cleared by `connectors_required`, and readmitted by the breaker,
for a host no turn could get an answer out of.

The connect leg has the same shape one level down, because httpcore charges the connect timeout
separately to the TCP connect and to the TLS handshake. That half is stated rather than measured:
in this sandbox a stalled handshake after an instant loopback connect cost **2.01 s** against a
2.0 s budget — one charge, because a local TCP connect is free. Producing the doubling needs a
slow-but-succeeding connect, which is a property of a network rather than of a socket API, so what
is claimed here is only what the wall clock now makes true regardless: one bound over both phases.

### 2. One budget across connect and RPC is right for a poll and wrong for a boot

Sharing the budget is what fix 1 of that earlier commit did, and on the hot path it is correct: a
`/readyz` sweep runs every 10 s per pod, reuses a cached Temporal client, and is wrong for at most
one period. The **first** check after process start has no cached client. It pays PEM parsing and an
mTLS handshake out of the same budget the `DescribeTaskQueue` needs in order to answer, and running
out of it reports `unknown` — a state that by deliberate design (`D-2026-08-08-an-outage-is-not-a-missing-job`)
neither counts in the gauge nor trips the gate.

So the one sweep whose verdict is irreversible could report a queue with no poller — a worker fleet
at zero replicas, jobs accepted and never run — as "could not measure", and `connectors_required`,
the posture that exists to refuse exactly that, would clear it. Driven in
`tests/test_connector_health.py`: one broker, one empty queue, a cold connect; at the poll's budget
the sweep says `unknown`, at the startup budget the same broker says `unpolled` and the gate raises.

### 3. The chart's probe timeout was guarded against the wrong number

`readinessProbe.timeoutSeconds` rendered the literal `5` — 4 s of stated work plus a margin — beside
two budgets an operator changes through `.Values.config`. The guard that was supposed to hold them
together derived its floor from **the test runner's own `Settings` object**, so it compared the
chart's literal against the code defaults: a pair that agree in CI no matter what a release does.
Measured, with `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS` set to 9 (11 s of readiness work), the
old guard evaluated `5 >= 4` and **passed**, while the kubelet gave up 6 s before the route it was
calling had to answer — draining a front door that was serving correctly. That is the same failure
mode the block was written to prevent, reached from the other side.

Thirty lines above it, `terminationGracePeriodSeconds` was already doing the right thing:
`.Values.config.CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` through Helm's `required`, so an absent key
refuses the render instead of silently producing a plausible wrong number.

## Decision

**A bound on a network answer is a wall clock, not a timeout kwarg.** The HTTP probe is wrapped in
`asyncio.wait_for`, the same instrument the queue half already used. It is **per endpoint** where
the queue half bounds its whole leg, and the difference is structural: the queue half shares one
`connect()`, so a per-bundle bound could not describe time the shared connect already spent, while
HTTP probes share only a pool. Bounding each one keeps the per-connector verdict — five answering
and one dark reports exactly that — and the sweep still returns inside one budget because the probes
run concurrently. The client keeps its `timeout=` as the socket-level bound, so a cancelled probe
does not leave a half-open connection for the next sweep; what it is not, and was relied on to be,
is a bound on the answer.

**The sweep takes its budget as an argument, and the boot passes a bigger one.**
`connector_startup_health_timeout_seconds` (10.0, five times the poll's 2.0) is a one-time cost at
start, bounded by a startup probe that already grants 30 x 10 s. The poll's budget cannot be raised
for the same benefit without moving the kubelet's patience with it — which is precisely the coupling
the chart now makes visible rather than a reason to leave the boot under-budgeted.

**The probe timeout is derived from the two budgets the route spends**, with `required` on both keys
and `ceil` rather than `int` — these are float seconds and truncation rounds the kubelet's patience
*down*, reintroducing the gap. The shipped defaults render the same `timeoutSeconds: 5` as before,
so a release that changes nothing sees nothing change. The guard now renders the chart and compares
two **rendered** numbers, the probe against the `chemclaw-config` ConfigMap the pods actually
receive, so an override reaches both sides of the comparison.

## Consequences

- A trickling or blackholed `/healthz` costs one budget and is reported `unreachable`, not
  `healthy`. Measured: 16.56 s → 2.04 s.
- `check_connectors_at_startup` can take up to `connector_startup_health_timeout_seconds` per
  connector at boot. That is inside the startup probe's budget by a wide margin, and it buys a
  verdict that is right on the one sweep that cannot be repeated.
- An operator who raises either readiness budget through `.Values.config` gets a probe timeout that
  moves with it; one who removes either key gets a refused render naming the key.
- What is still not covered: the whole-sweep wall clock is the per-endpoint bound *plus* pool
  scheduling. With the shipped fleet (seven bundles) against httpx's default 100 connections this
  is not reachable, and `test_the_http_half_bounds_the_answer_rather_than_each_socket_read` sweeps
  two endpoints concurrently so a serialising regression fails there. A fleet large enough to queue
  on the pool would need the bound moved outward, and would lose the per-connector verdict for it.

## Rule

**A timeout kwarg names an operation; a budget names an answer.** Where a caller is a kubelet, a
gate or anything else with a stopwatch, bound the answer — and where a number in a chart has to
agree with a number in the app, derive it rather than write it, because a guard that reads the test
process's own configuration is not reading the deployment's.
