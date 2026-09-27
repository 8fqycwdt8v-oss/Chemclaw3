# D-2026-09-12-a-tool-list-is-a-name-space-whichever-argument-it-arrives-on — the two name spaces a connector could still capture are closed

**Context.** `D-2026-09-04-a-name-is-one-capability-across-every-namespace` established the rule:
a tool name is an authorization key, `authz` classifies by it, the plan gate and the write gate
look a capability up by it, and the model has exactly one of them to call. Two capabilities sharing
one name means one capability's gate silently applies to the other's work.

`connectors/registry._declared_tool_names` and `_bound_by_this_process` enforce that over
*manifests*, and that is the path a deployment takes. Two name spaces were outside it, each found
by a review of the guard rather than by an incident, and each measured rather than reasoned about.

## 1. A connector could claim a step-template launcher, on exactly the cold paths

`_bound_by_this_process` refuses a bundle claiming an in-process tool, a scratchpad verb, a
plan-harness tool or the subagent spawner. Its docstring said template launchers were a fourth name
space it could not cover, and filed the gap. Re-driven:

```
cold: NOT REFUSED — a connector may claim 'run_bond_strength_survey'
```

**Cold is the shipped path**, which is what makes this worse than the filed row read.
`chemclaw_agent._register_generated_tools` is `[*job_tools(), *template_tools()]`, so the collision
check has already returned before the first launcher is registered — and `make connector-validate`
and a fresh pod loading its manifests are exactly processes that have never built an agent. In a
*warm* process the same bundle was refused, as **"an in-process tool"**: the right outcome for the
wrong reason, which is a worse failure than a clean one, because the reason is what an operator
reads to work out what to rename.

**Decision.** The launcher names are asked for directly, beside the three that already are, rather
than hoped for in the registry. The row named two candidate fixes and took neither: reading
`chemclaw.templates.registry` from `connectors/registry` is a new import edge, and moving the
collision check after both registrations changes *when* a misconfiguration is reported. Neither
cost is paid. `_bound_by_this_process` already does a function-scope
`from chemclaw.agent import chemclaw_agent` — the declared `connectors -> agent` edge — and
`chemclaw_agent` is where all six name spaces are assembled for `available_tool_names`, so the
fourth `bound.update(...)` reads `chemclaw_agent.template_tool_names()` and nothing moves. It also
settles the question the row said was the decision: **`chemclaw.templates.registry` owns that name
space**, and `connectors/registry` asks it rather than inferring it.

`template_tool_names` is re-exported from `chemclaw_agent` with an explicit `as`, because
`mypy --strict` runs with no implicit re-export; the alias is the declaration that this module
publishes it, which it does for the same reason it publishes the other three.

## 2. `build_langgraph_agent(connectors=...)` accepted a tool shadowing a first-party name

The residual `D-2026-09-04` named and left open. Driven, binding a `StructuredTool` called
`record_knowledge_note` through the keyword:

```
NOT REFUSED: 61 tools bound; 'record_knowledge_note' is 'a connector tool claiming a first-party name'
still classified state-changing: True
```

`ToolNode` keys `tools_by_name` by name and the connector half is appended second, so the connector
tool simply wins and the first-party writer is gone — no error, no warning.

**The second line is why this is not a typing gap.** The name stays in
`authz.side_effecting_tools()`, so the plan gate, the write gate and the audit trail all fire on
the *first-party capability's* identity while the connector's body runs behind them. A deployment
would see a knowledge-graph write gated, approved, recorded — and something else executed.

**Decision.** The check goes beside the concatenation, over the names the first list declares, and
raises `ConnectorError` worded like the registry's so an operator reading either does not have to
work out that they are the same rule. The keyword is a test and front-door seam rather than a
configuration surface — `api/runner.py` passes the turn's opened sessions — which is why this was
"closed in practice and open in the type"; it is now closed in both.

## What keeps it true

| property | test |
| --- | --- |
| a bundle claiming a step-template launcher is refused, **and refused as one** — the parametrized arm asserts the operator-facing reason belongs to the name space that owns the name | `tests/test_connector_registry.py::test_every_ambient_name_space_is_refused_to_a_connector[template_tool_names-a step-template launcher]` |
| a connector tool cannot take a first-party name through `connectors=`, with the classification asserted as the precondition and an innocent connector tool as the positive control | `tests/test_langgraph_agent.py::test_a_connector_tool_cannot_take_a_first_party_name_through_the_connectors_argument` |
| a generated launcher is still not read back as a collision on a second build | `tests/test_connector_registry.py::test_a_generated_launcher_is_not_read_back_as_a_collision_on_a_second_build` |
| no new import edge was opened | `tests/test_layering.py` |

Four mutations, each watched failing: dropping the new `bound.update(...)` line, drifting its
reason string to another name space's, deleting the concatenation's collision check, and widening
it to refuse every connector tool — the last reddening the positive control and one neighbouring
test, which is what a check that refused everything would do to a deployment.

**Superseded prose, corrected in the same commit:** `_bound_by_this_process`'s "filed in
`docs/planning/BACKLOG.md` rather than closed here" paragraph is gone, and the table in
`tests/test_connector_registry.py` says why it is four rows rather than three.
