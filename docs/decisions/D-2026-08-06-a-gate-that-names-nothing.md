# D-2026-08-06-a-gate-that-names-nothing — A gate that names nothing

**Status:** accepted · **Date:** 2026-08-06

## Context

From the whole-codebase security sweep, identity/RBAC lane. This is the second occurrence of one
shape, and the first fix is what makes the second one interesting.

Everything below was measured. Prose is evidence about what its author believed.

## Decision

### The one core durable launcher escaped the derivation

`D-2026-08-01` found that `expensive: true` in a `connector.yaml` had authorized nothing:
`authorize_trigger(job.name)` was called dutifully and returned immediately unless an operator had
*separately* named that job in `entra_expensive_actions`. It fixed that by **deriving** the gate set
from the enabled bundles' manifests, on the right principle — the owner of a capability declares the
fact, so a bundle added next year is gated the day it is enabled rather than the day someone
remembers to extend a list in core.

A job launched from **core** has no manifest to declare it. Exactly one exists:

```
authorize_trigger("request_development_report")   # agent/durable_tools.py:118
```

Measured on the shipped configuration (`entra_required=true`, both role settings empty):

```
derived expensive actions: ['compute_dft_energy', 'compute_interaction_energy',
                            'sample_conformers', 'start_optimization_campaign']
request_development_report gated? False
```

And nothing else covered it — it is in `STATE_CHANGING_TOOLS` but **not** in
`DEFAULT_WRITE_TOOL_GATES`. So any authenticated user could start an unbounded multi-section
research workflow, and the code that looks like it prevents that was inert.

`CORE_EXPENSIVE_ACTIONS` declares it in core, rather than adding it to the chart's
`entra_expensive_actions`, so a deployment gets it without configuring anything — the same property
the manifest derivation gives bundles.

### The generalizable defect is that nothing checked the call sites

Naming one action in one set closes this instance. It does not stop the third one, and there had
already been two. The gate was *armed but not enforced* — this repository's own recurring
anti-pattern, named in `docs/planning/refactor-hardening-plan.md` as the thing that recurs across
independent reviews.

`test_every_hardcoded_authorize_trigger_action_is_actually_gated` AST-walks `src/` for every
`authorize_trigger("literal")` and asserts the literal resolves to something `expensive_actions()`
actually protects. It fails by naming the file and line, so the next occurrence reports itself:

```
authorize_trigger names action(s) that nothing gates, so the call is inert:
{'request_development_report': 'src/chemclaw/agent/durable_tools.py:118'}
```

Dynamic call sites (`connectors/jobs.py` passes `job.name`) are skipped deliberately: the derivation
covers those by construction, and it is the hardcoded ones that can silently name nothing. AST
rather than grep so a call split across lines is still found and a mention in a docstring is not.

## Consequences

- `request_development_report` now requires an `entra_privileged_roles` role under enforcement. A
  deployment that wants it open must say so, which is the direction writes should fail in.
- Adding a core-owned expensive action means adding it to `CORE_EXPENSIVE_ACTIONS`; forgetting to
  means the new test fails with the call site's location.
- The fix is mutation-proven: removing `CORE_EXPENSIVE_ACTIONS` from the union fails the guard.

## Alternatives rejected

- **Adding `request_development_report` to the chart's `entra_expensive_actions`.** Fixes the
  shipped chart and leaves every other deployment — and every fresh install — ungated. The whole
  point of the D-2026-08-01 derivation was that a gate should not depend on operator configuration
  it can silently lack.
- **Adding it to `DEFAULT_WRITE_TOOL_GATES` instead.** That is the per-tool gate, a different
  control with a different failure mode; the action is a *durable job trigger* and belongs with the
  trigger gate that its own call site already invokes.
- **Only fixing the instance.** There had already been two occurrences of this shape. An instance
  fix with no enforcement is how the second one happened.

## Related, not fixed here

The lane's other findings are recorded in `BACKLOG.md`: the built-in write gate never consults the
connector-declared `state_changing` set; the unauthenticated `X-Chemclaw-Actor` header becoming
durable GxP attribution (whose root fix is connector authentication, already tracked); and
`map_to_hpc_identity` having no caller.
