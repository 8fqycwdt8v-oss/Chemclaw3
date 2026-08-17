# Verdicts — sweep-resources, reachability lens

Scope: only findings marked **critical** or **high**. The file has exactly one — the three-pool
finding. The other three (`memory_store()` publish-before-setup, the HPC per-poll TCP connection,
the unclosed checkpointer pool) are **medium** and were not examined.

---

## A process opens three Postgres pools; every bound and every gauge counts one

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (unchanged)

- **What I did**

  Working tree checked first: `git status --short` shows only an unrelated untracked verdict file,
  `git log -1` = `581e3982`. Nothing mutated in the paths below.

  1. **Are there really three pools in one front-door process?** Reproduced against the live
     Postgres with a script that enters `db.pooling()`, makes one ordinary store call, one
     `/readyz`-shaped call, and then builds the checkpointer (`/tmp/pools_check3.py`):

     ```
     core pool keys: [('postgresql://...chemclaw', '-c statement_timeout=30000'),
                      ('postgresql://...chemclaw', '-c statement_timeout=2000')]
     checkpointer pool: True  pool_max 16
     REAL per-process max: 48
     --- gauges ---
     chemclaw_pg_pool_size 6
     chemclaw_pg_pool_available 6
     chemclaw_pg_pool_requests_waiting 0
     chemclaw_pg_pool_max_size 16
     core pool_stats: {'pool_size': 6, ...}
     checkpointer stats: {'pool_max': 16, 'pool_size': 1, 'pool_available': 1}
     ```

     Same DSN, two `_POOLS` keys, plus the checkpointer's own `AsyncConnectionPool`. The
     `chemclaw_pg_pool_max_size` gauge reports 16 where the process may open 48, and `pool_stats()`
     reports 6 where the process holds 7 connections.

  2. **Is the split reachable under the shipped chart, or does something upstream collapse it?**
     `grep -rn "statement_timeout_seconds=" src/` returns exactly two hits: `api/routes/ops.py:79`
     (`service_readiness_db_timeout_seconds`, default **2.0**) and `core/db.py:193` (the unpooled
     fallback). All 32 `db.connection(` call sites but that one take the default
     `pg_statement_timeout_seconds` = **30.0**. `deploy/helm/chemclaw/values.yaml` overrides
     neither, so under the shipped chart the two values differ and `_merged_options` produces two
     distinct keys. `deployment-service.yaml:51-56` gives the front door a kubelet
     `readinessProbe` on `/readyz` every 10 s, and `ops.py:172` registers `/readyz` with
     `app.get(...)` and no auth dependency — so the second pool is opened by the kubelet on every
     front-door pod, and can additionally be driven by any in-cluster caller.
     `CHEMCLAW_SESSION_STORE: "postgres"` (values.yaml:341) makes `runner._turn_checkpointer` →
     `checkpointer()` → `_checkpoint_pool()` fire on the first turn, which is the third pool.
     Nothing upstream prevents any of this.

  3. **Is the consequence what is claimed?** I ran the chart's own numbers — `max_size=8`,
     `service_max_concurrent_turns=8` — with 8 concurrent "turns" each holding a session-store
     connection and a checkpointer connection at the same time (`/tmp/peak.py`):

     ```
     core pool_stats (what the gauges report): {'pool_size': 11, 'pool_available': 11, ...}
     checkpointer pool_size (reported nowhere): 8
     REAL connections held by this one process: 19
     what chart budgets per process: 8
     ```

     One front-door process, at its *own* admission cap, holds **19** backend connections against
     a per-process budget of **8**, and its metrics surface says 11 with a max-size gauge of 8.

  4. **Does the guard see it?** `core/config/__init__.py:218` computes
     `pg_fleet_pooled_processes × pg_pool_max_size` = 17 × 8 = 136 ≤ `postgres.maxConnections: 136`
     → startup passes. `prometheusrule.yaml` fires only when
     `sum(chemclaw_pg_pool_max_size) > max(chemclaw_pg_fleet_max_connections)`, i.e. 17 × 8 = 136,
     never > 136 → the alert cannot fire either. Both guards are computed from the setting, not
     from the pools that exist.

- **Why**

  Every link is reachable from ordinary deployment behaviour, not from a private call: a kubelet
  probe, a session-store write, and a turn. `_POOLS` is keyed on `(dsn, options)` and the options
  string carries the statement timeout, so `/readyz`'s deliberately-shorter 2 s bound buys a second
  pool as a side effect; the checkpointer's pool is constructed outside `_pool_for` entirely, so it
  is invisible to `pool_stats()` and to `bind_pool_metrics` by construction. The two comments the
  finding quotes (`core/db.py:216`, `:222`) are false as written — the checkpointer pool is
  acquired with no witness, and the gauge is one third of what a front-door pod may open. The
  `store.py:81` prose that states the total as `max_size × distinct DSNs × processes` names a
  factor the check two files away does not implement.

  Two corrections, neither of which weakens the finding:

  - The **6 × 3 × 8 = 144** figure is a ceiling, not a load-driven number. Reaching 8 connections
    in the `/readyz` pool needs 8 *concurrent* probe misses; the kubelet alone (10 s period, 5 s
    `service_readiness_cache_seconds`, serial) keeps that pool near `pg_pool_min_size` = 2–3.
    Measured under the chart's admission cap the realistic per-pod figure is 19, so the six
    front-door pods carry ~114 and the fleet ~202 against a declared 136 — still 66 over, so the
    conclusion is unaffected. And because `/readyz` is unauthenticated, the 144 ceiling *is*
    reachable by an in-cluster caller, which makes the ceiling claim defensible too.
  - What makes this worse than reported: the second core pool also raises the process's *idle*
    floor, since each `_POOLS` entry keeps `pg_pool_min_size` connections warm forever. The
    over-subscription is not only a load peak; every front-door pod holds a second warm set of
    connections for a probe that runs one query every ten seconds.

  Consequence check: the outcome is not a paraphrase. `postgres.maxConnections: 136` is stated in
  `values.yaml` as "a provisioning requirement, not a preference", so an operator who provisions
  exactly that will be refused connections at full scale. On refusal `connection()` raises
  `ConnectionError` — which `/readyz` catches and reports unready (readiness flapping across pods)
  and which a turn surfaces as a failed turn. No safety or impurity-limit answer is involved, so
  the "what would a chemist be shown" test does not bite here; the harm is availability. High is
  the right label: the failure mode is fleet-wide, load-correlated, and both mechanisms built to
  catch it in advance are arithmetically incapable of doing so.
