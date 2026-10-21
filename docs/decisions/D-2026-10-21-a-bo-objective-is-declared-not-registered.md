# D-2026-10-21-a-bo-objective-is-declared-not-registered — a campaign's objective is a spec over any keyed primitive or a measurement, not an entry in a code registry

**Status:** proposed · **Date:** 2026-10-21

## Context

`science/bo/objectives.py` holds a static `_REGISTRY` with two computed objectives
(`reizman_suzuki`, `solubility_max`) and the `measured` sentinel. Each factory is fixed to
`Callable[[LogSFor], Objective]`, so the only calculator a campaign can consult is one solubility
callable, bound in `connectors/bo/calculators.py`. Optimising against any other computed property
needs Python in two packages. That includes a DFT quantity from
D-2026-10-21-long-running-compute-runs-on-a-backend-seam.

## Options

1. **Static registry** (status quo). Each new objective is a code change and a release.
2. **Declarative `ObjectiveSpec`.**
   - Either `Computed{primitive, field, direction, transform, backend?}` or `Measured{unit}`.
   - Shipped specs live in `data/objectives/*.yaml`.
   - The factory takes one generic `Evaluate` callable, built from the backend-agnostic
     `cached_primitive`.
3. **Let the agent pass an arbitrary objective function.** Code from a model in a durable workflow
   is declined everywhere else in this system.

## Decision

**Option 2.**
- The two shipped objectives are ported to specs verbatim; `tests/test_bo_campaign.py` proves
  unchanged behaviour.
- An objective on a long-running backend evaluates each batch as child `ExternalJobWorkflow`s
  through `fan_out`.
- `_cannot_afford_another_dispatch` also consults the compute budget
  (D-2026-10-21-compute-is-budgeted-beside-tokens).

## Consequences

- `require_campaign_startable` resolves the spec. It refuses a primitive that serves no
  `calculation_key` (D-2026-10-21-every-compute-primitive-serves-a-calculation-key), so every
  evaluation is cacheable.
- Multi-objective optimisation stays out of scope. BoFire supports it, and adopting it is its own
  decision.
- Plan item: W13 Track B.

Revisit when: an objective a shipped campaign needs cannot be written as one keyed primitive's field
(a composite of several). The next step is composing specs, not returning to code.
