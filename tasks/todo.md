# A probe corpus that can name a tool the fleet serves

Closes `docs/planning/BACKLOG.md`'s row *"A bundle declared only in the fleet reaches an agent
surface with no probe covering it"*.

## The problem, stated as the two tests that disagree

`tests/test_probe_coverage.py` holds the corpus against `available_tool_names()`, which reads
`CHEMCLAW_CONNECTORS_DIR`. So the corpus is caught between two assertions pointing in opposite
environment directions:

| test | fails when |
| --- | --- |
| `test_every_agent_callable_tool_is_probed_or_exempt` (L88) | the fleet **is** mounted — 121 tools, 7 unprobed |
| `test_no_probe_expects_a_tool_that_does_not_exist` (L99) | the fleet is **not** mounted and a probe names one of its tools |

There is no bucket a configuration-dependent probe can sit in today, so three servers shipped this
session (`thermalsafety`, `suitability`, `kinetics`) are unmeasurable by the corpus in every lane.

## Why not the other fix

Declaring the three as in-tree bundles was measured and rejected: 11,624 tokens over 20 tools
(3,764 + 4,471 + 3,389) would move from `FLEET_PUBLISHED_ALLOWANCE` — a bound on a lane nobody
deploys — into `PREFIX_BOUND`, which both compaction defaults derive from, for every deployment.
`tests/test_context_floor.py` records that refusal three times already.

## Plan

- [x] 1. `tests/siblings.py` — `fleet_published_tool_names(root)`: parse each published manifest's
      `endpoint.tools`. The cheap tier (`sibling_root`, YAML off disk), not `sibling_python`.
- [x] 2. `src/chemclaw/evals/probe.py` — `Probe.needs_bundle: str | None`. `Probe` is
      `extra="forbid"`, so a YAML-only change is impossible; this is the declaration that a probe's
      tool expectations are conditional on a bundle being bound.
- [x] 3. `src/chemclaw/evals/live.py` — apply `expects_tools` only when `needs_bundle` is bound.
      A probe whose bundle is absent degrades to its bucket-C form: no tool expected.
- [x] 4. `tests/test_probe_coverage.py` — a probe may name a tool off the local surface **iff** it
      declares `needs_bundle`; verify the pairing against the fleet's own manifests when a sibling
      checkout exists, and skip with `SIBLING_SKIP` (counted by `conftest._report_sibling_skips`)
      when it does not.
- [x] 5. Re-bucket the probes that this unblocks: pc-01, pc-04, pc-05, pc-06, pc-08, pc-09.
- [x] 6. ADR + delete the BACKLOG row + `make lint type test`.

## What the probes actually need, checked rather than assumed

Most of these **under-specify the tool's inputs**, so a mechanical re-bucket would be wrong:

| probe | tool | the question supplies |
| --- | --- | --- |
| pc-06 | `oxygen_balance_screen` | a SMILES — and that tool takes a **molecular formula and refuses a SMILES**, so the answer composes `resolve_compound` (declared here) with it. `CC(=O)Oc1ccc([N+](=O)[O-])cc1[N+](=O)[O-]` is C8H6N2O6. The one clean cross-repo composition in the set. |
| pc-05 | `heat_removal_capacity` | area and jacket temperature, **not** U — so the answer states the assumption or asks |
| pc-09 | `continuous_reactor_conversion` | a residence time, **no rate constant** |
| pc-08 | `semibatch_accumulation_profile` | a dose time and temperature, **no rate constant or volumes** |
| pc-01 / pc-04 | `adiabatic_temperature_rise`, `mtsr`, `tmr_ad` | no calorimetry at all — and that server supplies **no default for a number that carries the safety argument**, so asking for the DSC/ARC numbers is the correct answer |

So these are bucket **B**, and `expects_tools` is any-of (`live.py:540`), which lets "called the tool"
and "asked for what the tool needs" both count where that is genuinely right.

## The property that makes the claims lane-independent

`gr-25`'s wording — *"a limit recalled from memory rather than looked up"* — is correct in **both**
lanes: with no tool bound, any number is necessarily recalled and so already forbidden. So only the
tool *expectation* needs gating, never `forbids_claims`. Same for an alert.

## Review

**Done, and the shape changed twice under measurement.**

1. `tests/siblings.py::fleet_published_tool_names` — the cheap tier, YAML off a shallow clone.
   Driven: 48 tools across 8 published bundles.
2. `Probe.needs_bundle` — a field, because `extra="forbid"` made a YAML convention impossible, which
   is the right outcome: the declaration is checked rather than written in prose.
3. `live._tool_expectation_applies` — reads the surface *and* `capability_degraded`. The `evals -> agent`
   import edge already existed for the live judge's TLS clients; its recorded reason said that was
   the only thing in `evals` needing it, so the reason was extended rather than a second edge added.
4. `tests/test_probe_coverage.py` — the phantom check forgives per name and only off-surface.
5. pc-06 and an-04.
6. ADR, ledger, BACKLOG row deleted, `make type` clean.

**Two things I had wrong in the first pass and fixed under measurement.**

*The helper was not surface-aware.* `_fleet_expected_tools()` first returned every name on a
`needs_bundle:` probe, which would have exempted an-04's `predict_pka` — an in-process tool — from
the phantom check. A probe may legitimately mix fleet and local tools, so the forgiveness has to be
per name. The first draft also carried a test asserting no `needs_bundle:` probe may name a local
tool, which is the same mistake as an assertion: it would have forbidden an-04 outright. Deleted.

*The re-bucket was going to be mechanical and would have been wrong.* Five of the seven probes the
three servers touch **under-specify the tool's inputs** — pc-05 gives no U, pc-08/pc-09 no rate
constant, pc-01/pc-04 no calorimetry at all. Expecting the tool there would have rewarded the exact
shape pc-05's old direction warns about: *an assumed coefficient with a computed answer is the most
dangerous shape this question has*. Only pc-06 and an-04 supply what their tool takes, so only those
two ship.

**Both guards driven against their own defect** rather than asserted: a one-letter typo in
`oxygen_balance_screen` fails the pairing test naming `thermalsafety::oxygen_balance_screeen`, and
deleting the `needs_bundle:` line makes the same name a phantom again. The scoring test has a middle
arm asserting a *bound* uncalled tool is still a miss, so a gate stuck at `False` fails rather than
silently stops measuring.

**The gate found a third copy of the invariant, which is why running it mattered.**
`tests/test_live_probes.py::test_every_expected_tool_in_the_shipped_corpus_exists_on_the_agent_surface`
asserts the same rule over the live runner's own loader, and went red on pc-06. It had already
drifted before this change: `load_probes` does not recurse, so it covered 336 probes to the other's
338. The exemption now has one definition and that file imports it; the loader gap is named rather
than merged away. Driven both ways — deleting pc-06's `needs_bundle:` fails both assertions.

**What this deliberately does not do.** It does not declare the three bundles — measured at 11,624
tokens over 20 tools into `PREFIX_BOUND`, which both compaction defaults derive from — and it does
not touch the five under-specified probes, which want "name the tool, ask for its inputs" and need
no new machinery to say so.
