# D-2026-08-26-the-envelope-is-not-the-result — a bundle publishes the model it computed, and a guard derives what it asserts

## Status

Accepted. Amends `D-2026-08-26-a-route-is-not-a-shape`, which stands and is the right design: a
route never identifies a shape, so the envelope carries `payload_kind`. What it got wrong is
*which value's name to put there* for the one bundle that wraps its results.

## Context

`D-2026-08-26-a-route-is-not-a-shape` fixed the publish seam by having each workflow set
`payload_kind` "from `type(result).__name__` at the sites that still hold a typed result". For `qm`
that site holds `QMJobResult` — its domain model — and `qm` published. For `calc` the same
expression holds `XtbJobResult`, which is **not** a domain model: it is an envelope with `kind`,
`summary`, `calc_refs`, and nine optional members of which exactly one is populated. So every
`calc` job stamped `payload_kind="XtbJobResult"`, which is in no projector table, and the
composite half of the seam stayed exactly as inert as before for the bundle holding nine of the
eleven shipped jobs.

Measured on the merged code, with the test suite's own fixture through the production expression:

```
what the TEST pairs   : calc.compute_reaction_energy + ReactionEnergyResult -> _reaction
what PRODUCTION builds: calc.compute_reaction_energy + XtbJobResult         -> None
rows queued (production shape): 0
rows queued (test's shape)    : 1
```

Two smaller findings from the same sweep, both the same shape — a string-keyed table describing
something it does not read:

- `_CALC_TYPE_PROJECTORS` listed `descriptors`. The descriptor panel has **always** stamped
  `developability` (checked against every engine's `CALC_TYPE` before and after
  `D-2026-08-16-the-physics-leaves-the-cache-stays`), so every `predict_developability_profile`
  result was dropped at the enqueue with a debug line. Three further entries — `logd`,
  `xtb.thermo`, `xtb.energy` — name spellings no version of this system ever wrote.
- `xtb.hess` is stamped and has no projector at all.

## Why the tests did not see any of it

`tests/test_publish_reaches_the_hooks.py` exists **specifically** to catch this class, and its
docstring says so: *"every test here starts at a real hook … a projector that no path can reach
fails here and passes there."* It then hardcoded four `(calc_type, payload_kind)` pairs "each with
the model its workflow returns", and all three claims in that sentence were false — the bundles
ship eleven jobs, one of the four named a *tool*, and the two `calc` pairs named inner models the
workflow never names. `test_publish_projection.py` compounded it by exercising `descriptors`,
`xtb.thermo`, `xtb.scan` and `xtb.energy` — four spellings nothing emits — and neither live one.

The general failure is worth naming because this repository keeps meeting it, and it is one level
below `D-2026-08-25-the-number-was-measured-on-a-path-production-does-not-use`:

> **A test that writes down what production does is a claim about production, and it is exactly as
> stale as any other comment.** Starting "at a hook" is not enough if the hook is one the test
> reconstructed. It has to *call* the thing.

## Decision

**A connector bundle's envelope publishes the model it computed, not the wrapper it arrived in.**
`XtbJobResult.outcome()` returns the one populated member — recognised by *type* (`BaseModel`), so
a tenth result shape is one field and no list to extend — and `job_envelope` sets `payload_kind`
from it and dumps it as `data`. This makes `calc` match `qm` rather than making `publish` special:
it is also the only fix available on this side, because `tests/test_layering.py` permits
`connectors -> publish` and not the inverse, so a projector may not unwrap a connector's envelope.

`data` is therefore the domain result with no wrapper key. That is a contract change with two
consumers, both updated: one shipped template reference and `tests/test_step_handoff.py`. It is
also the better contract — `kind` and `summary` were duplicated into `data` while already riding
on `ConnectorJobResult`.

**`job_envelope` is a module-level function, and `run` is one line.** The property worth asserting
is purity — a replay must produce byte-identical output — and a test can only assert that by
calling what the workflow calls. The first version of this fix was three lines inside `run` with a
test that copied them, which is the same defect one level down.

**A guard over a registry derives its inputs from whatever decides them.** The assertions now
parametrise over `XtbJobResult`'s member fields, the connector manifests, and
`calc_server_fake._KEYED`. A new job, result shape or cache type reaches them with no edit.

**A gap that cannot be closed here is declared, not omitted.** `_NOT_YET_PUBLISHED` names the four
multi-step shapes with no projector and `_PRIMITIVES_NOT_PUBLISHED` names `xtb.hess`; a shape *not*
named must route. This is the `tests/test_upstream_surface.py` pattern — asserting an absence so
that closing it turns the test red rather than letting the workaround outlive its reason.

**A retired calculator keeps its projector; a spelling that never existed does not get one.**
`calculation_results` is never pruned, so `xtb.scan` — a real `XtbTask` before the physics moved —
stays. The four that were never stamped go.

## Consequences

Measured the same way as the finding:

| | before | after |
| --- | --- | --- |
| primitive calculators publishing | 8 of 10 | 9 of 10 (`xtb.hess` declared) |
| durable jobs publishing | 1 of 11 | 6 of 11 (4 declared, 1 undecided) |

End to end against Postgres, a reaction-energy job and a descriptor panel each queue 1 row where
both queued 0. Reverting the workflow fix in place turns the new guard red with
`assert 'XtbJobResult' == 'ReactionEnergyResult'`.

What stays open, each with a `docs/planning/BACKLOG.md` row: projectors for `RefinedEnsemble`,
`EnsembleProperty`, `SpeciesDistribution` and `BondDissociationSurvey`; a third hook for
frequencies, since `ThermochemistryResult` is a tool composite that is neither cached nor returned
by a job; and whether a BO campaign is a scientific record at all.

Not changed: `bo` still stamps `CampaignResult` with no projector. That is either right or wrong
and the code cannot say which, which is why it is a decision to take rather than a line to add.
