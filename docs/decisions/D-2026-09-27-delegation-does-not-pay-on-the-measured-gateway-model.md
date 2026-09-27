# D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model — the delegation experiment, run

**Status:** accepted · **Date:** 2026-09-27 · Closes the `BACKLOG.md` row *"The delegation
experiment: run it against a gateway"* (issues #359, #472). Nothing it measured is removed.

## Context

`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller` shipped the helper roster on by
default and left one question open: whether delegating pays. `make live-delegation`
(`src/chemclaw/evals/delegation_run.py`) is the instrument — eight many-source or many-entity tasks
in `data/evals/probes/delegation.yaml`, three repeats per (task, arm), a judge verdict on
`VERDICT_SCORES`' scale, billed tokens off `turn_costs`, wall clock, and whether each repeat
delegated, each arm reported against the `no-helper` baseline. It had never run against a model.

On 2026-09-27 it ran three times through an OpenRouter gateway, agent and judge both
`deepseek/deepseek-v4-pro`. The reports are `devenv/live-state/p2-deleg1..3/summary.md` in the
operator's checkout (outside this repository); every figure below is copied from them.

| arm (against `no-helper`) | delegated | quality: helped / hurt / no effect (net) | median token ratio | median wall-clock ratio |
|---|---:|---|---:|---:|
| `helper` — on the caller's own model | 3/24 | 2 / 2 / 4 (+1) | 1.091 | 1.219 |
| `helper-routed` — `model_routes["helper"]` on a smaller model | 3/24 | 0 / 3 / 4 (−2.5) | 1.297 | 1.041 |
| `peer` — `agent_peer_roster` naming a second profile | 0/24 handed off | 2 / 3 / 2 (−1) | 1.015 | 1.055 |

The smaller model on the routed arm was DeepSeek V4 Flash by the run's launch configuration; the
report records only the posture (`CHEMCLAW_MODEL_ROUTES='{"helper": "<a smaller model>"}'`). One
repeat was ungraded on each of the second and third runs (`no-helper/dl-07#1`, `no-helper/dl-04#3`),
so dl-07 and dl-04 respectively fall out of those comparisons as incomplete.

## What the numbers can and cannot carry

- **Three of the eight tasks measured nothing about delegation.** dl-02, dl-05 and dl-08 said "these
  twenty intermediates", "these eight substrates", "these twelve intermediates" and listed none, so
  all 54 of their repeats, in every arm, asked which ones after one to three tool calls (read off
  the run's transcripts). Their "no effect"
  and their ±1 swings are a judge scoring a clarifying question. They are fixed in the same change
  as this ADR; the numbers above are from before the fix.
- **The baseline moves by itself.** The `no-helper` arm, which is the same configuration in all three
  runs, scored dl-01 at −1.0, −1.0 and 0.0 and dl-06 at +1.0 every time: a one-point swing on a
  task is inside what re-running the baseline produces, and most of the "helped" and "hurt" verdicts
  above are one point on one task.
- **The judge is the model under test.** An unset `live-probe-judge` route makes the run
  self-grading, which `evals/live_judge.judge_model` names in every report; it was unset.
- **The peer arm's zero is a behavioural observation with no surface record behind it.** Nothing
  logged whether a `transfer_to_<peer>` tool was bound on those turns, so "the model never chose to
  hand off" and "the roster never compiled" were the same observation. The same change adds the
  line that separates them (`agent/langgraph_agent._log_bound_surface`, INFO per compiled graph).

What survives those caveats is narrow and consistent across the three runs: **on this model the
agent rarely delegates when it may (6 of 48 helper-arm repeats, 0 of 24 peer repeats), delegating
does not improve the answer by more than the baseline's own run-to-run variation, and it always
costs more** — every arm's median token ratio is above 1.0, and the routed arm, whose premise was
that a cheaper helper model would make delegation cheaper, was the most expensive and the only one
with no task it helped.

## Decision

**Delegation does not pay on the measured gateway model, and it stays shipped as it is.**

- `task` stays on every turn — it is not a choice: `SubAgentMiddleware` is in `create_deep_agent`'s
  `_REQUIRED_MIDDLEWARE` and `_apply_excluded_middleware` raises rather than let a profile strip it.
- The named roster (`agent_helper_roster`) stays on by default. The measurement shows the model
  seldom uses it and that using it costs a median ~9% more tokens; it does not show harm large
  enough to separate from the baseline's variation, and switching it off would be a second decision
  taken on the same underpowered data.
- The peer roster (`agent_peer_roster`) stays off by default, as `D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it`
  shipped it.

**Declined: any further investment in delegation on this model** — a routed helper by default, a
prompt that pushes the model to delegate, a larger roster, a peer roster by default. Each would be
bought with the measurement above, and the measurement says the premise did not hold here.

Revisit when: the deployment's gateway model changes to a different or stronger one (the value of
`CHEMCLAW_LLM_MODEL` a release sets differs from `deepseek/deepseek-v4-pro`), or `make
live-delegation` is re-run over the corrected corpus — dl-02, dl-05 and dl-08 now name their
compounds — with a judge routed to a model other than the one under test, and at least one arm's
quality moves by more than the `no-helper` arm varies against itself between runs.

## Consequences

- `CLAUDE.md`'s "whether delegation pays" is answered by reference to this record rather than left
  open.
- The instrument stays: `evals/delegation.py`, `evals/delegation_run.py`, the three arm profiles in
  `data/evals/profiles/` and the corpus. Re-running it is the trigger above, and it costs a
  credential and three runs.
