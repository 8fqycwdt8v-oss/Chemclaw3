# Wave 11 — what a worker holds between tasks

## Items

- [x] **Bound the workflow cache, from a measurement.** `durable/background_worker.py` set
      `max_concurrent_activities` and stopped there, and a **child workflow is not an activity** —
      so the one ceiling this repository chose never reached the bundle children core starts. Two
      of the facts needed were wrong in the obvious reading: the SDK default is **100**, not the
      500 its own constructor docstring names (that is the resource-based tuner), and the
      workflow-task ceiling is not what holds memory. `max_cached_workflows` is.
      `worker_max_cached_workflows` ships at 750, held as an inequality against the chart's
      memory request rather than as a literal, with a second test reading the `Worker(` call so a
      declared-but-unarmed setting reds. ADR + ledger + topic row; the old row replaced by what is
      left of it.
- [x] Fresh-context subagent review; five blockers, three of them factual and all three reproduced
      independently before acting: the `1.35` multiplier's arithmetic, the falsified "replay
      history" mechanism, the 500 (it is `workflow_task_executor`'s thread pool and it *does*
      apply, because the task ceiling is deliberately unset), `connectors/worker.py` never getting
      the ceiling at all, and an `ast` assertion that checked a keyword's name rather than its
      value. All five fixed; the ceiling moved 1,000 → 750 as a consequence of the third term.
- [ ] Full serial `make cov`, PR, merge on green CI.

## Measurements this wave rests on

Against the broker `make up` runs — the downloaded dev server is not fetchable here, which
`tests/temporal_env.py` already records. N workflows parked in `wait_condition`, RSS from
`/proc/self/status` against a 65.8 MiB idle baseline:

| cached | RSS | over idle | each |
|---|---|---|---|
| 50 | 72.5 MiB | +6.7 | 137 KiB |
| 100 | 76.9 | +11.2 | 114 |
| 250 | 85.7 | +20.0 | 82 |
| 500 | 101.5 | +35.7 | 73 |
| 1,000 | 135.1 | +69.4 | **71** |

And at 200 cached, scaling the workflow's own state: 16 KiB → 91 KiB each, 64 → 142, 256 → 347.

**The two-term model those numbers were first fitted to was wrong twice**, and the fresh-context
review caught both. `~75 KiB plus 1.35x state` took `1.35` as `347 / 256` — the *total* per-workflow
cost over the state — so the fixed overhead sat inside the quotient and was then added again.
Against this table's own figures the state coefficient is `(91−75)/16 = 1.00`, `(142−75)/64 = 1.05`,
`(347−75)/256 = 1.06`: **~1.05**, not 1.35. The bad model overstates a 256 KiB workflow by ~18%
(411 MiB per 1,000 against 339 measured) — safe-direction, and still a number nothing produced.

And "the excess being the replay history beside it" is falsified by this table: the excess over
state is *flat* in state, so it cannot be the state term's residual. History is its own axis, and
measuring it on its own at zero state and 200 cached:

| signals per workflow | each | signals delivered |
|---|---|---|
| 0 | 69.1 KiB | 0 |
| 20 | 198.9 | 4,000 |
| 100 | 245.9 | 20,000 |

Slopes 6.5 and 1.77 KiB/signal here against the reviewer's 0.95, so **the axis is established and
its coefficient is not** — which is why the third constant is named an allowance.

Three terms, then: `85 + 1.05×state + history`. At the asserted shape (256 KiB of state, a hundred
signals) that is ~529 KiB, so the SDK's 1,000 comes to ~516 MiB — over half the worker's 1Gi
request. **The ceiling ships at 750**, ~387 MiB. Lowering it beats raising a request every worker
Deployment shares, and an evicted workflow replays rather than fails.

## The measurement that was wrong first

The state-scaling arm initially read 0 KiB per workflow at every size — "state is free". The
fixture was `["y" * 1024 for _ in range(n)]`, which CPython constant-folds into *n* references to
**one** string, so 200 workflows "holding 256 KiB" held 1 KiB between them. A live-instance count
found it: 200 instances, 51,200 entries, 15 MiB of RSS — three numbers that cannot all be true.
The figure only became a measurement once the fixture allocated what it claimed.

That is the third time this session a measurement's *instrument* was the defect rather than its
logic: a tokenizer that matched quotes and missed YAML scalars, a sweep that never reached the leg
it was about, and now a fixture that allocated one object and reported 51,200.

## Review

Pending the gate and the subagent review.

---

# Wave 12 — the backstop covers the gates, and a knob that starves is bounded where it starves

Two rows, both actionable (several neighbours in §4 are argued non-fixes with live triggers —
the SSH alias, the IPv4-mapped arm, `/readyz`'s re-wait, the worker probe port, the connector
sweep — and stay as they are).

## Row A — three first-party refusal gates are still outside the weekly mutation backstop [S]

`agent/plan_gate.py` (671 lines), `agent/skill_backend.py` (367) and `agent/loop_cap.py` (333)
join `agent/spend_cap.py` (309) in `[tool.mutmut].source_paths`. The row's own premise is already
answered: the run is 87 minutes, not hours, and `spend_cap.py`'s share was ~18 s.

- [x] Measured, selection-alone against selection-plus-file: `plan_gate.py` **66% -> 87%**,
      `skill_backend.py` **51% -> 90%**, `loop_cap.py` **88% -> 100%**. Two of the three would have
      reported a large block of mutants as survivors purely from being unpaired.
- [x] Three source paths and their three test files added in one edit; the population pin reds
      until the floor is re-measured, which is what it is for.
- [x] Re-measured. **4,057 mutants, 2,555 killed — 63.0%** — 986 survived, 473 no-test (11.7%),
      43 timed out, 0 suspicious or segfault, 82m40s at 0.76/s. Both rates improved (62.1% ->
      63.0%, 12.3% -> 11.7%). **The floor stays at 57.0**: the rule stated in the workflow is five
      points under the observation, which gives 58.0, and keeping 57.0 is the more conservative of
      the two rather than a one-point ratchet on a nine-tenths-of-a-point move. The number not
      moving is a result, not an omission — the pin made re-deriving it compulsory.
- [x] **The row's own question answered exactly.** +417 mutants and the wall clock went *down*,
      87 -> 82m40s. The addition is smaller than the run-to-run variance, so at this size it is
      not the term that decides the run's length — the opposite of "the run is hours long".

## Row B — `retrieval_source_weights` has no upper bound [S]

The row names its own fix and the reason a ceiling is the wrong shape: measured, the damage is a
function of `weight x legs x cut` rather than of the weight, and the validator's docstring already
argues "a weight has no upper bound to clamp toward". So the bound belongs on the surviving *mix*
— a per-source floor in `retrieval/hybrid.py::reciprocal_rank_fusion` — not on the number a
deployment writes down.

- [x] Reproduced exactly: `graph 8 / 0 / 0 / 0 / 0` at a cut of 8, `graph 22` against 2 each at 30.
- [x] Floor designed, and the mix measured rather than asserted. Nine cases swept (three
      weightings x three cuts) and **exactly one moves** — the starved one. A floor of
      `limit // (2 x legs)` was measured too and rejected: it acts on cuts that were never starved.
- [x] `D-2026-09-23-the-bound-belongs-on-the-mix-not-on-the-weight` amends
      `D-2026-08-01-a-cap-that-starves-a-source` by supplying the half it did not reach — the
      ceiling the row asked for is **declined**, because measured the damage is `weight x legs x
      cut` rather than a property of the weight.

## Review

**Both rows closed and deleted from `BACKLOG.md`**; one new row opened by the review, so 42 -> 41.

**The fresh-context review found ten things and every factual one was reproduced before acting.**
Three mattered:

- **The test named for the central design choice did not discriminate it.** A floor reading
  `chunk.retriever` instead of the legs' offered lists passed all 131 retrieval tests. The fixture
  was wrong, not the argument: on it, the naive reading reserves *nothing* for the two legs it
  cannot see, so it is also the identity there. Rewritten around a fixture where the naive floor
  **evicts** `n3` and `n1` from a three-slot window and substitutes the two worst-ranked notes in
  the fusion — a worse answer than no floor at all. The `retriever`-reading mutant is now caught by
  exactly the test named for it, and two other wrong implementations are caught by three others.
- **The ADR dropped the reachability qualifier the row had measured.** `retrieval_mode` ships
  `graph`, so this code is never called by default; and in `hybrid`, every leg is cut to
  `retrieval_top_k` (8) before fusion, so five legs offer at most 40 into a cut of 40 and the count
  cap cannot bind. The guard ships **inert**, arming only when a deployment moves off those
  numbers. That is a fine thing to ship and a bad thing to leave unsaid — the ADR read as if a live
  starvation had been fixed.
- **A justification invented to reinforce a true argument contradicted the code.** Four documents
  said the offered-list read is what makes the floor work under `corpora`, "where `_fuse_by_corpus`
  relabels `retriever` to the corpus name". It relabels on a `model_copy` and returns the
  originals — its own comment says that is the point. The real argument never needed the second one.

And `loop_cap.py`'s lift is **88% -> 100%**, not the 97% first written: 97% is what
`tests/test_loop_cap_floor.py` scores *alone*, missing lines `tests/test_runner.py` already covers.
Two measurements of different things reported as one before-and-after — which is the fourth time
this session the instrument, not the logic, was the defect.

**What the wave turned on, twice, was refusing to read a count off the wrong field.** Row B's floor
would have been wrong if it counted a leg's survivors by `chunk.retriever` — the fusion keeps the
first finder, so a note three legs found credits one and pins two at zero, which is the error
`fanout.record_kept_chunks` already had measured and recorded. The tests count by what each leg
*offered* for the same reason, and one of them constructs the case where the naive reading sees two
legs starved that are not.

**And a test that asserts inertness is satisfied by a change that does nothing.** Four of the five
new tests red when `with_no_leg_cut_out` is replaced by the identity; the fifth did not, because its
whole content is "the floor does not move this". It was rewritten to assert the naive reading *would*
have moved it, so it now discriminates the design choice it is named after rather than restating it.

**Row A's premise was already dead and the run confirmed the second half too.** "Measure the runtime
before adding a module" assumed the addition was the expensive term. It is not: +417 mutants, and the
wall clock fell 87 -> 82m40s. The expensive thing was never the modules — it was the stretch of
not pairing the test selection with them, which is what took the rate from 44.3% to 62.1% with no
code change at all.

---

# Wave 13 — two declared ceilings that no measurement backs

Picked from 41 open rows after classifying every one. Most of this file is **not** a work queue:
eight rows are argued non-fixes carrying a live trigger (the SSH alias, the IPv6 guard arm,
`/readyz`, the worker probe port, the connector sweep, the event-loop defang, the `/scratch/` cap,
`_CALIBRATED`), and eighteen are blocked on something this environment does not have — a gateway
with credit, an IPv6 host, a companion repo, an embeddings endpoint, an upstream fix.

The two taken are the same defect in two resources: **a ceiling this repository declares, split or
sized by something that was never measured**, each closing by landing a derived number in a test
that already exists rather than by changing behaviour.

## Row A — neither net sees one Postgres server that two DSNs spell differently [M]

`core/config.pg_endpoint` is a string comparison, so `localhost` against `127.0.0.1` is charged and
alerted as *two* servers, each inside its own ceiling, and the real total is checked by nothing.
The released expression before the split gauge existed would have caught it, so this is a **runtime
regression** for that configuration.

Reproduced first, against the live server: both spellings answer `system_identifier`
**7687905078163619878**. And the row's load-bearing claim — that an unprivileged role can read it —
holds: a freshly created `NOSUPERUSER NOCREATEDB NOCREATEROLE` role read it fine. 1.5 ms here
rather than the row's 0.24, same order.

- [ ] The row frames the decision as a trade: the fix belongs on the gauge, where a pool has
      already connected, and "costs the alert its series during a database outage". **Check whether
      that trade is actually forced.** A `system_identifier` is assigned at initdb and never
      changes, so it can be *learned once and cached* — string comparison until a pool has
      connected, measured identity after, and the cached value survives an outage. If that holds it
      is strictly better than both options the row names, which is the "is there a more elegant
      way" CLAUDE.md asks for.
- [ ] Whatever ships, `pg_endpoint`'s own docstring is where the refusal is recorded and must stay
      true — it is long, exact, and currently argues the measurement cannot live *there*, which
      stays right.
- [ ] Anchors: `core/config/__init__.py::pg_endpoint`, `core/db.py::_session_store_max_connections`.

## Row B — nothing bounds what a turn costs the front door's memory [M]

`resources.service` was sized against a resident set with **no turn in flight** (431.9 MiB), and
`CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` is 12 — so the one part of the pod that scales with load is
the part no number covers. The CPU half is measured (0.581 s per turn, which is why `requests.cpu`
is 1); the memory half never has been.

- [ ] Drive a turn against the mock LLM and express the peak as MiB per admitted permit, so
      `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`
      can take a third term.
- [ ] **Sample the cgroup's peak, not `Pss`** — the row corrected itself on this and says why:
      `Pss` is a system-wide proportional share that understates a cgroup's charge. Watch for
      cgroup v2, which names it `memory.peak` rather than `memory.max_usage_in_bytes`.

### Row B — what measuring it found (2026-09-24, in progress)

Method: the real front door (`uvicorn chemclaw.api.app:create_app --factory`, the live lane's exact
argv and environment) started inside its own cgroup-v1 memory cgroup; every other lane process
outside it. Anon = `memory.stat total_rss` sampled at 10 ms (v1's `max_usage_in_bytes` folds in page
cache, which here is 229 MiB of mostly inactive file). Turns driven with `live_storm.storm` against
the mock LLM.

- Idle, no turn: 314.6 anon, reproduced four times to 0.2 MiB (Pss 432 is anon + file share).
- **Warm-up, once**: the first ~50 turns at c=1 add +65 to +67 MiB anon, flat thereafter (3 runs).
- **Concurrency high-water**: stepping c=1 -> 4 -> 8 -> 12 adds ~5-6 MiB per permit, retained
  (allocator high-water), +56 MiB at 12. c=16 and c=24 add ~0 and refuse the excess — the
  admission cap bounds it. `c-parallel` and `f-call-flood` add nothing once warm.
- **Thread length is the real term.** A turn loads the whole checkpointed thread (no window;
  compaction trims only the request). 12 sessions x 100 turns of 95,000-char messages (one em dash
  each) drove the front door alone to **1,268 MiB anon**, linear in stored thread length, past the
  1 GiB limit at ~turn 75 — ~7.6 bytes of pod per stored char per permit.
- The only bound on a thread is `budget_max_turns_per_session` (100), counted **in-process** in an
  LRU — a restart or another replica resets it.
- The existing inequality already fails with the short-turn terms alone: 432 + 91 + 70 (warm-up) +
  ~60 (12-permit high-water) + 448 (two parses at 160 MiB x 1.4) = ~1,100 against 1,024; and warm
  idle (~653) sits over the 640Mi request.

Also found and fixed on the way (`f8e52cc7`): the background worker could not boot at all —
`durable/memory_jobs.py` imported `regex` outside the sandbox pass-through (since #434).

## Handoff — state at the end of this session

**Branch `claude/backlog-implementation-waves-ax8llz`, two commits ahead of `origin/main`, tree
clean, nothing unpushed.** Waves 8-12 are merged (PRs #437-#441). Wave 13 is half done.

### Done — Row A, committed as `6ae9bf6e`

`core/db.same_server` reads `system_identifier` off a borrow and caches it per endpoint; a split
the measurement disproves collapses to 0. ADR
`D-2026-09-23-the-server-says-which-server-it-is`, both ledger rows written, `BACKLOG.md` row
deleted (41 -> 40 open). Four tests in `tests/test_fleet_pools.py`, each driven red against its own
regression. `make lint type` green; `tests/test_fleet_pools.py` and `tests/test_decision_log.py`
green. **The full serial `make cov` has NOT been run since Row A landed.**

### Left to do, in order

1. **Start the infrastructure first.** The container restarts lose it, and it is down right now:
   `sudo -n dockerd &` then `make up` and `make db-migrate`. Without it ~216 Postgres-backed tests
   skip and still print green — and Row A's own tests are among the ones that need a live server.
2. **Row B — nothing bounds what a turn costs the front door's memory** (`BACKLOG.md`, the row
   anchored on `deploy/helm/chemclaw/values.yaml` `resources.service`). Not started. Drive a turn
   against the mock LLM (`chemclaw.cli.mock_llm` on loopback, no credential needed), express the
   peak as MiB per admitted permit, and land it as a third term in
   `tests/test_deploy_chart.py::test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares`.
   **Sample the cgroup's peak, not `Pss`** — the row corrected itself on this and says why. Watch
   for cgroup v2, which names it `memory.peak`, not `memory.max_usage_in_bytes`.
   `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` is 12 and the idle floor is 431.9 MiB.
3. **Full serial `make cov`** (~34 min with infra up; it is the gate CI runs, and `make test
   PYTEST_WORKERS=4` is a local loop only — a failure under it is re-run serially before believed).
4. **Fresh-context subagent review** before the PR. Every wave this session found real defects
   this way, including three in Wave 12 and one in Row A's own first attempt.
5. **PR, merge on green CI, delete the branch, restart it from the new `origin/main`, start Wave
   14.** Squash-merge is the convention on `main`. CI's `check` job runs `make cov`; expect the
   push-triggered suite's `check` to show `cancelled` when opening the PR starts a fresh suite —
   that is concurrency supersession, not a failure, so confirm the *newest* suite is green.

### If Row B turns out to be blocked or not worth it

Say so and close the wave with Row A alone rather than padding it. The next best actionable rows,
from a triage of all 41: **the probe-coverage tail** (`tests/test_probe_coverage.py`, [S], no
infrastructure, the row names both the shape and the anti-shape — deliberately *not* a ratchet on
the count) and **an arrival signal for undated notes** (`durable/digest._is_new`, ~1-2 days).

Do not re-open these: the SSH host alias, the IPv6 guard arm, `/readyz`, the worker probe port, the
connector readiness sweep, the event-loop defang, the `/scratch/` cap, `_CALIBRATED`. All eight are
argued non-fixes carrying a live trigger, and `_CALIBRATED` has zero current defect by its own
measurement. Eighteen more rows are blocked on a gateway with credit, an IPv6 host, a companion
repo, or an upstream fix.

## Review

Pending Row B.
