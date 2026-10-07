"""Regenerate `data/evals/baseline.json` from a real scoring run.

Generated rather than hand-edited so the baseline always pins every metric the case set scores —
`detect_drift` iterates the baseline, so a missing metric gets no drift signal at all. Run it after
a deliberate, reviewed score change, never to turn a red drift check green.

The case-set version is an option (not a positional) so `make eval-baseline` stamps the same
version `make eval-baseline-check` asks for, and the case-directory override still applies.
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
