# D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool — a plan step declares what it will call, and the approval is bound to the declaration

**Context.** D-137 made the plan approval a durable row. D-167 made it bind an *act* rather than
latch onto a session: an approval is keyed to `(session_id, plan_hash)`, is spent by the turn that
used it, and `enforce_plan_approval` refuses a state-changing tool whose plan holds no live
approval. Everything about *which plan* a person said yes to is settled and tested.

Nothing bounded *what saying yes let the agent do*. `enforce_plan_approval` asked one question —
does an approval stand for this plan — and the tool being called was never compared to anything.
Driven at `11cf67e5` against the gate, with an approval recorded for the one-line, read-only plan
`["look up the melting point of aspirin"]`:

```
side_effecting_tools(): 49
PERMITTED under the read-only plan's approval: 49
REFUSED: 0
```

Every knowledge-graph write, every durable launcher, every enabled bundle's state-changing surface.
The partition behind that number is 12 in-process `STATE_CHANGING_TOOLS`, 9 template launchers and
28 connector tools and jobs — so the surface an approval reaches *grows with every bundle a
deployment enables*, and the thing it was approved for does not. Combined with the unframed
injection surfaces this repository already tracks, that is the amplifier: untrusted text reaching
the model during an approved turn reaches the full write surface while the chemist believes they
approved a lookup.

`docs/planning/BACKLOG.md` carried this as an `[L]` and said the clean fix is not a patch, which is
right. It also said the gate "binds plan *content* only … deliberately left as a feature rather
than shipped as a heuristic", which has been true for long enough that the deliberateness had
become the finding.

**Decision.** A plan step declares the tools it will call; the human approves the plan *and the
declaration*; the declaration is stamped onto the approval; and the gate permits a state-changing
call only when the approval it stands on names that tool.

## 1. The declaration is a required field of the plan tool's own schema

The data can only come from the model — which tools carry out a step is a modelling decision taken
when the step is written — so it comes through `write_todos`. `agent/plan_scope.py` subclasses
`TodoListMiddleware`, keeping its prompt, its parallel-rewrite guard, the `todos` channel and the
`write_todos` name, and widening only the argument schema: a step is `content`, `status` and
`tools`.

Scanning the todo prose for tool names stays rejected, for the reason the row gives and this ADR
does not re-litigate: it fails in both directions, and the failure that matters is a legitimate
plan that does not spell a registered name failing to authorize its own step.

**The row does not name the decision that actually had to be taken, which is what happens when a
step declares nothing.** Three answers were available and two of them are bad:

- *Optional, falling through to today's behaviour.* Bounds nothing until the model volunteers to be
  bounded. That is a control that reads as one and is not — the failure this whole review keeps
  finding in this repository's own perimeter.
- *Optional, refusing when absent.* The first omission looks, to a chemist who has approved a plan
  and watched its own steps refused, like an authorization decision rather than a malformed call.
- *Required.* The omission becomes unrepresentable. A `write_todos` call without `tools` fails the
  tool's argument validation, and what the model gets back is the ordinary retryable error —
  measured, driving the real graph:

  ```
  Error invoking tool 'write_todos' with kwargs {'todos': [{'content': 'screen three solvents',
  'status': 'pending'}]} with error:
   todos.0.tools: Field required
   Please fix the error and try again.
  ```

  No plan lacking a declaration ever reaches a human, so there is no fall-through to decide about
  and no knob to ship it switched off. An empty list stays expressible and means what it says:
  this step changes nothing.

Required is taken. The choice is visible where the row asked it to be — in the schema the model is
shown, and in a refusal sentence that names what the approval does cover rather than repeating the
"not approved yet" one.

## 2. The scope is stamped by the decision and never re-read from the plan

`plan_approvals` gains `scope TEXT[] NOT NULL DEFAULT '{}'`
(`infra/sql/095_plan_approval_scope.sql`). The two decision surfaces — `POST /sessions/{id}/plan/decision`
and the CLI's `/approve` — derive it with `declared_scope` over the plan they are deciding on, and
the gate reads it back off the row.

**That direction is the whole control, and the alternative is what makes it necessary.**
`plan_identity` hashes `content` only — deliberately, because that is what lets the canonical
harness batch ("tick the finished step, run the next step's tool") keep its standing approval
(`plan_gate.plan_after_batch`). So a rewrite that keeps every step's text and widens its `tools`
hashes to the *same approved plan*. A gate that read the declaration off the live todo list would
therefore let the model grant itself a tool by editing its own plan; a gate that reads the row
cannot. Nothing about `plan_identity` changed, so no approval a deployment has already recorded is
invalidated by the hash, and the status-flip exemption is untouched.

**`DEFAULT '{}'` rather than a NULL meaning "unbounded", and the direction of that default is a
decision rather than a detail.** An approval recorded before this migration ran gets the empty
scope, so it authorizes nothing and the chemist is asked again. Migration 034 chose the opposite
direction — re-arming live approvals rather than revoking them — because there the fail-open cost
was one extra approval request. Here it is the whole control: a NULL meaning "everything" would
make every pre-upgrade row a standing authorization for the full surface, and would leave a second,
permanently fail-open meaning inside the column this migration exists to close. An approval is one
turn wide, so what the strict direction costs is bounded by the deploy window.

## 3. What a person is shown

`GET /sessions/{id}/plan`, `GET /plans/pending` and the CLI's `/plan` all carry the declaration
beside the steps. This is not decoration: the gate enforces the scope, so a surface that rendered
the steps alone would be collecting a yes to something it had not displayed. `PlanStatusOut.scope`
and `PendingPlan.scope` default to `[]`, so the companion UI is not broken by the addition and
picks the field up when it wants it.

`declared_scope` fails **closed** on anything it cannot read — a `tools` that is a bare string, a
number, or absent contributes nothing. The scope is what a call is checked *against*, so a
malformed plan must narrow the authorization, never widen it.

## What this does not close

The gate is per *plan*, not per *step*: the scope is the union over the plan's steps, because a
batch that ticks step N while running step N+1's tool is the canonical shape and a per-step scope
would refuse exactly it. A plan whose last step declares a knowledge write therefore authorizes
that write from its first step onward. Narrowing to the step in flight is a separate decision and
would need `plan_link`'s reading of "which step is this call serving", which exists.

`HumanInTheLoopMiddleware` stays declined for the plan gate itself
(`D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question`). Nothing
here bears on per-call approval of an irreversible action, which is still open.

## What keeps it true

| property | test |
| --- | --- |
| an approval for a plan declaring tool A refuses tool B, in the same session, under the same live approval | `tests/test_plan_scope.py::test_an_approval_for_one_tool_does_not_authorize_another` |
| an approval for a plan that declares nothing authorizes **none** of `side_effecting_tools()` — the ratchet the 49/49 figure above lives in, taken over the live surface so a bundle enabled next year is covered | `tests/test_plan_scope.py::test_the_surface_a_read_only_plans_approval_reaches` |
| rewriting a step's declaration after approval does not widen the approval, although the plan hashes identically | `tests/test_plan_scope.py::test_widening_a_step_after_approval_does_not_widen_the_approval` |
| a step cannot be written without declaring its tools, and declaring none is still expressible | `tests/test_plan_scope.py::test_a_plan_step_cannot_be_written_without_declaring_its_tools` |
| an unreadable declaration narrows rather than widens | `tests/test_plan_scope.py::test_an_unreadable_declaration_narrows_rather_than_widens` |
| the decision route records what the plan declared, and shows it before asking | `tests/test_plan_scope.py::test_the_decision_route_records_what_the_plan_declared` |
| the CLI's `/approve` records the same scope and `/plan` displays it | `tests/test_cli.py::test_approve_records_and_arms_a_real_plan`, `tests/test_cli.py::test_the_plan_command_reads_the_store_the_turns_wrote_to` |
| the durable backend round-trips a scope, and a row written before migration 095 comes back authorizing nothing | `tests/test_plan_scope.py::test_the_durable_backend_round_trips_a_scope_and_defaults_a_legacy_row_to_none` |
| the two refusals are different sentences, because they have different remedies | `tests/test_plan_scope.py::test_a_refusal_outside_the_scope_names_what_was_approved` |
| the subclass still binds `write_todos` over the `todos` channel, so the gate's batch rule and the plan link keep working | `tests/test_plan_scope.py::test_the_scoped_plan_tool_is_still_the_one_the_gate_and_the_harness_know` |
| upstream still exposes the three attributes the subclass reads and replaces, and its `Todo` still carries only the two keys `ScopedTodo` restates | `tests/test_upstream_surface.py::test_the_todo_middleware_still_lets_a_subclass_replace_its_tool_and_read_its_prompts`, `tests/test_upstream_surface.py::test_a_todo_still_carries_only_content_and_status_upstream` |

Six mutations were driven, each watched failing before the commit message was written: deleting the
gate's scope check (3 tests red), recording `()` from the decision route (1), recording `()` from
the CLI (1), making `tools` a `NotRequired` field (1), making `declared_scope` fail open on an
unreadable declaration (1), and reverting the harness to upstream's `TodoListMiddleware` (1).
