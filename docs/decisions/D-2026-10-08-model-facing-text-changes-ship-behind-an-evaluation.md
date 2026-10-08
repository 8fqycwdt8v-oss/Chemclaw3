# D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation — tool descriptions and prompts are edited one batch at a time, and a batch ships only if the eval says it is not worse

**Status:** accepted · **Date:** 2026-10-08

## Context

W1 cut code prose under a rule that the code may not change. It left untouched everything the model
reads: agent-tool docstrings, schema descriptions that reach a tool or response schema, the fleet's
tool descriptions and output schemas, `ModelProse` constants, prompt blocks and `SKILL.md` bodies.
That text was verified byte-identical to `main`, because changing it changes behaviour. Much of it
still carries history, and all of it is paid for in every request's prefix (the `CEILINGS` ceiling
in `tests/test_context_floor.py`).

## Options

1. **Leave model-facing text as it is.** No behaviour risk. The prefix cost and the stale history
   stay.
2. **Edit it like the rest of the prose, in one pass, checked only by the offline evals.** Cheap,
   but the scripted model cannot tell whether a real model still picks the right tool or still
   refuses what it should.
3. **Edit it one batch at a time behind an evaluation fixed in advance.**
   - Offline: `eval-strict`, `eval-baseline-check` and `test_prose_contract`.
   - Live: `live-ab` against a real gateway, with the current text as the control arm.
   - A batch ships only if no metric is worse than the control by more than the control's own
     run-to-run spread.

## Decision

**Option 3, by owner decision.**
- **A batch** is one tool family (one fleet server, or one core tools module, plus its schema
  classes). Prompt blocks and `SKILL.md` bodies come last, each block on its own.
- **Metrics:** tool-selection accuracy, first-call argument validity, refusal correctness, graded
  task success, tokens per turn and turn cost.
- **The noise floor** is measured on the control arm first, as
  `D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model` did.
- **A fleet batch** bumps that connector's `contract_version` minor.
- **The prefix ceiling** is lowered to the measured value after each shipped batch.

The plan is programme items W2.13–W2.17.

## Consequences

- No batch ships without a gateway credential in the environment that runs the live arm.
  Running only the offline evals is not enough.
- Each PR carries its eval table. A batch that fails the rule is reverted or reworded, never shipped
  "close enough".
- The prefix saving goes to the thread budget through `PREFIX_BOUND` and the derived defaults in
  `core/config/agent.py`.

Revisit when: two consecutive batches fail the ship rule on the same metric. That suggests the metric
or the probe corpus, not the text, is what needs work. The batch PRs' eval tables show it.
