"""`python -m chemclaw.cli.hypothesis_recovery` — what a tournament's ranking is worth.

Prints the table `D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate` quotes, so the ADR's
numbers are reproducible by a command rather than by reading a test. That ADR asks for exactly this
of itself — "make the trigger executable where you can" — and the figure it turns on, that a
plausible judge leaves the leader wrong more often than right, is the one a reader is most likely
to want to check.

**Needs no credential and no model.** The judge is simulated at a stated accuracy, which is what
makes the ground truth constructed and the null controllable. It measures the *instrument* — Swiss
pairing plus the Bradley-Terry fit — and says nothing about whether a language model judging real
chemistry is accurate. `evals.hypothesis_tournament.backtest_shape` states the measurement that
would settle that, and records that it has never run.
"""

from __future__ import annotations

import argparse

from chemclaw.evals.hypothesis_tournament import backtest_shape, simulate

_ACCURACIES = (0.50, 0.55, 0.65, 0.75, 0.90, 1.00)


def main() -> None:
    """Print recovery against a null control across judge accuracies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", type=int, default=10, help="hypotheses in the field")
    parser.add_argument("--runs", type=int, default=1000, help="tournaments per cell")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    print(f"field={args.field}  runs={args.runs}  seed={args.seed}")
    print(f"{'judge':>6} {'cmps':>5} {'top-1':>7} {'null':>7} {'rho':>8} {'null rho':>9}")
    for accuracy in _ACCURACIES:
        result = simulate(field=args.field, judge_accuracy=accuracy, runs=args.runs, seed=args.seed)
        print(
            f"{result.judge_accuracy:>6.2f} {result.comparisons:>5} {result.top_one:>7.3f} "
            f"{result.null_top_one:>7.3f} {result.spearman:>+8.3f} {result.null_spearman:>+9.3f}"
        )
    print()
    print("A perfect judge recovering top-1 at 1.000 is the instrument working; the row that")
    print("matters is the middle one, where the leader is right less than half the time.")
    print()
    print(f"Not measured here: {backtest_shape()}")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
