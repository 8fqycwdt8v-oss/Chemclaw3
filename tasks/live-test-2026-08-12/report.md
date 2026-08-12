# Live test report — Phase 5 (2026-08-12)

**Model: `claude-haiku-4-5-20251001` only. Total spend: $3.88 ≈ €3.57** against a €10 target and a
€16 hard stop. No Sonnet or Opus call was made; no escalation was taken.

## The stack this ran against

Real, not mocked. Docker daemon started in-session, then `make live-infra` brought up
`infra-postgres-1` (healthy), `infra-temporal-1` and `infra-temporal-ui-1`; 45 migrations applied;
`make live-up` started 6 connector servers, 4 Temporal workers and the front door on `:8000`.

**Both Sonnet-defaulting settings were pinned before any spend** — `agent_model` *and*
`live_probe_judge_model` (`core/config/evals.py:77`). The judge is the trap: running the probe suite
without pinning it would have made Sonnet calls silently while the agent itself was on haiku.

## Cost control, and why the ledger alone could not prove the constraint

Spend was read from `turn_costs` and priced at haiku rates ($1/MTok in, $5/MTok out).

| | |
|---|---|
| turns billed | 22 (of 37 rows — see below) |
| input tokens | 3,783,663 |
| output tokens | 18,967 |
| **cache reads** | **0** |
| cost | $3.88 ≈ €3.57 |
| average | ≈ €0.20 per turn |

The open backlog row *"`turn_costs` cannot say which model a turn spent its tokens on"* is real and
bit here: the table cannot prove no Sonnet call happened. The control used instead was pinning the
model at the provider seam and verifying it resolved (`agent_model = claude-haiku-4-5-20251001`)
before the first turn.

---

## Results

### 1. Full suite against real Postgres + Temporal — **4223 passed, 13 skipped, 0 failed** (13m22s)

The offline baseline was 4079 passed / 157 skipped. Standing the infrastructure up ran **144
additional tests, all green**, covering retention, the checkpointer, the session store and the
connector job workflows. The only remaining skips are the four native-binary ones (`xtb`, `crest`,
`tblite`, `make`).

### 2. `make live-jobs` — **6/6 PASS**, €0

A real durable job end to end on real Temporal + Postgres + connector workers:

| check | result |
|---|---|
| workflow reached COMPLETED | PASS |
| calculation cached in Postgres | PASS — 3 `xtb*` rows |
| job recorded in Postgres | PASS |
| duplicate launch rejoins the same run | PASS — id matches; cache rows 6 → 6 |
| wedged worker yields a pending job | PASS — returned the id after 20s, then COMPLETED once resumed |
| audit chain verifies | PASS |

The fourth row is the static Phase 2 vindication (`job_workflow_id` +
`ALLOW_DUPLICATE_FAILED_ONLY`) **confirmed live**.

### 3. M12 probe (a) — plan gate — **4/5 PASS**, one inconclusive

First run scored 0/5 and that was **my setup error, not a defect**: the stack was started without
`harness_enabled`, which defaults to `False`, so `gate_applies` was false and there was no plan to
decide on. Recorded because the failure mode is instructive — the probe cannot distinguish "the gate
is broken" from "the gate is not attached".

With `CHEMCLAW_HARNESS_ENABLED=true` and `plan_only`:

| check | result | observed |
|---|---|---|
| a plan a human can decide on | PASS | 3 plan items, hash `f6f1180bc0b2` |
| an unapproved state-changing call is refused | PASS | refused `compute_reaction_energy` |
| the decision was accepted | PASS | `POST /plan/decision` → 204 |
| the approved plan executes | PASS | ran `compute_reaction_energy` ×3, `propose_knowledge_note` |
| a changed plan is re-gated (DARK-1) | **INCONCLUSIVE** | plan hash UNCHANGED, `approved=False`, ran — |

**The fifth is not a failure.** Read the observed column: the plan hash did not change, so haiku
never rewrote the plan and the DARK-1 scenario was never staged. Nothing ran under the earlier
decision, so the gate held. The probe could not set up its own test on this model.

This is the one place the brief's escalation rule applies — interpreting it needs a run where the
model actually rewrites the plan. **Flagged, not escalated.**

### 4. M12 probe (b) — degradation ordering — **3/3 PASS**

Run with `infra-temporal-1` deliberately stopped:

| check | result | observed |
|---|---|---|
| the outage was announced | PASS | `capability_degraded` named `durable-jobs (Temporal)` |
| announced before the first token | PASS | degraded at event 1, first token at event 2 |
| the durable launcher was reached | PASS | called `compute_reaction_energy` |

REV-6's ordering claim holds live.

### 5. M12 probe (c) — routing — **ran after the fix; the measurement does not favour teams**

Re-run against the fixed build. The probe executes now, and it answers the question
`agent_teams_enabled` has been waiting on since M9:

| arm | probes | delegated | correct | accuracy | tokens |
|---|---:|---:|---:|---:|---:|
| single | 15 | 0 | 0 | — | 1,759,062 |
| team | 15 | **1** | 1 | 100% (n=1) | **2,309,667** |

Per specialist: `computation`, 1 turn, 260,500 tokens.

**Read it carefully, because 100% is the least informative number in the table.** The denominator is
one. What the run actually establishes is the other two columns: on haiku the supervisor delegated
**1 of 15** probes, and the team arm cost **31% more tokens** than the single agent for that one
delegation — the whole surface is paid for on every turn whether or not it routes.

That is evidence for keeping the flag off, and it is the *kind* of evidence D-2026-08-10 said would
decide it ("a supervisor that mis-routes is worse than the agent it replaces, and no unit test can
establish which of those a deployment gets"). It is not yet evidence about routing *quality*, which
needs a model that delegates often enough to score.

**And it was blocked by a defect the probe itself surfaced.** The first run reported 0/15 delegated
and suggested the flag was unset; the flag was set. `.live/api.log` showed every turn failing at
graph construction:

```
TeamError: specialist 'evidence' would reach connector tool(s)
['find_calculations', 'resolve_compound', 'similar_molecules',
 'similar_reactions', 'substructure_matches'] that its supervisor cannot
```

**15 of 15 turns failed before the model was called** — hence €0 and `completed=false` rows.
`build_langgraph_agent` composes the surface as `[*tools, *connectors, skill_read_tool]` but passed
the team middleware only `tools`, so every connector tool a specialist kept read as a widening. The
guard (added by D-2026-08-12 to close a real hole) was right; the half of the surface it was handed
was not. Fixed in `2a59a02`.

**Why no test caught it:** all 22 tests in `tests/test_agent_team.py` build the supervisor with **no
connectors**, and `_narrowed_connectors` returns `[]` before reaching the assertion.

## The finding only a live run could produce

**No prompt caching is happening at all: `cache_read_tokens = 0` and `cache_write_tokens = 0` on
every turn**, across all 22 billed turns — with an input:output ratio of **199:1** (3,783,663 in
vs 18,967 out) and single turns reaching 189k–260k input tokens.

The ledger *reads* cache columns (`api/runner_usage.py:85`, and `turn_costs` has both), so the
observability exists — but nothing ever requests caching: `cache_control` / `ephemeral` have zero
hits across the agent layer. Every model call re-sends the full system prompt and all 28 tool
schemas at full price.

Input dominates cost by two orders of magnitude and the prefix is large and static, which is exactly
the shape prompt caching is for (cache reads bill at a tenth of input). This is the single largest
cost lever available and it is untouched. Backlog row added.

---

## What remains owed

- **DARK-1 re-gate**, inconclusive on haiku — needs a run where the model rewrites its plan.
- **Routing accuracy**, unmeasured — the probe can run now that the build defect is fixed, but it
  was not re-run in this session.
- **T-10's failure mode** (a start-to-close timeout on the non-heartbeating `run_agent_step`) still
  needs a deliberate broker-level test; it was not exercised here.
- `make live-storm` (chaos/stress on the mock model, €0) was not run.
