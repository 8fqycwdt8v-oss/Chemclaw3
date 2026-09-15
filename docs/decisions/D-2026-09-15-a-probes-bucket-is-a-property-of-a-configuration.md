# D-2026-09-15-a-probes-bucket-is-a-property-of-a-configuration — `needs_bundle:` lets the corpus name a tool the fleet serves and this tree does not declare

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** `Probe.needs_bundle`, the coverage tests
that hold it, and pc-06 and an-04 as its two callers. Closes the `docs/planning/BACKLOG.md` row
*"A bundle declared only in the fleet reaches an agent surface with no probe covering it"*, and the
residual `D-2026-09-14-a-bundle-this-tree-does-not-declare-is-still-reachable` left open by name.

## The problem, as the two assertions that disagreed

`tests/test_probe_coverage.py` holds the corpus against `available_tool_names()`, which reads
`CHEMCLAW_CONNECTORS_DIR`. So the corpus sat between two assertions pointing in opposite environment
directions:

| assertion | fails when |
| --- | --- |
| `test_every_agent_callable_tool_is_probed_or_exempt` | the fleet **is** mounted — 121 tools where the corpus covers 114 |
| `test_no_probe_expects_a_tool_that_does_not_exist` | the fleet is **not** mounted and a probe names one of its tools |

There was no bucket a configuration-dependent probe could occupy, so `thermalsafety`, `suitability`
and `kinetics` — three servers that shipped the same day — were unmeasurable by this corpus in every
lane. A developer who ran the four-repo lane's environment and then ran the suite got a red test for
a corpus gap, which teaches people to unset the variable.

## Why not the fix that looks obvious

Declaring the three as in-tree bundles was measured and rejected. They cost **11,624 tokens over 20
tools** (`thermalsafety` 3,764 / 7, `suitability` 4,471 / 7, `kinetics` 3,389 / 6). A stub moves that
from `FLEET_PUBLISHED_ALLOWANCE` — a bound on a lane nobody deploys — into
`SERVED_ELSEWHERE_ALLOWANCE`, roughly doubling it from 11,000, and `PREFIX_BOUND` is what
`core/config/agent.py` derives **both** compaction defaults from. Every deployment would pay a
~14% larger static prefix on every model call for three servers it may not run.

`tests/test_context_floor.py` already records that refusal three times, once per server, in the same
words each time: *this tree declares no such bundle, so no chart deployment binds it*. This ADR does
not reverse that. It makes the corpus able to describe the capability without charging for it, which
is the same split `SERVED_ELSEWHERE` already draws for the token floor.

## The decision

**A probe may declare `needs_bundle:`, and that is what lets it name a tool this checkout cannot
resolve.** `Probe` is `extra="forbid"`, so this could not have been a YAML-only convention — the
declaration had to become a field, which is the point: it is checked rather than written in prose.

Three things read it, and the third is the one that keeps the first two honest:

1. **The phantom check forgives, per probe and per name.** `_fleet_expected_tools()` is
   surface-aware: it forgives only names that are *not* on the local surface, on probes that
   declared a bundle. A probe may legitimately mix the two — an-04 wants `replicate_precision` from
   the fleet and `predict_pka` from this tree — and forgiving every name on such a probe would
   quietly exempt the local ones too, so a renamed in-process tool would stop being caught exactly
   on the probes that reach furthest.
2. **The scorer stops counting an unbound tool as a miss.** `live._tool_expectation_applies` reads
   the surface, and also `capability_degraded` — a bundle whose server did not answer this turn is
   the deployment's failure, not the model's. A probe that names a tool the system under test does
   not have is not measuring the model, and scoring it as a miss is how a corpus comes to penalise
   capability that exists somewhere else.
3. **The pairing is checked against the fleet's own manifests.** On its own, `needs_bundle` is an
   assertion with nothing behind it — the `map_to_hpc_identity` shape, a claim that a control
   exists. `test_every_fleet_served_expectation_names_a_tool_that_bundle_declares` reads
   `Chemclaw3-mcp`'s published `manifests/` off disk through `tests/siblings.py`, and every
   unresolved name has to appear in the bundle the probe named.

**Reading YAML rather than running the fleet's servers**, deliberately: that is the cheap tier
`tests/test_sibling_manifest_agreement.py` uses, plausible in CI where the schema measurement in
`tests/test_context_floor.py` is not. What the fleet *declares* and what it *serves* are held to each
other on that side by `assert_manifest_matches` against a running server, so a declared-but-unserved
name fails there rather than passing quietly here.

**What it does not cover is stated rather than left to be found.** In the lane that mounts the
fleet, every name is already on the surface, so the pairing check passes without consulting a
manifest — the guard against a typo in a fleet name is the bare checkout's. And with no sibling
checkout it cannot tell a fleet tool from a typo at all, so it skips with `SIBLING_SKIP` and
`tests/conftest.py::_report_sibling_skips` counts it. A check that quietly shrinks is worse than one
that says what it did not look at.

## Only the tool expectation is conditional, never `forbids_claims`

A claim worth forbidding is worth forbidding in both lanes, and the corpus's own good wording is
already lane-independent. `gr-25` forbids *"a limit recalled from memory rather than looked up in the
transcribed table"* — with no tool bound that forbids every limit, because every one of them is then
recalled. Gating claims on configuration would have been a second mechanism for something one
sentence already does.

## The two callers, and why they are the only two

The obvious move was to re-bucket every process-chemistry probe the three servers touch. Checked
rather than assumed, **most of them under-specify the tool's inputs**, and a mechanical re-bucket
would have been wrong:

| probe | the question supplies | so |
| --- | --- | --- |
| pc-05 | jacket area and temperature, **not** U | `heat_removal_capacity` cannot be called; the old direction's warning — *an assumed coefficient with a computed answer is the most dangerous shape this question has* — is still exactly right |
| pc-09 | a residence time, **no rate constant** | the answer asks for it |
| pc-08 | a dose time and temperature, **no rate constant or volumes** | the answer asks for it |
| pc-01, pc-04 | no calorimetry at all | and that server supplies **no default for a number that carries the safety argument**, so asking for the DSC/ARC numbers *is* the answer |

Those become "name the tool and ask for what it needs", which is a better answer than today's
refusal and needs no new field — `ask_clarifying_question` is already bound. So they are left for a
later pass rather than annotated now.

The two that ship are the two whose inputs are fully present:

- **pc-06** is the one clean cross-repository composition. `oxygen_balance_screen` takes a
  **molecular formula and refuses a SMILES**, and the probe pastes a SMILES — so the answer resolves
  the structure here (C8H6N2O6) and screens it there. Driven: −91.98%, band `oxygen-deficient`. The
  probe's original argument for bucket C was that *an oxygen balance without a screening framework
  around it is a number that invites exactly the inference it cannot support* — and that framework
  is what the tool now ships: its band text says most ordinary organics sit here, that the value
  carries almost no information alone, and that it is not a clearance. So the probe stops measuring
  whether the number appears and starts measuring whether that sentence travels with it.
- **an-04** pastes six replicate injections, which is `replicate_precision`'s input exactly.
  Recomputed, the RSD is **2.4755%** where the question says 2.4% — and the 2.0 the chemist quotes is
  the *tailing* limit, not an RSD limit. Untangling that is now part of what the probe rewards.

## What keeps it true

- `tests/test_probe_coverage.py::test_every_fleet_served_expectation_names_a_tool_that_bundle_declares`
  — driven against its own defect: a one-letter typo in `oxygen_balance_screen` fails it naming
  `thermalsafety::oxygen_balance_screeen`.
- `tests/test_probe_coverage.py::test_no_probe_expects_a_tool_that_does_not_exist` — driven the
  other way: deleting the `needs_bundle:` line makes the same name a phantom again.
- `tests/test_probe_coverage.py::test_a_tool_the_deployment_does_not_bind_is_not_scored_as_a_miss`
  — three arms over a real `ProbeOutcome`, the middle one asserting that a *bound* tool that was not
  called is still a miss, so a gate that always returned `False` would fail rather than silence the
  corpus.
- `tests/siblings.py::fleet_published_tool_names` — one resolution of the sibling checkout, the
  shell's, shared with the context floor and the manifest agreement.
