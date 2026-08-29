# The readiness sweep's remaining unbounded halves (D-2026-08-29-a-per-read-timeout-is-not-a-budget)

Context: `fca8f4a` bounded the connector sweep's *queue* leg to one
`connector_health_timeout_seconds` and gave the chart explicit probe timeouts. An independent review
found the argument that fix made — "`/readyz` is inside a kubelet probe, so the number a deployment
reads must bound the whole answer" — unfinished in three places. All three were reproduced by
measurement before being fixed.

## 1 — the HTTP half was bounded by a timeout that restarts

- [x] Reproduced: a `/healthz` trickling one byte every 1.5 s held `_probe_endpoints` for
      **16.56 s** against the shipped 2.0 s budget, and reported the connector **`healthy`**.
- [x] `asyncio.wait_for` around each endpoint probe — the queue leg's own instrument. Per endpoint
      rather than per sweep, because HTTP probes share only a pool while the queue leg shares one
      `connect()`; the per-connector verdict survives and the sweep is bounded by concurrency.
- [x] The client keeps `timeout=` as the socket-level bound (no half-open connection left in the
      pool for the next sweep), which is all it was ever able to be.
- [x] After: **2.04 s**, `unreachable`, detail naming the budget.
- [x] Two tests, both failing against the pre-fix module: one on the wall clock over two concurrent
      endpoints, one on the verdict alone (a fix that bounded the wait and reported `unprobed`
      would leave the gauge as wrong as it was).

## 2 — one budget across connect and RPC is right for a poll and wrong for a boot

- [x] `connector_startup_health_timeout_seconds` (10.0, 5x the poll's), ENV-overridable, documented
      in `.env.example`. New rather than a multiplier: the two numbers answer to different
      constraints (a kubelet stopwatch vs a startup probe's 300 s) and neither derives the other.
- [x] `probe_connectors(budget=None)`; `check_connectors_at_startup` passes the startup budget.
      Read from `settings` at call time, so an override (and a test) is honoured.
- [x] Test drives one broker with a cold connect and an empty queue: `unknown` at the poll's
      budget, `unpolled` + `ConnectorsUnavailable` at the boot's, and the RPC itself is asserted to
      have received the larger deadline (not just the `wait_for` above it).
- [x] A settings test pins that the two defaults stay materially apart.

## 3 — the chart's probe timeout was guarded against the wrong number

- [x] Reproduced: with the app set to 9 s of connector sweep (11 s of readiness work), the old
      guard compared the chart's literal 5 against the *test runner's* `Settings` (4) and **passed**
      while the kubelet gave up 6 s early.
- [x] `readinessProbe.timeoutSeconds` derived from `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS` +
      `CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS` + `probes.service.readiness.marginSeconds`,
      both config keys through `required` — the `terminationGracePeriodSeconds` shape 30 lines up.
- [x] `ceil`, not `int`: float seconds truncated round the kubelet's patience *down*.
- [x] Shipped defaults still render `timeoutSeconds: 5` — nothing moves for a release that changes
      nothing. 9 → 8; 2.5 → 6; either key `null` → the render refuses, naming the key.
- [x] The guard renders the chart and compares the probe against the `chemclaw-config` ConfigMap,
      plus a drift test, a rounding test, both keys added to the `required`-refusal parametrize, and
      a chart-vs-code-default test.

## Review

- `helm` (3.16.3) and `kubeconform` (0.6.7) were **not** installed in this sandbox and are now, so
  none of the chart assertions were skipped: `tests/test_deploy_chart.py` runs 121 (was 115 with
  helm absent) and `make helm-validate` renders and validates (29 valid, 1 skipped Route).
- Not done, deliberately: the whole-sweep wall clock is the per-endpoint bound plus pool
  scheduling. Unreachable with seven bundles against httpx's default 100 connections, and moving
  the bound outward would lose the per-connector verdict. Stated in the ADR's consequences.
- Not measurable here: the connect-phase double-charge (httpcore applies the connect timeout to TCP
  connect and TLS handshake separately). A stalled handshake after an instant loopback connect
  measured 2.01 s against 2.0 s — one charge, because a local connect is free. The doubling needs a
  slow-but-succeeding connect, which is a property of a network; the wall clock bounds it either
  way, and the ADR says only that.
