# D-2026-08-06-a-write-gate-that-reads-the-wrong-declaration — A write gate that reads the wrong declaration

**Status:** accepted · **Date:** 2026-08-06

## Context

From the whole-codebase security sweep's authorization lane, filed [L]:

> **The built-in write gate never consults the connector-declared `state_changing` set**
> (`agent/authz.py`). `DEFAULT_WRITE_TOOL_GATES` is a hand-maintained list while every manifest
> already partitions its tools into `state_changing`/`read_only`; deriving the gate from the
> declaration is the same move `expensive_actions()` and `side_effecting_tools()` already make.
> `report_measurement` is the live example — any authenticated user may write the shared calibration
> ledger.

The finding is right and the proposed fix is wrong, which only became visible by running it.

## Decision

### The gap is real

`report_measurement` appends to the calibration ledger. `calculator_trust` computes every chemist's
"how far should I trust this prediction" answer from that ledger. So one wrong or malicious
measurement moves a number the whole site reads — and on the shipped chart
(`entra_required=true`, `tool_authz_default="allow"`, no `tool_role_gates` entry) any authenticated
user could write it, because the gate was a list in core and nobody in core had remembered a `calc`
tool.

### Deriving from `state_changing` would have closed the science

Measured against the enabled bundles before writing any of it:

```
connector-declared state_changing (+jobs): 19
would newly require a privileged role:     18
['compare_solvents', 'compute_electronic_properties', 'compute_interaction_energy',
 'compute_reaction_energy', 'compute_thermochemistry', 'compute_xtb_energy', 'optimize_geometry',
 'predict_developability_profile', 'predict_logd', 'predict_outcome', 'predict_pka',
 'predict_site_reactivity', 'predict_solubility', 'report_measurement', 'sample_conformers',
 'scan_coordinate', 'start_optimization_campaign', 'suggest_next_experiment']
```

`predict_pka` and `compute_xtb_energy` are on that list because a bundle declares them
state-changing — correctly. They burn CPU and write a `calculation_results` row, which is exactly
what the *plan gate* needs to know, since running one under an unapproved plan spends real
resources. It is not what the RBAC write gate needs to know. Deriving one from the other would have
required a privileged role for the system's whole scientific surface in any deployment that turned
on `entra_required` without hand-writing `tool_role_gates` entries.

The distinction already existed in core and had never been named on the connector side.
`agent/authz.py` keeps `STATE_CHANGING_TOOLS` and the write-gate set apart, with a comment saying
they answer "different questions with different blast radii" — `remember_preference` writes, and it
writes *the asking chemist's own* preference, so gating it behind a privileged role would refuse a
user their own settings.

**The axis is whose state the write touches.** One chemist's `predict_pka` cannot change what
another chemist's returns — the cache is keyed on the inputs. `report_measurement` can.

### So the manifest gained the narrower declaration

`endpoint.privileged`: the subset of `state_changing` whose writes are shared across users, refused
at load if it names anything outside `state_changing` (a read is not a privileged write). The same
shape as `JobSpec.expensive` — the bundle owns the fact, so a capability added next year is gated
the day it declares itself rather than the day someone extends a list in core.

`default_write_tool_gates()` unions three sources, each owned where its knowledge lives:

- `CORE_WRITE_TOOLS` — core's own writes, which have no manifest;
- every enabled bundle's declared `privileged` subset;
- `expensive_actions()` — already refused to the same actors by `authorize_trigger` against the
  identical predicate, so including it changes no decision.

That third one lets core stop naming `compute_dft_energy`, which was never core's tool: it is
`qm`'s `expensive: true` job, and the hand entry was a second source of truth about another
bundle's capability.

### What stays hand-written, and why it is not the same defect

`index_molecule` and `index_reaction` remain in `CORE_WRITE_TOOLS` although they belong to `molfp`
and `rxnfp`. It is structural rather than an oversight: both are deliberately absent from those
manifests' agent-facing `tools`, and a manifest may only classify what it serves the agent — so
there is no place to declare them. They are defense in depth anyway, since `allowed_tools` already
keeps them off the agent (D-029).

## Consequences

- `report_measurement` now requires an `entra_privileged_roles` role under enforcement. That is a
  real narrowing for any deployment relying on it being open, and it is the direction a shared-ledger
  write should fail in. An operator who wants it open says so with a `tool_role_gates` entry.
- A bundle declaring `privileged` gets the gate with no core edit. A bundle that declares none is
  unchanged, so the seven shipped manifests move by exactly one line.
- Both halves are mutation-proven: dropping `expensive_actions()` from the union fails the test that
  pins `compute_dft_energy`'s coverage, and emptying `calc`'s `privileged` list fails the test that
  pins `report_measurement`'s. `test_the_write_gate_does_not_close_the_ordinary_calculators` pins the
  measurement's correction in the direction that matters — those tools stay open — so the
  plausible-looking refactor to `state_changing` fails rather than shipping.
- `tests/test_authz.py`'s containment claim moved up a layer: `CORE_WRITE_TOOLS ⊆
  STATE_CHANGING_TOOLS` for the in-process half, and `default_write_tool_gates() ⊆
  side_effecting_tools()` for the derived whole, since the gate now names connector tools that the
  in-process set has never known about.

## Alternatives rejected

- **Deriving from `state_changing`**, as the row proposed. Measured above: 18 tools closed,
  including the calculators the product exists to run.
- **Adding `report_measurement` to the hand-written list.** Closes this instance and leaves the next
  connector write ungated until someone in core remembers — which is the defect, not the symptom.
- **Making `privileged` default to `state_changing` when unset.** Would have the same effect as
  deriving, one indirection later, and would silently change behaviour for a bundle that simply had
  not been updated.
- **A `privileged: true` flag per tool instead of a list on the endpoint.** The manifest has no
  per-tool object for endpoint tools — `tools`, `state_changing` and `read_only` are all name lists —
  so a fourth list is the shape that already exists. `JobSpec` has a per-job object and uses a flag
  there, which is the same rule applied to a different structure.

## Related, not fixed here

The remaining rows of the same lane are in `BACKLOG.md`. `NoAuth`'s docstring — which claimed a
validator that does not exist — is corrected in
`D-2026-08-06-a-connector-that-authenticates-nobody`, since that is where the rule it described
actually landed.
