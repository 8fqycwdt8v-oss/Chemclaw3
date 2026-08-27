# D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold — the D-032 asynchronous approval feature is deleted, the PR-gate it duplicated is not

## Status

Accepted. Applies `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` to the
second feature of that shape. D-005 (the agent proposes, a human decides) and D-032's *reason* both
stand — what goes is one unreachable implementation of the second half, not the rule.

## Context

D-032 built an asynchronous "Save this knowledge? [Yes] [No]" hold: a candidate answer is held
durably by `InteractionApprovalWorkflow` until a human clicks, and only on Yes does the PR-gate
activity run. Three HTTP routes serve it (`GET /approvals`, `GET /approvals/{id}`,
`POST /approvals/{id}/decision`), one SSE event type announces it (`ApprovalRequestEvent`), one turn
signal carries the handle (`ApprovalSignal` / `record_approval_request`), and one dependency scopes
it to its owner (`api/deps.owned_approval`).

**Nothing starts a hold.** `agent/interaction_tools.start_approval` is the only caller of
`client.start_workflow(InteractionApprovalWorkflow.run, …)`, and it has zero callers in `src/`:

```
$ grep -rn "start_approval" src/ --include='*.py'
src/chemclaw/agent/interaction_tools.py:6:   … `start_approval` surfaces a candidate   (docstring)
src/chemclaw/agent/interaction_tools.py:36:async def start_approval(candidate: InteractionCandidate) -> str:
src/chemclaw/agent/interaction_tools.py:90:  … but nothing populated it: `start_approval`   (docstring)
src/chemclaw/core/turn_signals.py:72:        … `start_approval` returns the id *into the model's context*  (docstring)
```

Every route that could reach a hold was ruled in; every path that could open one was ruled out. It
is not an MCP tool — `tests/test_approvals.py:165` asserts `"start_approval" not in names`
deliberately, because a tool would let the agent approve its own candidate. It is not a FastAPI
route: `api/routes/approvals.py` registers the three *consumers* and there is no `POST /approvals`.
It is not resolved by a manifest string, an `__all__` re-export, a `getattr`, a console script or a
Temporal registration. `api/app.py` imports `approval_owner`, `approval_status`, `decide_approval`
and `list_pending_approvals` — all four consumers — and deliberately not the producer.

So `GET /approvals` can only ever return `[]`, and the two id-addressed routes can only 404. The
module docstring says the opposite in the present tense: *"These thin adapters are the one working
reference caller for that workflow — the seam a chat UI hooks onto."* There is no such caller.

This is the `record_handoff` shape exactly. It is worse than dead code, because three of the
surfaces are *controls*: an owner-scoped decision route and an event type carrying a handle read as
a human sign-off that exists, and it does not.

The synchronous half is untouched and is what actually runs: `agent/memory_tools.record_confirmed_answer`
calls `memory.interaction.propose_confirmed_answer` directly, and the resulting note lands on a
branch for the real human PR review. The human decision D-005 requires is therefore taken —
in the pull request, which is the gate the whole architecture is arranged around.

## Decision

**Delete the asynchronous hold and everything that exists only to serve it**, and pin its absence.

Removed: `agent/interaction_tools.py`, `durable/interaction_approval.py`, `api/routes/approvals.py`,
`api/deps.owned_approval`, `api/schemas.ApprovalDecisionIn` / `ApprovalStatusOut`,
`api/events.ApprovalRequestEvent` (and its `Event` union member),
`core/turn_signals.ApprovalSignal` / `record_approval_request` (and its `Signal` union member), the
`api/app` re-exports, the background worker's import, and `interaction_approval_timeout_seconds`.

`tests/test_turn_signals.py::test_nothing_opens_a_durable_approval_hold` is the absence test: no
module under `src/` starts an `InteractionApprovalWorkflow` or emits an approval turn signal.
Whoever re-adds the hold fails it, which is the point — **the producer and the surface have to
arrive in the same change.**

**The line this draws, and why it is not "delete everything with no caller in `src/`":** a thing no
*configuration* can reach is dead; a thing a *deployment* selects is not. `publish/drivers/http.py`
and `kg/crosslink.py` were examined in the same sweep and kept, because the first is a shipped
driver a site names in its own `sink.yaml` (`D-2026-08-25-a-cache-is-not-a-record` ships it
deliberately) and the second is recorded as declared-but-unwired in `kg/README.md` by
`D-2026-08-05-three-searches-that-disagreed-about-one-note`, with its write side live
(`propose_knowledge_note(calc_refs=…)`). No knob and no manifest can cause `start_approval` to run.

## Consequences

- **The wire contract loses a member.** `approval_request` leaves the `Event` union and
  `tests/fixtures/turn_events_contract.json` is regenerated in the same change. Removing a member
  is the safe direction — a mirror that still declares it simply never receives it — but
  `Chemclaw3_ui`'s `shared/events.ts` and `Chemclaw3_mock` should drop it, and that is a follow-up
  in those repositories rather than a break in this one.
- **`GET /approvals` is gone rather than empty.** A surface that called it received `[]`; it now
  receives 404. That is the honest answer, and it is the reason a UI that had "wired approvals"
  never worked.
- **`docs/planning/BACKLOG.md`'s "A decided approval hold can be reopened" row is deleted, not
  fixed.** It described an `id_reuse_policy` defect inside `start_approval`. With no producer the
  bug was unreachable; with no `start_approval` the row has no subject. The *finding* survives here:
  the distinction Temporal's reuse policies cannot express is "closed with a decision" versus
  "closed without one", and whoever re-adds a hold needs the prior run's terminal outcome read
  before starting rather than a policy.
- **`tests/test_third_party_layering.py` loses one `_KNOWN_LEAKS` row.** `interaction_tools.py` was
  one of five copies of the Temporal launch idiom inside layer 1; four remain and the fix they want
  (one `start_job()` in `durable/`) is unchanged.

## What a re-adder owes

1. **A producer in the same change.** Not a tool — the agent may not open a hold it can then be
   asked about — and not a test. The intended shape was a route or a runner hook on
   `record_confirmed_answer`'s path.
2. **The reuse question answered, not passed.** `REJECT_DUPLICATE` and
   `ALLOW_DUPLICATE_FAILED_ONLY` both fail on expiry, because an expired hold *completes*; read the
   prior run's terminal outcome.
3. **The event member back in the union, the fixture regenerated, and both mirrors updated** —
   `tests/test_event_contract.py` says why.
4. **An argument that it is not the PR-gate twice.** The synchronous path already ends in a pull
   request a human merges. A second human decision in front of it needs a reason beyond "a button
   is nicer", and D-032's reason — the click outlives the turn — is the one to restate.

## What was rejected

- **Wiring `start_approval` into `record_confirmed_answer`.** It would put a second human gate in
  front of the PR-gate for every confirmed answer, on a code path with a known unfixed reuse defect,
  to serve a UI affordance no surface in this family renders. Turning a feature on is a product
  decision; a dead-code sweep is not where it gets taken by default.
- **Keeping the routes and deleting only the producer chain.** That is the state this ADR exists to
  end: consumers with no producer are what made the control look real.
- **Keeping `ApprovalRequestEvent` "for plan approval".** Its docstring offers that reading and
  `api/graph_stream.py` refutes it in a comment: *"Plan approval is not this: that is
  `chemclaw.agent.plan_gate`, and it never reaches this stream."*

## Verification

477 lines of `src/` go: three whole modules (383) and the wiring that reached them (94), plus 366
lines of tests replaced by one 27-line absence test.

`tests/test_event_contract.py` regenerated and re-asserted — `approval_request` leaves the golden
file, which is the deliberate act that docstring asks for. Green: `test_turn_signals`,
`test_route_auth_coverage`, `test_layering`, `test_third_party_layering`, `test_repo_map`,
`test_decision_log`, `test_docstring_paths`, `test_event_contract`, `test_cli`, `test_compaction`,
`test_message_migration`. `ruff check`, `ruff format --check` and `mypy --strict` report nothing in
any file this change touches.

Two stale declarations were found by running the gates rather than by reading: `test_layering`'s
`_AGENT_LAUNCH_SURFACE` imported the deleted module by name, and `test_third_party_layering`'s
`_KNOWN_LEAKS` held its Temporal row — the ratchet working, which is why neither is a policy edit
made on faith. `tests/test_entra_end_to_end.py` no longer asserts a 401 on `/approvals`, because a
route that does not exist answers 404 and asserting otherwise would pin the absence as an auth
result.
