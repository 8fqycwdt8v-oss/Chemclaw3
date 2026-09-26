# D-2026-09-26-a-launcher-no-profile-names-is-withheld-when-its-capability-is-off — which template launchers a deployment binds

**Status:** accepted · **Date:** 2026-09-26 · Closes the `BACKLOG.md` row *"A template launcher is
bound whatever its tools are, so an opt-in capability's template costs every deployment prefix it
cannot use"* (issue #451), and narrows the refusal in `templates.registry.unrunnable_reason`'s
docstring to the templates it was measured on.

## Context

Every enabled template got a `run_<name>` launcher whatever the connector set, and a launcher whose
steps do not resolve was refused at launch rather than withdrawn. The reason was measured and still
holds: `data/profiles/computation.yaml` and `safety.yaml` name nine launchers between them, and
`chemclaw_agent._reject_unknown_tool_names` *raises* when a profile lists a name the surface lacks,
so withdrawing an unrunnable launcher turns "one procedure is unavailable" into "every turn on this
profile fails at build".

`scale-up-thermal-envelope` was the first template whose steps are an opt-in bundle's
(`thermalsafety`, `default_enabled: false`), and it was parked in `docs/archive/proposed-templates/`
because binding it costs every default deployment prefix for a launcher that deployment refuses.
Measured with `tests/test_context_floor.py::_floor` on the `default` profile, the template back in
`data/templates/`, twice each, identical both times:

| launcher | `default` static prefix |
|---|---|
| bound (every enabled template) | 73,181 |
| withheld (this decision) | 72,398 |

**783 tokens on every model call**, and the bound figure is over `CEILINGS["__default__"]`
(72,850), so shipping the template without this would have been a ceiling raise. The six narrowing
profiles measure the same either way: they list their tools, and none lists this launcher.

## Decision

**A launcher is withheld when no profile names it and a tool or job its steps call is declared by
a bundle here and not bound by this deployment** (`templates.registry.withheld_reason`, over
`agent/template_surface.unbound_opt_in_references` and `profile_named_tools`). Withheld means not
bound: absent from `template_tools()`, `template_tool_names()`, `available_tool_names()` and every
compiled graph. Enabling the bundle binds it with no other change.

It is still **declared**: `template_tools(declared=True)` and `chemclaw_agent.declared_tool_names`
include it, so `make template-validate`, the skill and prose validators and the prose contract keep
reading it — the same split `D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`
made for an opt-in bundle's own tools.

"Declared here and not bound" is the predicate, rather than "not in the tool union", on purpose: a
template naming a tool that nothing declares is a typo or a deletion, not an opt-in capability, and
it keeps its launcher and its refusal-at-launch naming the missing tool. And it is asked of the
connector registry alone, because the launcher names are one of the seven spaces the union
assembles.

## Alternatives

- **Keep binding every launcher and leave the template parked.** Declined: it keeps a finished,
  argument-checked procedure out of the tree to avoid a cost this rule removes.
- **Withhold every launcher whose steps do not resolve.** Declined, for the measurement above:
  a profile naming a withheld launcher fails every build.
  **Revisit when:** `chemclaw_agent._reject_unknown_tool_names` no longer raises for a name that
  is declared but unbound — then a profile could name a withheld launcher and the profile half of
  this rule would be unnecessary.
- **Defer the launcher's schema instead of withholding it**
  (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`). Not taken here: it is designed and
  unbuilt, and would still bind a launcher the deployment refuses.

## Consequences

- Registration is once per process (`chemclaw_agent._register_generated_tools`), so the decision
  is a deployment's, taken at its first build — the connector enable-list and the profile files
  are configuration, not turn state.
- `authz.side_effecting_tools` and `connectors/registry._bound_by_this_process` read the bound
  names, so a withheld launcher is neither gated nor reserved; it cannot be called either.
- `tests/test_template_withholding.py` holds all four edges: withheld by default, bound when the
  bundle is on, bound whenever a profile names it, and still declared.
