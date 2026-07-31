# D-165 — An approval authorizes a request, not a session

**Status:** accepted · **Supersedes part of:** D-137 · **Closes:** DARK-1

## Context

D-137 built the pre-execution gate the documents describe: `mode_set` retracted from the model's
tool surface, an owner-scoped HTTP route as the only path into execute mode, and a `plan_approvals`
row recording who decided what. It closed the hole where the *model* granted itself autonomy.

It did not make the approval mean anything afterwards. A live pass (D-155) found the consequence:

> Approve a four-item plan — `mode` flips to `execute`, a `plan_approvals` row is written. Then ask
> a *completely different* question in the same session. `GET /sessions/{id}/plan` reports a new
> `plan_hash` with `approved=false`, the session is still in `execute`, and the turn autonomously
> ran `compute_xtb_energy` and `propose_knowledge_note` — a knowledge-graph write — with no
> approval for that plan.

`PlanApprovalStore.decision` was read in exactly one place: the front door's **display** route. No
execution path consulted it. `grant_execute` had no mirror, so nothing ever returned a session to
`plan`, which also meant a rejection recorded after an approval revoked nothing — against migration
020's stated contract that the latest decision wins.

Two coupled decisions blocked the fix, which is why DARK-1 was recorded rather than patched.

## Decision

### 1. An approval binds to the plan's work items, not its rendering

`current_plan_hash` covered the rendered `[x]`/`[ ] title` lines. D-137 argued for that
deliberately — "a plan whose steps have been ticked off is not the plan that was approved, and
re-approval is the correct outcome" — and it is coherent in the abstract and fatal in practice: the
hash moves on the *first* ticked box, so an approval can never be checked against the plan being
executed. It could only ever be recorded, which is exactly what the system did. A four-item plan
would have needed four approvals, and nobody would have operated that.

So `current_plan_hash` now hashes `todo_plan_items`: the titles in order, without the checkbox, and
excluding the `awaiting-job:` rows the launcher writes (`_mark_awaiting_if_harness`) — counting
those would let an approved plan revoke itself the first time it started a job. `todo_titles` and
`PlanEvent` are untouched, so nothing a chemist reads changes.

**This reverses D-137 on the point it argued most explicitly.** Ticking a box is the plan
proceeding; adding, removing or rewording an item is a different plan.

### 2. And to the *request* it was given for

Binding to work items made the approval checkable and left a second hole, which only the live run
found: the model is free to answer a new question **without touching its todo list**. It did. The
plan identity never changed, the approval never lapsed, and `compute_xtb_energy` ran under an
authorization given for a hazard-screening plan. A plan-shaped identity cannot detect this, because
the plan genuinely has not changed. What changed is the request.

So an approval is **spent by the turn it authorizes**. `run_turn`'s teardown calls
`consume_turn_approval`; the harness loop runs a plan to completion inside one `agent.run`, which is
exactly the scope of "execute the approved plan". The next user message is a new request and needs
its own decision. Re-approving an unchanged plan re-arms it — a person saying "yes, again".

Consuming also ends execute mode, and `GET /sessions/{id}/plan` reports the **effective** approval
rather than the stored row. A surface that says `approved` for a plan whose every state-changing
call is refused is the same lie the whole finding was about.

### 3. The enforcement is a function middleware

`enforce_plan_approval`, attached only under `harness_enabled` + `plan_only`, inside `audit` (so a
refusal is a recorded `error` outcome) and inside `surface_authorization_denials` (so the model is
handed a sentence a chemist can act on). It raises `PlanNotApprovedError(AuthorizationError)` and
inherits both behaviours with no new plumbing.

The obvious alternative — check in `before_run` — looks sufficient and is not: on the repro turn the
todo list still holds the previous, approved plan when `before_run` runs, and the model rewrites it
afterwards. `PlanApprovalModeProvider` does demote a stale session there, because a displayed mode
should be true, but the enforcement is at the tool boundary because that is where an action is.

The loop predicate is wrapped too. Without it an unapproved session still iterates, has every write
refused, and spends the whole runaway budget achieving nothing.

### 4. Reads stay open; writes are classified where the knowledge lives

A gate over every tool would make `plan_only` a mode in which the agent can neither answer nor build
the plan it needs approved, so the deployments that want the GxP posture would turn it off. The line
is state change:

- **in-process** — `authz.STATE_CHANGING_TOOLS` / `READ_ONLY_TOOLS`, held to a partition of the tool
  registry by a test, so a new tool must be classified or the suite fails;
- **connector** — each bundle's own `endpoint.state_changing` / `read_only`, and the manifest
  **refuses to load** unless every served tool appears in exactly one. Every way of getting this
  wrong fails open, so none is tolerated;
- **jobs and templates** — structural, no declaration needed.

The connector half is what makes the gate cover the finding: `compute_xtb_energy` is a `calc`
*endpoint* tool, not a job, so a set built from in-process names plus declared jobs looks complete,
passes every test anyone would think to write, and misses half of what the unapproved turn ran.

### 5. The store follows the session store

The fail-open/fail-closed question DARK-1 posed dissolves rather than being answered. The approval's
only job is to authorize a mode that lives in the session's own state, and the requirement is that
neither outlives the other. Under `session_store="memory"` that mode dies with the process, so
`InMemoryPlanApprovalStore` matches it exactly; under `postgres` both are durable. Same switch and
same polarity as `default_audit_sink` and `history_provider`. The CLI gets `/plan` and `/approve` —
typed by a person, never callable by the model (D-005) — so `make chat --admin` keeps working
fail-closed with no new config field.

## Consequences

- The gate is off with the harness, which ships off. `harness_autonomy="execute"` is untouched: a
  deployment that configured autonomy has said it does not want an approval-first posture.
- A `plan_only` deployment now needs one approval per request. That is the GxP posture, stated
  plainly rather than implied by a control that did not run.
- **A residual limit, stated because it is real:** the system cannot tell "proceed" from "a new
  question" in the message that follows an approval. That one turn is authorized by construction.
  It is bounded to a single turn, fully audited, and immediately preceded by a human decision —
  but it is not zero.

## Verification

Counterfactual on every regression test: neutering the middleware fails three plan-gate tests
including the repro; reverting the hash to the rendered lines fails the two identity tests.

Live, on the full stack with signed identities and real traffic: 10/10 — a follow-up request reports
itself unapproved, the session stays in plan mode, and no state-changing tool runs. An earlier run
of the same probe caught `compute_xtb_energy` refused with the refusal in `audit_events`.
