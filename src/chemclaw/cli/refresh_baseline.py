"""Regenerate `data/evals/baseline.json` from a real scoring run (REV-5, D-136).

`chemclaw.evals.baseline.save_baseline` existed with **no caller anywhere in the repository**, so
the
committed baseline was hand-maintained. That is how it came to be missing `retrieval_recall` and
`retrieval_precision` — the two metrics that actually run a retriever, and the only ones whose
drift would show a retrieval regression. `detect_drift` iterates the *baseline*, not the run, so
their absence gave them not a weak signal but no signal at all: collapsing both to 0.0 produced no
alert.

A generator rather than a hand-edited file, because the failure mode was drift between what the
case-set scores and what the baseline pins, and only a generator makes those the same thing by
construction. Run it after a deliberate, reviewed change in scores — never to make a red drift
check go green, which would be pinning the regression as the new normal.

**The version is a flag, for the reason the harness already gives for its own.** It used to be a
second *positional* defaulting to "unversioned", so `make eval-baseline` — which passed neither
argument — wrote a baseline stamped "unversioned" while `make eval-baseline-check` asked for
`$(EVAL_CASE_SET_VERSION)`. The check then refused to compare them, correctly, and the generated
baseline failed the very check it was generated for. Nobody saw it because regenerating is rare;
adding a case is what makes you do it. A positional cannot be skipped, so passing the version meant
also restating the case directory and silently defeating its `CHEMCLAW_EVAL_CASE_DIR` override —
which is exactly why `evals.harness` made its version an option too.
"""

import argparse

from chemclaw.core.config import settings
from chemclaw.evals.baseline import Baseline, aggregate_metrics, save_baseline
from chemclaw.evals.harness import load_eval_cases, run_eval


def main() -> int:
    """Score the case-set and write the aggregate of every metric it produced."""
    parser = argparse.ArgumentParser(
        prog="chemclaw.cli.refresh_baseline",
        description="Regenerate data/evals/baseline.json from a real scoring run.",
    )
    parser.add_argument("case_dir", nargs="?", default=settings.eval_case_dir)
    parser.add_argument(
        "--case-set-version",
        default="unversioned",
        help="the case-set version to stamp; must match what `--baseline` will be checked against",
    )
    args = parser.parse_args()
    case_dir, version = args.case_dir, args.case_set_version
    report = run_eval(load_eval_cases(case_dir), version)
    metrics = aggregate_metrics(report)
    save_baseline(Baseline(case_set_version=version, metrics=metrics), settings.eval_baseline_path)
    print(f"wrote {settings.eval_baseline_path} with {len(metrics)} metric(s):")
    for name in sorted(metrics):
        print(f"  {name:24s} {metrics[name]:.6g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
