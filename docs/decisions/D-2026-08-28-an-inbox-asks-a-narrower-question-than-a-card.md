# D-2026-08-28-an-inbox-asks-a-narrower-question-than-a-card — `GET /plans/pending` lists plans nobody has decided, not plans nobody has approved

## Status

Accepted. Extends D-137/D-167 (the plan gate and its one-turn approval) with the cross-session read
they never had. Does not change what the gate enforces, what a decision is bound to, or when an
approval is spent.

## Context

The plan gate is answered per session. `GET /sessions/{id}/plan` reads the plan and the decision
standing against it, `POST /sessions/{id}/plan/decision` records one, and
`runner._pending_plan_approval` emits the `approval_request` card at the end of a plan-gated turn so
a surface can offer the decision inside the conversation that raised it.

**Every one of those is addressed by a session id**, and that is the gap. The card lives in a live
turn; the companion UI recovers it after a reload only for a conversation somebody opens; and a
chemist who closed the tab holds no session id at all — ids are minted server-side and returned once
into the response that created them. So a plan could sit blocking every state-changing step of a
conversation with nothing anywhere able to answer *which conversation*. `Chemclaw3_ui`'s
`ReviewQueue.tsx` carried the empty slot and the sentence saying so: the gate that does block work
"currently lives only as an inline card in a live turn — so it survives no reload".

The obvious route — "list the sessions whose plan holds no live approval" — is the card's predicate,
and it is wrong here. An approval authorizes one turn and is consumed when that turn ends (D-167), so
"no live approval" is the **resting state of finished work**: every plan-gated conversation the
chemist ever completed would sit in the inbox permanently. That is the mirror of the failure the
companion UI already shipped once and recorded in its `ISSUES.md` — a deleted `GET /approvals`
whose 404 was swallowed into `[]`, rendering a confident, permanently *empty* inbox. A permanently
full one is read exactly as long before it stops being read at all.

## Decision

**`GET /plans/pending` lists a plan with no decision recorded against it — not one with no live
approval.** A spent approval and a rejection are both answers; asking again is the conversation's
job, and the card already does it there at the end of the turn that needs it.

Three further properties, each because the alternative is a claim the route cannot back:

- **The scan is pruned by `gate_applies`, on the profile the ownership row already carries.** With
  the harness off there is no todo list to read at all, and under `harness_autonomy="execute"` there
  is a plan but no gate — the agent acts without asking, so nothing about that plan is anyone's
  decision. `session_owners.profile` was already a column; `list_for_owner` now returns it, so a
  session that cannot be holding a decision costs no read. The prune is the same predicate
  `api/runner` uses to decide whether to show the card, so the inbox and the card cover the same
  sessions.
- **The remaining scan is bounded by `service_max_plan_scans` (25).** Each plan read is a statement
  on the checkpointer, and `AsyncPostgresSaver` serializes every statement behind one `asyncio.Lock`
  — so an inbox that read a full `service_max_listed_sessions` listing would hold the checkpointer
  against every concurrent turn on the pod for the length of the scan.
- **The response carries what the scan covered**, so an empty list is never ambiguous:
  `considered`, `gated`, and `unread`. `gated == 0` means the deployment has no plan gate and
  nothing can ever appear; `unread > 0` means the answer is partial — including a session whose
  checkpoint could not be read, which is an *unknown* plan rather than an absent one
  (`plan_state.session_todos` returns `None` for exactly that, and the route does not fold it into
  `[]`).

`get_plan` and the inbox now share one read (`routes/plan._read_plan`), so the two surfaces cannot
disagree about what a session is proposing or whether it was decided.

## Alternatives considered

**Record pendingness durably** — a table `_pending_plan_approval` writes when it emits the card,
which the inbox then queries in one statement. Rejected: it is a second piece of state saying what
the checkpoint and `plan_approvals` already determine between them, on a different lifetime. That
is the DARK-1 shape exactly — a session mode beside the approval, two answers to "may this session
act" that could and did disagree — and `routes/plan.get_plan`'s own docstring closes it from the
other side with "what a surface renders is one fact seen twice".

**Derive "blocked" from the audit trail** — list the sessions whose most recent plan-gate refusal
is unanswered. This is the *most* faithful signal of work actually stopped, and it is what a future
version should use if the queue turns out to be noisy under `plan_only`. Not now: `audit_events` has
no reader, the app role is granted INSERT and neither UPDATE nor DELETE, and building a query path
onto the trail to answer a product question is a larger decision than this route.

**No bound on the scan** — rejected on the checkpointer lock above. The cost is not the query count,
it is that the queries are serialized against turns.

## Consequences

- One case is missed, and it is stated in the route's docstring rather than left to be found: a plan
  **re-proposed byte-identically** after its approval was spent hashes to a row that exists, so it
  does not list — while the card, at the next turn's end, still shows. The alternative misses
  nothing and drowns the queue in decided work.
- `SessionOwners.list_for_owner` returns a five-tuple. `GET /sessions` drops the new field:
  `SessionSummary` describes a conversation to a person, and the profile is not that.
- A deployment with `harness_enabled=False` — the default — gets an empty list for one query and no
  checkpointer read, and the response says why.
- `tests/test_plan_inbox.py` pins the filter (approved, spent, rejected and undecided, separately),
  the ownership scoping, the prune *as a read count*, the bound, the unreadable-plan case and the
  registry-less deployment.
