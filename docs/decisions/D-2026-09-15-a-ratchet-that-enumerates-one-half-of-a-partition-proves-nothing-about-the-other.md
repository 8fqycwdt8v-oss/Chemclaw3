# D-2026-09-15-a-ratchet-that-enumerates-one-half-of-a-partition-proves-nothing-about-the-other — A ratchet that enumerates one half of a partition proves nothing about the other

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** the two `BACKLOG.md` §1 plan-scope rows,
closed together because both are about the same object — what a plan declares, and what the gate
does with it. Supersedes nothing.

## Context

`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` left behind a ratchet:
`tests/test_plan_scope.py::test_the_surface_a_read_only_plans_approval_reaches` drives **every** name
in `authz.side_effecting_tools()` through the plan gate under an approval that declared nothing, and
requires every one to be refused. Its docstring makes the property explicit — *"the assertion is over
`side_effecting_tools()` itself rather than a list, so a bundle enabled next year is covered the day
it is enabled"*.

That is true, and it is a claim about one half of a partition.

`authz.side_effecting_call` is a **union**: the names a gate can enumerate, plus the calls whose
gatedness is a function of their *arguments*. `write_file` and `edit_file` serve two roots —
`/scratch/` dies with the turn, `/memories/` outlives the deployment — so gating by name would
refuse the agent its own notepad, and `writes_durable_memory` reads the path instead.

## Measured

```
argument-driven verbs: ['edit_file', 'write_file']
  edit_file    in side_effecting_tools(): False   durable-call: True   scratch-call: False
  write_file   in side_effecting_tools(): False   durable-call: True   scratch-call: False
ratchet surface size: 49
```

So the ratchet walks 49 names and **both** argument-driven verbs are outside them. The gate does
refuse them — this is a coverage gap, not a live bypass — and nothing held that it would, which is
the shape `CLAUDE.md` names as *"a claim that a control exists"*. **The backlog row that found it
named one verb; there are two**, which is itself the argument for deriving rather than listing.

The second row, measured on the shipped schema:

```
validated: 50000 names          declared_scope size: 50000
refusal chars: 600192
todos accepted: 20000
```

Both halves of a plan were unbounded. **The row named the per-step declaration and not the step
count**, so that half had never been measured either.

## Decision

**`authz.memory_write_verbs()` exposes the second half**, and the ratchet gains an arm that derives
from it plus `scratchpad.MEMORY_ROOT` — the same two sources `writes_durable_memory` itself reads.
No name appears in the test. A third argument-driven verb is covered the day it is added, which is
the property the first arm already claimed for bundles.

The arm asserts **both directions**, and the second is what stops a blunt name-gate passing it: a
`/scratch/` write must still succeed under an approval that declared nothing, or the gate has
refused the agent its notepad rather than its memory. Two further assertions keep it honest — that
the verbs are still argument-classified, and that they are still *outside* `side_effecting_tools()`,
so if the partition ever moves this test says so instead of quietly measuring the same thing twice.

**`plan_max_steps` (64) and `plan_max_tools_per_step` (32)** bound the declaration, enforced by a
`model_validator` on `ScopedWriteTodosInput`. Two design points:

- **A validator rather than `Field(max_length=...)`**, which is what the backlog row proposed.
  `ScopedTodo` is a `TypedDict` whose annotations are evaluated at class definition, so an
  `Annotated[...]` bound could only carry a literal — and a threshold in this repository comes from
  the one settings object. A validator also names the offending step, which pydantic's own
  `too_long` does not.
- **Refused at the tool's own argument validation**, which is the one place the model reads the
  error and can act on it: *"step 3 declares 50 tools and at most 32 are accepted per step. Split it
  into steps that each name the tools they will actually call."*

**Neither is an escalation**, which is why both are bounds rather than gates: the scope only ever
*narrows* what a call may do, and a name no tool answers to is refused by `enforce_tool_authz`
regardless. What they close is an unpriced write a model can repeat.

## What keeps it true

- `tests/test_plan_scope.py::test_the_gate_also_reaches_the_half_of_the_surface_a_name_cannot_enumerate`
  — driven against the defect arm before it was believed: with the `writes_durable_memory` half
  removed from `side_effecting_call`, it fails naming `['edit_file', 'write_file']`; restored, all
  12 pass. A guard nobody has seen fail is a guard nobody has seen.
- `tests/test_plan_scope.py::test_a_plan_is_bounded_in_both_directions_at_argument_validation`
  — both halves, with the numbers read off `settings` so the assertion is that the bound *binds*
  rather than that it is 32.
- `tests/test_plan_scope.py::test_the_refusal_names_the_step_so_the_model_can_split_the_plan`
  — the message, since refusing at argument validation is wasted on one the model cannot act on.
- `tests/test_plan_scope.py::test_an_ordinary_plan_is_nowhere_near_either_bound`
  — asserted as a *ratio* against real use, because "an eight-step plan validates" would stay true
  at a bound of nine.
- `tests/test_config.py::test_env_example_documents_every_field` — which caught the missing
  `.env.example` rows before the gate did, this time.
