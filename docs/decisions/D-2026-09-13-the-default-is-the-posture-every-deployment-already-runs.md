# D-2026-09-13-the-default-is-the-posture-every-deployment-already-runs — the harness and its plan gate ship on

**Status:** accepted · **Date:** 2026-09-13

`harness_enabled` now defaults to `True`. The plan/execute harness and its approval gate are the
shipped posture, `.env.example` says so, and `deploy/helm/chemclaw/values.yaml`'s line is no longer
what turns them on.

## The finding

`core/config/agent.py` shipped `harness_enabled: bool = False`, argued as *"off by default so the
single-turn agent stays the safe fallback"*. `.env.example` repeated it. The Helm chart set
`CHEMCLAW_HARNESS_ENABLED: "true"` with `CHEMCLAW_HARNESS_AUTONOMY: "plan_only"`.

So the supervised posture was the one every supported deployment ran, and the unsupervised one was
the only one any test measured.

**Three merged positions already said the default was on the wrong side, and none of them moved it.**

- `D-2026-09-06-the-write-gate-is-three-names-and-the-plan-gate-carries-the-rest` declined to widen
  `DEFAULT_WRITE_TOOL_GATES` and named this gate as what covers the 29 write tools left outside it:
  *"the shipped chart sets `CHEMCLAW_HARNESS_ENABLED=true` / `CHEMCLAW_HARNESS_AUTONOMY=plan_only`,
  so a human approves the plan before any of them runs."* With the flag off that cover is absent,
  and the ADR says so in the same paragraph — the residual it states is a role-less authenticated
  user reaching a durable launcher with no RBAC underneath.
- `Settings._check` refuses `entra_required` with the flag off under `plan_only`, in as many words:
  the gate *"is not attached at all and a turn can start state-changing work with nothing to approve
  it"*. The config already classified this pairing as an enforcement hole; it only refused it for
  the deployment that had separately declared it was in production.
- D-152 §3 found the consequence empirically. Because the chart shipped `true` while *"the code
  default and every test run `false`"*, the production agent-construction path had never met a live
  model, and the first turn under the shipped configuration crashed before reaching it. That review
  added a live smoke test through the CLI and left the disagreement itself standing.

The capability audit of 2026-09-13 restated the disagreement as a finding about the *test suite* —
the ratchet, the probe corpus and every offline assertion measure a graph shape production does not
run. That is true and it is the smaller half. The larger half is that the default was the less safe
posture of the two.

## The decision

The default is the chart's. A deployment that wants unsupervised turns states
`CHEMCLAW_HARNESS_AUTONOMY=execute`, which keeps the todo list and drops the gate.

**The opt-out is that knob and not `harness_enabled`, stated deliberately.** Turning the harness off
drops the *plan* along with the gate, and a deployment asking for less supervision is not asking for
less planning. This also gives `harness_autonomy=execute` a meaning it did not have before: while
the harness defaulted off, `autonomy_for` was read nowhere but `gate_applies`, which was already
`False` — so the opt-out changed no behaviour at all and bought only visibility in a values diff.
It is now the real thing it always claimed to be.

**A profile still overrides in both directions** (`plan_gate.harness_enabled_for`). That is why
`data/profiles/computation.yaml` needed no global flag to get the harness its own comment argues
for, and why it measured **+0 tokens** across this change while every other profile moved by 1,862.
Its comment — *"Not globally, and not at a higher autonomy"* — read as a contradiction of the chart
and is not one: it argues against a higher *autonomy*, and this changes neither the autonomy nor
that profile.

## What it costs, measured rather than estimated

Both postures measured in one process against the observed system message, per registered profile:

| | harness off | harness on | delta |
| --- | --- | --- | --- |
| `default` | 64,907 | 66,769 | +1,862 |
| `design`, `evidence`, `property-lookup`, `reporting` | — | — | +1,862 |
| `safety` | 5,028 | 6,891 | +1,863 |
| `computation` | 30,150 | 30,150 | **+0** |

Decomposed on `default`: **+1,372** for `tool:write_todos` and **+490** for
`prompt:middleware-sections`. Only `default` was near enough to its ceiling to matter — over the old
65,500 by 1,269.

`CEILINGS["__default__"]` rises 65,500 → **67,500**, and the price is paid where
`tests/test_context_floor.py` says it always lands: `agent_tool_result_clear_trigger` rises 2,000
with it and keeps its allowance whole because nothing bounds it from above, while
`agent_context_token_budget` cannot follow — it is derived downwards from the 128k window. So the
thread allowance falls **42,500 → 40,500, 4.7% of the thread**. Wave 13 paid 500 here and called it
1.16%; this is four times that for one middleware's schema, which is worth stating plainly.

**Nothing was narrowed to pay for it, deliberately.** The narrowing this wants is the
`default`-profile allow-list, worth a measured −5,787 tokens, and it is blocked on a live lane that
can show every probe still reaching its tool. Buying the ceiling now and narrowing later is the
right order; narrowing blind to buy a ceiling is how a cheaper prompt stops finding tools.

## What the flip found

**A helper was silently acquiring the harness.** `helper_profile` builds the helper by copying the
caller's profile, so it inherited `harness_enabled=None` — which resolves to the deployment default.
The helper therefore gained a todo list and a plan gate, and both are pure cost there:

- The gate can never fire. It refuses `side_effecting_call`, and `helper_profile` has just removed
  `side_effecting_tools()` from the surface.
- The plan is written where nobody reads it. A helper's state is discarded when its report crosses
  back, so `write_todos` spends 1,372 tokens of the helper's own prefix on a plan that reaches
  neither the chemist, the caller, nor the gate.

`helper_profile` now sets `harness_enabled=False` explicitly. What caught it was
`tests/test_checkpointer_prune.py`'s bound on a helper namespace at one turn's own writes: the
namespace wrote an eighth checkpoint per turn. That is the cheaper statement of the argument
`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller` makes throughout.

## What keeps it true

- `tests/test_plan_gate.py::test_the_default_deployment_has_the_plan_gate` — asserts both halves of
  `gate_applies` plus the resolved predicate, because the gate is their conjunction and either one
  drifting silently removes it. It asserted the opposite until this ADR.
- `tests/test_config.py::test_enforcing_identity_without_the_plan_gate_is_refused` — the refusal
  still stands, with the pairing now asked for explicitly.
- `tests/test_config.py::test_the_enforced_posture_with_the_gate_attached_constructs` — the chart's
  posture boots, and so does the bare default.
- `tests/test_config.py::test_env_example_ships_the_code_defaults` — the documented file and the
  code cannot disagree again, which is the defect this ADR is about.
- `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling` — every profile
  under 67,500, measured off the wire.
- `tests/test_compaction.py::test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims`
  — the 40,500 allowance, asserted rather than described.
- `tests/test_langgraph_agent.py::test_every_in_process_tool_reaches_the_graph_unchanged` and
  `::test_a_profile_narrows_the_graph_surface` — both union the harness surface through
  `harness_enabled_for` and `harness_tool_names()` rather than naming `write_todos`, so a posture
  change or an upstream rename moves the assertion instead of staling it.
- `tests/test_checkpointer_prune.py::test_every_namespace_of_a_thread_is_bounded_and_not_only_the_root`
  — bounds a helper namespace at one turn's writes, which is what found the inherited harness.
