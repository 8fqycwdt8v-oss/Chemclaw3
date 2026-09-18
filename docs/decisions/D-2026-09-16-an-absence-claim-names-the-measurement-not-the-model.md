# D-2026-09-16-an-absence-claim-names-the-measurement-not-the-model — three fleet servers shipped and seventeen bucket-C markers went on denying them, while op-22 denied a tool this tree binds on every turn

**Status:** accepted · **Date:** 2026-09-16 · **Commit:** the process-chemistry header rewritten,
seventeen `asserts_absent` markers narrowed across four probe files, op-22 re-bucketed C → B with
`draft_experiment_protocol`, two new guards in `tests/test_probe_coverage.py`, and the stale
figures deleted from `test_every_fleet_served_expectation_names_a_tool_that_bundle_declares`.

Follows `D-2026-09-16-an-absence-claim-that-names-no-capability-cannot-be-refuted`, which made the
claim a field. This is what reading the field against a fleet that had moved underneath it found.

## What was measured, before anything was changed

`Chemclaw3-mcp/manifests/` on 2026-09-16 publishes `thermalsafety` (7 tools), `kinetics` (6) and
`unitops` (7), all three marked **built** in that repository's `MODULES.md`. `unitops` merged on
2026-09-16 — the same day as the probe file whose markers deny it — and `kinetics` on 2026-09-15.

Against that, a bucket-C marker in `data/evals/probes/process-chemistry.yaml` said in the present
tense: *no reactor model of any kind*, *no distillation, vapour-liquid equilibrium or stage-sizing
model*, *no mixing, agitation or blend-time correlation*, *no filtration or cake model*, *no
solubility curve, metastable zone or crystallisation model*, *no kinetics engine*, *nothing that
derives an onset or a time to maximum rate*, *no heat-transfer model*. Every one of those names a
tool the fleet ships.

**And the header above them contradicted itself in three directions at once.** It opened by
recording that `thermalsafety` had shipped and listing its tools; it then said the denial below was
*conditional on the lane*; and it then stated, under a heading promising what does not exist, that
there is *no adiabatic temperature rise, MTSR, TMR_ad … no kinetics … no unit-operation sizing — no
distillation, crystallisation, filtration, drying or mixing models*. That paragraph is the most
likely cause of the seventeen markers below it: it is what a probe author reads before writing one.

## The finding that decides the fix

The obvious repair is to re-bucket every affected probe with `needs_bundle:`, the way pc-06 already
carries `oxygen_balance_screen`. **Read against the servers' real signatures, that is wrong for
sixteen of the seventeen**, and the reason is a property of the fleet rather than of the corpus.

Every tool on all three servers takes a number a person *measured* as a required argument, and
`unitops` says so about itself: it holds no data at all — no vessel register, no VLE table, no
solubility curve, no cake resistance, no impeller catalogue — so each tool refuses to default the
number its answer is made of. Extracted from the three `tools.py` files, the required arguments
include `overall_heat_transfer_coefficient_w_per_m2_k`, `zwietering_constant`, `power_number`,
`relative_volatility`, `solubility_hot_kg_per_kg_solvent`, `specific_cake_resistance_m_per_kg`,
`rate_constant`, `heat_release_rate_w_per_kg` and `heat_of_reaction_kj_per_mol`.

**None of the probe questions supplies one of those, and this system holds no register any of them
could be looked up in.** pc-12 gives two liquid volumes and asks for a P/V; pc-13 names a 30-inch
Nutsche and asks for a filtration time; pc-11 gives two temperatures and a solvent and asks for a
crystallisation yield; pc-09 gives a residence time and asks for a conversion. So the refusal
survives the fleet being mounted — for a *different reason* than the one the corpus was giving.

That is the decision: **an honest absence claim in this section names the missing measurement, not
the missing model.** It is the shape pc-05 and an-05/an-06 already had (*"no coefficient for this
reactor, and the question supplies none"*; *"nothing PREDICTS a retention time"*), generalised. It
is lane-independent by construction, which is what
`D-2026-09-16-an-absence-claim-that-names-no-capability-cannot-be-refuted` asks of a claim, and it
is a *stronger* probe: it says which number would make the question answerable, so a good answer can
be told from a refusal that merely stops.

`oxygen_balance_screen` is the single tool on those servers whose only input is derivable from a
structure, and pc-06 is correspondingly the single probe that carries `needs_bundle:`. The
exception and the rule have the same cause.

One marker was narrowed rather than re-argued: pc-07's *"no kinetics engine and no regression
surface"* keeps its second half and loses its first, because `kinetics` ships **no** fitting —
"nothing here fits anything" is its own manifest description, its module docstring and its
catalogue row, stated there as a decision rather than a shortfall.

## op-22 is the other direction, and it needed no sibling at all

`data/evals/probes/optimization.yaml` op-22 asserted *no plate-layout, well-volume or
stock-concentration tooling* and forbade *a well coordinate assigned to a condition*. Measured on
this commit with no bundle mounted and no checkout of anything: `available_tool_names()` binds
`draft_experiment_protocol`, which takes `plate_format: int`; `protocols/layout.py::place()` returns
`Well(label, row, column, arm_id, run_order)` per arm over five plate shapes; `protocols/render.py`
puts `well=well.label` in every run-sheet row; and `protocols/export.py` emits that row set as a CSV
whose columns open `arm_id, well, run_order`.

That is the an-28 defect exactly — a probe scoring the correct answer as a fabrication — in a file
`D-2026-09-15-a-probe-that-forbids-the-answer-a-bound-tool-serves-measures-nothing` did not reach,
and it is worse than the fleet cases because no configuration makes it true. op-22 is now bucket B
with `draft_experiment_protocol` in `expects_tools`, and the two thirds of its ask that *are* absent
— a liquid-handler interface, and any stock concentration or dispense volume — stay forbidden, as
`rx-18` does for the half of its question that does not exist. `pl-22` keeps bucket C and loses only
*plate-layout generator* from its marker: it asks for machine instructions, which genuinely have no
producer here.

## The two figures in the test file were wrong, and the sentence around them was wrong in kind

`test_every_fleet_served_expectation_names_a_tool_that_bundle_declares` claimed the fleet lane
"is not unguarded — it is the lane where `test_every_agent_callable_tool_is_probed_or_exempt` has
121 tools to account for instead of 114". Measured on this commit: the bare surface is neither
number, the fleet surface is neither number, and — the part that matters — that test does not
**pass** with the fleet mounted, because the corpus is written against the surface this repository
declares and every fleet tool no probe names is unprobed there. Two stale figures were dressing a
claim that was false in kind, which is `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`.

No figure replaces them. What each lane binds is `available_tool_names()` under that lane's
`CHEMCLAW_CONNECTORS_DIR`, and the only number worth reading is one an assertion computes.

**The adjacent claim in that docstring was checked and is true**, and is kept: with the fleet
mounted every `needs_bundle:` expectation is on the surface, `unresolved` is empty, and the test
returns before reading a manifest. So the typo guard is the **bare** lane's, which makes the bare
lane the one that has to stay green.

## What this does not change

- **No probe was re-bucketed to make a test green.** Every test in
  `tests/test_probe_coverage.py` passed before this commit in the lane CI runs, and the sixteen
  narrowed markers keep bucket C. op-22 moved because the tool is bound, not because anything
  failed.
- **`run_python` on pc-07 and ws-12 stays.** It fails in a lane that mounts `pyexec`, and
  `D-2026-09-16-an-absence-claim-that-names-no-capability-cannot-be-refuted` argues that asymmetry
  shut on purpose: in that lane those two probes *are* stale, and going red is the correct report.
  What was wrong was only the parenthesis in `NEAR_MISS_RATIO`'s comment saying no deployment binds
  it; that is corrected in place.
- **The header still records the fleet's status with a date rather than in the present tense**,
  because it is a fact about a repository this one cannot read on any run — the convention that
  paragraph already used for `next`/`proposed`, now applied to `built` as well.
- **Nothing scans `direction:` or `forbids_claims` for bound tool names.** pc-11 forbids *"a call to
  predict_solubility presented as answering the IPA question"* and that is correct wording; a scan
  there would fire on the corpus's best sentences. The stale prose in those fields was found by
  reading, and it is fixed by reading.

## What keeps it true

- `tests/test_probe_coverage.py::test_no_probe_asserts_a_capability_the_agent_surface_serves` — the
  markers are prose, so this cannot read them; what it does hold is that no *name* in them resolves
  to a bound tool, which is the arm that keeps the narrowed wording from laundering a claim back in.
- `tests/test_probe_coverage.py::test_no_probe_names_one_tool_in_both_expects_tools_and_asserts_absent`
  — new, and the guard op-22 now needs: a probe that expects `draft_experiment_protocol` may not
  also deny it. Lane-independent, because it consults no surface.
- `tests/test_probe_coverage.py::test_an_absence_claim_one_edit_from_a_bound_tool_is_read_as_the_typo_it_is`
  — extended to the marker arm, closing the gap where `NO-TOOL nothing like ich_impurity_limits
  here` escaped both the near-miss check and the exact-name scan at once.
- `tests/test_probe_coverage.py::test_a_near_miss_is_caught_on_the_marker_arm_as_well_as_the_bare_one`
  — new, and driven against its own defect on both arms, so the extension above is watched refusing.
- `tests/test_probe_coverage.py::test_no_tools_only_coverage_is_a_question_the_surface_cannot_answer`
  — what op-22's re-bucket had to satisfy from the other side: a tool named only by bucket-C probes
  is covered on paper and never called.
- `tests/test_live_probes.py::test_a_bucket_c_probe_expects_no_tool` — the existing rule op-22 could
  not have stayed bucket C under, since it names a tool now.
- `tests/test_probe_coverage.py::test_every_fleet_served_expectation_names_a_tool_that_bundle_declares`
  — pc-06's `needs_bundle: thermalsafety` is still checked against the fleet's own manifest, and
  still skips loudly without a checkout.
