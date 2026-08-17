# Verdicts — `sweep-resources.md`, lens: does it actually reproduce?

Scope note: the findings file contains **one** finding at critical/high severity (the pool-count
one, `high`). The other three — `memory_store()` publishes before `setup()`, the HPC per-poll TCP
connection, and the never-closed checkpointer pool — are all marked `medium` and are therefore out
of scope and not verified here.

Working tree checked first: `git status --short` shows only untracked audit files, `HEAD` =
`0da9f3d457b7cec9d8ad6089a42a0fb3ce85b4cc`. No mutation markers, no diff against the pristine copy
needed.

---

## A process opens three Postgres pools; every bound and every gauge counts one

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

I did not run the reporter's `t5.py`/`t6.py`. I wrote two scripts of my own from the source.

**1. `/tmp/repro_pools.py`** — enters the real `db.pooling()`, makes one ordinary store-shaped
`db.connection(dsn)` call, then makes the *readiness* call with the arguments I read out of
`src/chemclaw/api/routes/ops.py:77-80` at HEAD, then builds the checkpointer pool via the same
entry point `api/runner._turn_checkpointer()` reaches (`agent.checkpointer._checkpoint_pool`), and
finally reads the actual bound gauges. Against the live Postgres it printed:

```
settings.pg_pool_max_size = 16
settings.pg_statement_timeout_seconds = 30.0
settings.service_readiness_db_timeout_seconds = 2.0

distinct core pools in db._POOLS: 2
   options='-c statement_timeout=30000'  max_size=16
   options='-c statement_timeout=2000'   max_size=16
checkpointer pool max_size: 16
checkpointer pool in db._POOLS? False

REAL per-process ceiling: 48
gauge chemclaw_pg_pool_max_size: 16.0
db.pool_stats(): {'pool_size': 6, 'pool_available': 4, 'requests_waiting': 0}
fleet check computes: 16 (pooled_processes=1)
while holding a checkpointer connection, db.pool_stats() = {'pool_size': 6, 'pool_available': 6, 'requests_waiting': 0}
   checkpointer's own stats: {... 'pool_max': 16, 'pool_size': 1, 'pool_available': 0 ...}
```

The last two lines are the metric-blindness half, and they are stronger than the reporter's
transcript: while a checkpointer connection is *checked out* (`pool_available: 0` on the
checkpointer's own stats), `db.pool_stats()` reports `pool_available: 6` — full availability. The
saturation signal cannot see a borrowed connection on the pool that serves every turn's state.

**2. `/tmp/repro_readyz2.py`** — the same question driven only through HTTP, so nothing depends on my
choosing the arguments. Builds the real app with `create_app()`, runs the real lifespan under
`TestClient`, and hits `/healthz`, `/readyz`, `/metrics`:

```
pools after lifespan start: []
/healthz 200
/readyz 200 {'status': 'ready', ...}
distinct core pools after /readyz: 1
    -c statement_timeout=2000 -> max 16 min 2 size 3
METRIC: chemclaw_pg_pool_size 3
METRIC: chemclaw_pg_pool_max_size 16
```

One kubelet readiness probe, on its own, materialises a pool the stores will never share — and it
is not a token pool: `pg_pool_min_size` defaults to **2**, so it holds warm connections
permanently (3 open here) for the life of the pod.

**3. Chart arithmetic, rendered rather than read.** `helm template t deploy/helm/chemclaw`:

```
CHEMCLAW_PG_POOL_MAX_SIZE: "8"
CHEMCLAW_PG_FLEET_POOLED_PROCESSES: "15"
CHEMCLAW_PG_FLEET_MAX_CONNECTIONS: "136"
```

`values.yaml:341` sets `CHEMCLAW_SESSION_STORE: "postgres"`, so `_turn_checkpointer()` returns a
real saver and the third pool exists on every front-door pod that takes a turn.
`templates/_helpers.tpl:497` counts *processes* — front door at `autoscaling.maxReplicas` (6), plus
workers, plus connector pods — one pool each, explicitly ("pools once each").

- Checked/declared: 15 × 8 = **120** ≤ 136 → `Settings` passes.
- Actual ceiling: 6 front-door × 3 pools × 8 = 144, plus 9 other pooled processes × 8 = 72 →
  **216** > 136.
- Alert LHS: `sum(chemclaw_pg_pool_max_size)` = 15 × 8 = **120**, so
  `ChemclawFleetAboveItsConnectionCeiling` (`templates/prometheusrule.yaml:254`) can never fire on
  this shape either.

**4. Every cited symbol and line is real and current.** `grep -rn "AsyncConnectionPool(" src/`
returns exactly two constructors — `core/db.py:148` and `agent/checkpointer.py:367` — so "three
pools" is 2 keys in `_POOLS` plus the checkpointer, and there is no fourth hiding. `_POOLS` is at
`db.py:60`, the constant-setting gauge at `db.py:238`, `pool_stats` at `db.py:269`, the fleet check
at `core/config/__init__.py:217-226`, the probe at `ops.py:77-80`, the `max_size × distinct DSNs ×
processes` sentence at `core/config/store.py:81`. `grep -rn "statement_timeout_seconds="` over
`src/` confirms `ops.py:79` is the *only* non-default call site, so the count is 2 core pools and
not more.

**5. The control encodes the same error.**
`tests/test_deploy_chart.py:979` (`test_the_shipped_connection_ceiling_matches_the_fleet_the_chart_renders`)
asserts `processes * per_pool <= declared` — the identical one-pool-per-process assumption. So the
test that exists to catch this passes precisely because it repeats the mistake.

### Why

Every element of the claim reproduced independently, in the real process, with my own scripts: the
two `_POOLS` keys arise from ordinary traffic (one store call, one kubelet probe), the checkpointer
is a third pool by construction and is registered nowhere, the max-size gauge is a bound *constant*
(`lambda: float(settings.pg_pool_max_size)`) rather than a sum over pools, `pool_stats()` iterates
`_POOLS.values()` only, and the `Settings` validator multiplies `pooled_processes × pg_pool_max_size`
with no term for pools-per-process. Three separate mechanisms that exist to bound the number all
compute 1× where the code opens 3×, and the shipped chart's own numbers cross its declared 136 as
a result. The two docstrings the finding quotes (`db.py:216`, `db.py:222`) do assert the property
that is false — I checked the text against the code, and "a process cannot acquire a pool without
also acquiring its witness" is untrue of `_checkpoint_pool`, which never calls `bind_pool_metrics`.

Two corrections that do not change the verdict:

- The finding says the real fleet figure is "~232". Rendering the chart gives **216**: it used the
  "17 pooled processes" from `values.yaml`'s own (stale) prose comment, where the helper actually
  renders **15**. Direction and the >136 conclusion are unaffected.
- "6 × 3 × 8 = 144" is a *ceiling*, and the readiness pool will not realistically hold 8 — its
  probe is cached and serialized. But the realistic loaded figure is still ~8 (core) + ~8
  (checkpointer) + 2 (readyz `min_size`) ≈ 18 per front-door pod against a declared 8, i.e. still
  a 2.25× under-count, and 6 × 18 + 9 × 8 = 180 > 136 even on that conservative reading. And every
  mechanism at issue — the validator, the gauge, the alert — is explicitly ceiling arithmetic, so
  the ceiling is the right comparison anyway.

I would keep **high**, not raise to critical: the consequence is connect failures against an
idle-looking database at full scale (D-119's presentation) plus a blind saturation metric, which is
an availability failure under load rather than data loss or a security boundary. The thing that
argues *for* high is that all three guards and the chart test agree with each other and are all
wrong together, so nothing in the system can currently report it.
