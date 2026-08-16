# Tranche 4 — `calc`: the physics leaves, the cache and the orchestration stay

Status: **Chemclaw3 side done.** mcp side: primitives exposed and merged
(`Chemclaw3-mcp#5`); the engine port for `reaction`/`xtb_scan`/`conformers`/`complexes`/`crest_cli`
is that repo's own row and is not needed by anything here — this repository composes those four
from primitives rather than calling them.

## The architecture, after the 2026-08-16 decision

> **Chemclaw3 keeps orchestration and the cache. Chemclaw3-mcp holds the physics, exposed as
> individually-keyed primitives.**

Two ADRs: `D-2026-08-16-the-physics-leaves-the-cache-stays` (the decision) and
`D-2026-08-16-a-key-the-caller-cannot-see-is-a-key-the-caller-can-poison` (what carrying it out
found).

## Steps

- [x] mcp: expose the primitives — `optimize_geometry`, `compute_hessian` (multi-MB `.npy` blobs
      cross the wire as base64), single-point/properties, the scan *step*, and the CREST searches
      whole. `calculation_key` covers every one.
- [x] Chemclaw3: the nine compute tools call `cached_remote`. The agent-facing surface — every
      signature, docstring and return type — is unchanged, because profiles, eval probes and
      `SKILL.md`s name these by string.
- [x] Chemclaw3: `compute_thermochemistry` = remote optimise + remote Hessian + **local** RRHO,
      refinement loop included. `predict_logd` = remote cached pKa + **local** Crippen.
- [x] Chemclaw3: the five Temporal activities compose remote primitives, every remote call wrapped
      in `durable/heartbeat.py::beating`.
- [x] Chemclaw3: `science/calc`'s engines deleted. The surviving models are `models.py`; the
      arithmetic that had to stay is `thermo.py` and `logd.py`.
- [x] `run_cached_with_artifacts` deleted (zero production callers).
- [x] No code here derives a `calc_version`. `calculator_trust`/`calculator_outliers` ask the server
      (`remote_version`); the compute tools read it off the payload, which on a cache hit is the
      version that produced the number rather than the one that is current.
      `tests/test_calc_remote.py`'s `xfail(strict=True)` marker is gone, which is how that was
      verified.
- [x] `CALCULATION_EPOCH` is the one constant both repositories must change in the same PR.

## What the work found that the plan could not

Four silent defects, each measured against the running server, each recorded in the second ADR:

1. **`optimize_geometry` and `relax_structure` derive the same `xtb.opt` key and return different
   payloads.** Caching either poisons the other; the failure is a validation error on a *hit*, deep
   inside a reaction job. Resolution: only the full result is ever stored, and the summary is
   derived locally.
2. **A Fukui key does not name the mode**, and the server re-ranks on the way out — which a cache
   hit never reaches. Resolution: `SiteReactivityResult.ranked_for` re-ranks after the cache.
3. **`multiplicity=None` means the opposite on the two sides.** Sent as-is, every homolysis fails at
   the embed. Resolution: `compose.radical_multiplicity` derives it here and states it.
4. **`CalcServerError` conflated "unreachable" with "refused".** Fine for a tool; opposite outcomes
   for an activity. Split into `CalcServerError` (a `SubsystemUnavailableError`, retryable) and
   `CalcToolError` (a `ChemclawError`, registered non-retryable).

## Verified

`make lint type test` green, plus `kg-validate`, `skill-validate`, `connector-validate`,
`template-validate`, `prose-validate`, `eval-strict` (0 regressions).

Measured against the live server, fresh store per row — `computed` is remote calculations actually
performed:

| | cold | repeat |
|---|---|---|
| `compute_thermochemistry(CCO)` | 0.856 s, **2 computed** | 0.372 s, **0 computed** |
| `compute_thermochemistry(CC(=O)OCC)` | 2.060 s, **4 computed** | 0.626 s, **0 computed** |
| `compute_thermochemistry(ibuprofen)` | 11.469 s, **2 computed** | 0.448 s, **0 computed** |
| `predict_logd(pyridine)` | 0.824 s, **1 computed** | 0.107 s, **0 computed** |

The cold column reproduces the design ADR's in-process baselines (0.816 s / 3.273 s). D-011 holds
across the wire — the `0 computed` column — and the repeat clock is round trips and nothing else, at
~0.11 s each, because a session belongs to exactly one caller.

## Review

**What went well.** The instruction to measure rather than argue is what found all four defects
above: three of them are invisible in source and only show up when you ask the running server what
key it derives. Recording real Hessians into a fixture kept the measured physics (NIST entropies for
H2O/CO2/H2, to a few hundredths) testable offline after the engines left, and pins the base64
`.npy` transport as a side effect.

**What was harder than expected.** `science/bo` called the calculators directly, and the client is
one package above `science` — so completing the split meant inverting that dependency
(`PropertiesFor`/`LogSFor`, bound in `connectors/bo/calculators.py`) rather than excusing a
`science ↔ connectors` cycle. Not in the plan, and not optional.

**A failed approach, recorded so it is not retried.** The first wall-clock measurements showed
`compute_thermochemistry(CCO)` at 115–147 s cold, which read as a runaway refinement loop. It was
not: two abandoned background pytest runs were saturating all four cores. Instrumenting the loop
showed one pass and two remote calls, as designed. **A wall-clock number taken while the test suite
is running is not a measurement** — check the load before believing a timing.
