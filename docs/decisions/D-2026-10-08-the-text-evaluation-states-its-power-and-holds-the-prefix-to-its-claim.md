# D-2026-10-08-the-text-evaluation-states-its-power-and-holds-the-prefix-to-its-claim — the ship rule keeps the control's spread, needs five runs to ship, says what it can detect, and holds the prefix to what the batch claims

**Status:** accepted · **Date:** 2026-10-08

## Context

`D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation` fixes the rule: a batch ships only
if no metric is worse than the control by more than the control's own run-to-run spread. The
programme plan (W2.14) added two things the decision does not say: at least three runs per arm, and
that the per-request prefix must shrink. Writing the rule as code (`evals/model_text.py`) and
simulating it on normal noise (`rule_power`, seeded; `tests/test_model_text_ship_rule.py` holds the
figures) showed what those two additions cost.

Spread is the control's range. Per metric, with a true regression of 1 and 2 run-to-run standard
deviations (σ), and a neutral edit:

| runs per arm | neutral edit passes a metric | on all six | 1σ regression passes | 2σ regression passes |
| ---: | ---: | ---: | ---: | ---: |
| 3 | 93% | 64% | 71% | 39% |
| 5 | 99% | 95% | 89% | 60% |
| 7 | 100% | 99% | 97% | 76% |
| 10 | 100% | 100% | 99% | 88% |

At three runs a neutral rewrite fails some metric of six about one time in three, so a batch would
be reworded and re-run on noise. Adding runs fixes that, and does **not** make a regression easier
to catch: the range widens with the run count faster than the means' noise narrows. The rule is a
guard against large regressions, and a report that hides that overstates what "not worse" means.

"The prefix must shrink" is stricter than the decision. It blocks a neutral rewording that moves a
few tokens, and a correction that has to add a sentence.

## Options

1. **Keep three runs and a smaller prefix, as the plan has it.** Cheapest. A third of neutral batches
   fail on noise, and the prefix rule rejects corrections.
2. **Five runs minimum, the range floor kept, power stated, the prefix held to the batch's claim.**
   Neutral edits fail about one time in twenty. The rule's weak detection of small regressions is
   printed in every report instead of implied.
3. **Seven to ten runs.** Almost no false failures, and about the same weak detection, at two to
   three times the live spend per batch.
4. **Replace the range by a test of the difference of means** (floor of `s * sqrt(2/n)`).
   Detects a 1σ regression about half the time at five runs, and fails a neutral edit on some metric
   of six about 45% of the time. Unusable with six metrics, and a different rule from the one the
   owner decided.
5. **Drop the prefix rule.** Loses the point of the programme, which is the prefix.

## Decision

**Option 2.**
- The spread stays the control's range, so the merged decision's rule is unchanged.
- **A ship verdict needs at least `SHIP_MINIMUM_RUNS` (5) runs per arm.** Fewer measured runs are
  reported, never shipped ("underpowered"). `--min-runs` can be raised, not lowered; three runs is
  the least a spread exists at, and the pure function accepts it for analysis only.
- **Every report states the power**: per metric the observed worse-by, the noise floor and what
  that floor can see, and once the neutral-failure and detection rates at that run count.
- **The prefix may not grow past a stated tolerance** (100 tokens by default, `--prefix-tolerance`),
  and **a batch that claims a token saving must show one** (`--claims-token-saving`, default off).
- The arms are compared on the probes every run of both completed; an arm that failed more than a
  stated share of them (10% by default) blocks the verdict.
- A live run states its spend, refuses to start over a stated ceiling, and starts only when the
  operator types the planned count back.
- The exit code for "ship" is its own (10); only a live run whose offline gate passed returns it.

## Consequences

- A batch costs five runs per arm, not three, and a ceiling-sized run is refused until someone
  raises the ceiling on purpose.
- The report no longer reads as stronger than the rule is. A reviewer sees that a regression the
  size of the noise floor passes about half the time.
- A neutral rewording and a correction can ship; a saving can only be claimed by showing it.
- The ADR that set the rule is unchanged: this refines how it is run.

## What keeps it true

- `tests/test_model_text_ship_rule.py`: the run floor, the power figures against the simulation,
  the prefix tolerance and claim, and the probe intersection and drop share.
- `tests/test_model_text_eval.py`: the ship exit code, the spend plan and ceiling, and that two
  prefixes measured in different environments are refused.

Revisit when: a live evaluation on a fixed text shows the control's range is not the run-to-run
noise of the candidate arm (the same text, run as both arms, fails a metric more than the figures
above), which `make model-text-eval` can show with the shipped text as both arms.
