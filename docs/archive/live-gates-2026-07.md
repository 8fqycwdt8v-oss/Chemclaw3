# The two authorization gates, run live (2026-07-31, D-164 / D-165)

*Closes DARK-1 and DARK-2 from `docs/planning/BACKLOG.md`. DARK-3 was already closed by D-153; see
the last section. The decisions are in `docs/decisions/D-164-…` and `D-165-…`; this is the record of
what was actually run.*

## The stack

Everything native — Docker is unavailable in this environment:

| Piece | How |
|---|---|
| Postgres 16 + pgvector | `pg_ctlcluster 16 main`, migrations 018–022 applied |
| Temporal | dev server on `127.0.0.1:7233`, own SQLite db file |
| Entra | local RSA keypair, JWKS served over HTTP, Entra-shaped tokens minted per identity |
| Connectors | all six (`molfp`, `rxnfp`, `safety`, `chem`, `calc`, `bo`) via `connectors_dev` |
| Workers | `background-jobs` + `connector-calc` + `connector-bo` + `connector-qm` |
| Front door | uvicorn on `127.0.0.1:8000` |
| Model | `claude-haiku-4-5`, real Anthropic traffic |

Flags: `harness_enabled=true`, `harness_autonomy=plan_only`, `entra_required=true`,
`session_store=postgres`, `connectors_required=true`. The credential lived only in a mode-600 file
outside the repository and was loaded as an environment variable; a secret scan ran before every
commit.

## What the live run changed about the fix

The offline suite went green on both fixes before any of this ran. The live stack then found **five**
further defects — one in the D-164 fix itself, four in the path D-165 governs. This is the record
that matters, because each was invisible to a suite that had just passed.

### D-164: the approval outlived the request

Reproduced the DARK-1 sequence: plan → approve → execute → *a completely different question*. The
plan hash did not move, because **the model never rewrote its todo list**. Binding the approval to
the work items — the fix for the hash that moved too much — produced a hash that moved too little,
and `compute_xtb_energy` ran under an authorization given for a hazard-screening plan.

A plan-shaped identity cannot see this: the plan genuinely has not changed. What changed is the
request. So an approval is now spent by the turn it authorizes. Fixing that then exposed a second
one: `GET /sessions/{id}/plan` still reported a spent approval as `approved` — the same
display-disagrees-with-enforcement defect the whole ADR is about, reintroduced one layer up by the
fix for it.

### D-165: the template path had never executed

Four consecutive failures, each on something different, running the *shipped* `hazard-briefing`
template against a real server:

1. `Activity function run_tool_step … is not registered on this worker` — only the job-step resolver
   carried `@durable_activity`.
2. `Unable to serialize unknown type: agent_framework._types.Content` — a tool step's result cannot
   cross the activity boundary, on either branch.
3. `ToolApprovalMiddleware requires an AgentSession` — an agent step cannot run under
   `harness_enabled`, which is what the Helm chart sets.
4. The fix for (3) is to run the step without the harness, not to invent a session — a template
   exists to remove exactly the discretion a planning loop would give the step back.

**No template with a `tool` or `agent` step had ever completed a run in a deployment.** Every one of
these is the class D-155 catalogued: written, tested, served by nothing.

## Results

**Plan gate — 10/10.**

| Check | Result |
|---|---|
| a fresh `plan_only` session is in plan mode | pass |
| the proposed plan is not approved | pass |
| approving the shown plan is accepted (204) | pass |
| approval moves the session to execute | pass |
| the approval is spent by the turn it authorized | pass |
| a follow-up request reports itself unapproved | pass |
| the session stays in plan mode for it | pass |
| no state-changing tool runs on the unapproved request | pass |
| a rejection after an approval revokes it | pass |
| a stale plan hash is a 409 | pass |

On an earlier run of the same probe the model *did* reach for a gated tool on the unapproved
request: `compute_xtb_energy` was refused, and the refusal is in `audit_events` with the reason. The
final run happens not to have attempted one — recorded as a skip rather than a pass, because a check
that the model never triggered proves nothing.

**Template — 6/6.** `hazard-briefing` completes all three steps; both connector tool steps
(`screen_hazards` on `safety`, `similar_molecules` on `molfp`) leave GxP rows under the requester's
oid with the run's workflow id as correlation id; an expensive job step (`compute_dft_energy`) is
refused for an unentitled requester **before** any child workflow starts, and the refusal is audited
as an error.

## Not exercised

- **Real HPC/DFT** — `hpc_launch_interface=mock`; the QM connector's authorization was exercised, its
  execution was not.
- **A real Entra tenant** — a local JWKS with the same token shape; the OIDC flow itself is untested
  here, as in every prior pass.
- **A multi-replica front door** — the plan gate reads session state, which is per-pod. Under
  `session_store=postgres` the *approval* is shared but the consumed marker is not, so two replicas
  could each spend the same approval once. Recorded as a limit, not fixed: it belongs with the
  wider per-pod-state question (`active_turns`, `event_streams`, the attachment store) rather than
  being solved once for this control.

## DARK-3

Closed by D-153 before this pass began, and re-verified rather than re-fixed. `await_job_results`
does not touch `session_events` at all — each job is awaited on its own Temporal handle, so there is
no destructive mailbox claim left to steal another job's completion.
`tests/test_mid_turn_resume.py::test_the_wait_leaves_other_jobs_push_back_alone` already pins the
property and is counterfactually sound. **No code was changed for DARK-3, and none was needed.**
