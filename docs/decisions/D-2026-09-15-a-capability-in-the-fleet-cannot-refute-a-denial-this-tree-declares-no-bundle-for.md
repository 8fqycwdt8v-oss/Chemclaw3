# D-2026-09-15-a-capability-in-the-fleet-cannot-refute-a-denial-this-tree-declares-no-bundle-for — a capability in the fleet cannot refute a denial this tree declares no bundle for

**Status:** accepted · **Date:** 2026-09-15 · Supersedes nothing. It records what
`cli/validate_prose_contract.py` refused, why the refusal was right, and the rule that follows for
every future edit to the "what this system does not hold" paragraph.

## What happened

`Chemclaw3-mcp` shipped two servers on 2026-09-15: `thermalsafety` (adiabatic temperature rise,
MTSR, TMR_ad, the Stoessel class, jacket heat-removal capacity) and `suitability` (USP <621> system
suitability). The agent's system prompt contains a paragraph of denials, and one of its clauses read:

> no calorimetry, heat- or mass-transfer, mixing or addition-rate model, so a computed reaction
> enthalpy is never a process heat load, **an adiabatic rise, a jacket duty** or a safe addition rate

Two of those four are now things a served fleet computes. The mechanism for exactly this already
exists — `PromptBlock.absent_unless` drops a denial when the graph binds a tool that refutes it, and
two clauses already use it (`screen_genotoxic_alerts`, `ich_impurity_limit`). So the obvious fix was
to cut the refuted half into its own block keyed on `adiabatic_temperature_rise` and
`heat_removal_capacity`.

**`make prose-validate` refused it, and the refusal is the finding:**

> keys on `['adiabatic_temperature_rise', 'heat_removal_capacity']`, which `build_langgraph_agent`
> never binds … so this block would be dropped from every deployment (or, for `absent_unless`, from
> none).

## Why the refusal is right

The two existing keyed clauses work because **this tree declares a `safety` bundle**: there is a
`connectors/safety/` manifest here, so `build_langgraph_agent` can bind `screen_genotoxic_alerts`,
and the denial can therefore drop in a real deployment.

There is no `thermalsafety` bundle in this tree and no `suitability` one. Those servers are reachable
only by a deployment pointing `CHEMCLAW_CONNECTORS_DIR` at the fleet's *own* `manifests/` directory —
which `infra/live/e2e-full-stack/up.sh` does and which no chart deployment does. A block keyed on
their tool names would have been dropped from **no deployment, ever**.

That is `reject_widening` and `map_to_hpc_identity` in a prompt: a control whose condition cannot
occur, kept alive by looking like one that can. This repository has deleted both of those for the
same reason, and the prompt is a worse place for the shape than `src/` is, because nothing about a
system message's *text* makes an unreachable condition visible to a reader.

## What shipped instead

The denial was split by what is true in every lane rather than keyed on what is true in one:

- **Kept unconditional, because it is exact.** That server holds no calorimetry *model*. Every input
  to it is a DSC, ARC or RC1 number a person measured, and it fits, measures and predicts nothing. So
  "no calorimetry, heat- or mass-transfer, mixing or addition-rate model, so a computed reaction
  enthalpy is never a process heat load or a safe addition rate" stays, in every deployment.
- **Dropped rather than keyed.** The "adiabatic rise / jacket duty" consequence is the half a
  mounted fleet makes wrong, and nothing in this tree can tell whether the fleet is mounted.
  Under-claiming a limit is the safe direction: a model told only that there is no calorimetry model
  will still reach a bound tool that computes from numbers it is given, whereas a model told the
  computation is impossible will refuse a tool it holds.

A second clause moved for a related but distinct reason. `estimate_stability_trend` shipped the same
day and is **in-process**, so it is bound on every turn — which means keying "no stability,
shelf-life or batch-trending data" on it would drop the clause from every deployment, and *that*
would be wrong too, because this system genuinely holds no stability data. The distinction is the one
`D-2026-09-15`'s method-store removal already drew: **content versus capability.** The clause now
reads "no stability **study**, shelf-life or batch-trending data" — denying the content, which is
true, in wording a reader cannot take as denying the arithmetic, which is not.

## The rule

**Before keying a `PromptBlock` on a tool name, check that this tree can bind it.** Writing a
capability in `Chemclaw3-mcp` does not make it reachable from here; that takes a manifest in
`connectors/`, and a sibling repository's `manifests/` directory is not this tree's surface.

Three cases, and they take three different edits:

| The refuting tool is… | The edit |
| --- | --- |
| a bundle this tree declares | `absent_unless` — it drops where the bundle is bound |
| in-process, so always bound | rewrite the clause; keying it drops it everywhere, which is a deletion with extra steps |
| served only by the fleet | narrow the clause to what holds in every lane, and drop the rest |

The validator already enforces the first row's precondition. The other two are judgement, which is
why they are written down here.

## The corpora said it too, and were stale

`data/evals/probes/process-chemistry.yaml` exists so the fleet's largest capability gap has an exit
criterion, and its header still called `thermalsafety` `next` in `MODULES.md` the day it shipped as
`built`. `data/evals/probes/analytical.yaml` still said "no stability trending" after
`estimate_stability_trend` landed. Both headers now state the lane-dependence explicitly: re-bucket a
probe from C only for the lane it is run in, and record which — because a C scored against a mounted
fleet, or an A scored against an unmounted one, is a number about the wiring rather than about the
capability.

## What keeps it true

- `cli/validate_prose_contract.py` rule 10, run by `make prose-validate` — refuses a block keyed on a
  name `build_langgraph_agent` cannot bind, in either direction. This is the control that caught it.
- `tests/test_prose_contract.py` — drives two real surfaces, so a clause that should drop and does
  not is visible off the wire rather than from the source.
- `tests/test_context_floor.py::test_the_whole_directory_the_e2e_lane_mounts_is_bounded_too` — the
  other half of the same seam: what the fleet publishes is bounded here even though this tree
  declares no bundle for most of it.
