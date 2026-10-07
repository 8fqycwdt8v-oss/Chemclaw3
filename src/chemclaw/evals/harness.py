"""Eval harness: run a metric set over a versioned case-set → citable report.

Runs each case's named metrics and renders a Markdown report where every row carries its case id and
the metric's provenance. Cases are versioned frontmatter files under `eval_case_dir`, loaded here
rather than through `chemclaw.kg.note`: an eval case is an evaluation payload
(`output`/`reference`), not a graph note, and does not live under `knowledge_dir`.
"""

import argparse
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from pydantic import BaseModel, Field, ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.markdown import MISSING, render_table
from chemclaw.evals.baseline import (
    CaseSetMismatchError,
    compare_to_baseline,
    load_baseline,
    render_comparison,
)
from chemclaw.evals.metric import EvalCase, gated_names, get_metric


class ScoredResult(BaseModel):
    """One metric result tagged with the case it scored (a report row)."""

    case_id: str
    result_metric: str
    value: float
    unit: str | None
    passed: bool | None
    provenance: str


class EvalReport(BaseModel):
    """A scored run over a case-set: reproducible from the same version + metrics."""

    case_set_version: str = Field(min_length=1)
    results: list[ScoredResult]
    # Per case, whether its gates were declared expected-to-pass, so `regressions()` and a
    # serialized report can tell intended failures from real ones.
    expect_pass: dict[str, bool] = Field(default_factory=dict)

    def failed(self) -> list[ScoredResult]:
        """Gated results that did not pass (a regression, treated like a test failure)."""
        return [r for r in self.results if r.passed is False]

    def _demonstrations(self) -> set[str]:
        """Case ids declared `expect_pass: false` — the cases that exist to fail."""
        return {case_id for case_id, expected in self.expect_pass.items() if not expected}

    def regressions(self) -> list[ScoredResult]:
        """Failures that were not supposed to happen — `failed()` minus the demonstration cases.

        What `--strict` gates on: some shipped cases exist to demonstrate a gate firing.
        """
        return [r for r in self.failed() if r.case_id not in self._demonstrations()]

    def gates_no_demonstration_can_fire(self) -> list[str]:
        """Gated metrics that no demonstration case actually fails — gates that cannot go red.

        A gated metric scored only by cases written to pass could stop measuring and nobody would
        see it, so every gated metric owes the case-set one case that fails it. Ungated metrics owe
        nothing. The gated set comes from the registry, not from this run's results, so a metric no
        case scores at all is still caught. Name-sorted.
        """
        demonstrations = self._demonstrations()
        fired = {
            r.result_metric
            for r in self.results
            if r.passed is False and r.case_id in demonstrations
        }
        return sorted(gated_names() - fired)

    def inert_demonstrations(self) -> list[str]:
        """Demonstration cases that no longer fail anything — the other half of `expect_pass`.

        `regressions()` suppresses expected failures, so it cannot see a gate that stopped firing (a
        loosened threshold, a broken metric). So `expect_pass: false` asserts that at least one of
        the case's gated metrics fails — at least one, since a demonstration may carry a passing
        metric beside the failing one. Id-sorted.
        """
        failing = {r.case_id for r in self.results if r.passed is False}
        return sorted(self._demonstrations() - failing)


class EvalCaseError(ChemclawError):
    """A case file could not be read or is not a valid eval case (G4)."""


def run_eval(cases: list[EvalCase], case_set_version: str) -> EvalReport:
    """Score every case by its named metrics into a versioned report.

    A metric failure is re-raised with the case and metric that triggered it, so a bad case names
    itself.
    """
    results: list[ScoredResult] = []
    for case in cases:
        for name in case.metrics:
            try:
                mr = get_metric(name)(case)
            except ValueError as exc:
                # Covers both an unknown metric name and a metric's own MetricError,
                # so either way the failure names the case + metric that caused it.
                raise EvalCaseError(f"case {case.id!r} metric {name!r}: {exc}") from exc
            results.append(
                ScoredResult(
                    case_id=case.id,
                    result_metric=mr.metric,
                    value=mr.value,
                    unit=mr.unit,
                    passed=mr.passed,
                    provenance=mr.provenance,
                )
            )
    return EvalReport(
        case_set_version=case_set_version,
        results=results,
        expect_pass={case.id: case.expect_pass for case in cases},
    )


def load_eval_cases(directory: str) -> list[EvalCase]:
    """Load eval cases from `*.md` frontmatter files under `directory`, id-sorted.

    Each file's frontmatter carries `id`, `metrics`, `output`, and optional `reference` (the body is
    free-form rationale). A malformed file raises `EvalCaseError` naming the path; a missing or
    empty directory also raises, so the gate cannot pass vacuously.
    """
    root = Path(directory)
    if not root.is_dir():
        raise EvalCaseError(f"eval case directory {directory!r} does not exist")
    cases = [_load_case(path) for path in sorted(root.glob("*.md"))]
    if not cases:
        raise EvalCaseError(f"no eval cases found in {directory!r} — empty case-set")
    return cases


def _load_case(path: Path) -> EvalCase:
    """Parse one eval-case frontmatter file into an `EvalCase`."""
    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalCaseError(f"{path}: malformed frontmatter: {exc}") from exc
    metadata: dict[str, Any] = dict(post.metadata)
    if not metadata:
        raise EvalCaseError(f"{path}: no frontmatter — not an eval case")
    try:
        return EvalCase(**metadata)
    except ValidationError as exc:
        raise EvalCaseError(f"{path}: invalid eval case: {exc}") from exc


def render_report(report: EvalReport) -> str:
    """Render the report as a citable Markdown table (case id + provenance per row).

    Cells are escaped by `core.markdown`, since provenance contains literal pipes. A metric with no
    unit renders `MISSING`, not a blank.
    """
    lines = [
        f"# Eval report (case-set {report.case_set_version})",
        "",
        render_table(
            ["Case", "Metric", "Value", "Unit", "Pass", "Provenance"],
            [
                [
                    r.case_id,
                    r.result_metric,
                    f"{r.value:.4g}",
                    r.unit or "",
                    MISSING if r.passed is None else ("pass" if r.passed else "**FAIL**"),
                    r.provenance,
                ]
                for r in report.results
            ],
        ),
    ]
    failed = report.failed()
    regressions = report.regressions()
    demonstrated = len(failed) - len(regressions)
    summary = f"**{len(failed)} gated metric(s) failed** of {len(report.results)} scored"
    if demonstrated:
        # Named rather than merely subtracted: a reader seeing "3 failed" in a green build needs to
        # know which of them are the case-set demonstrating that a gate can fire at all.
        summary += f" — {demonstrated} of them by design, {len(regressions)} regression(s)"
    lines += ["", summary + "."]
    unfireable = report.gates_no_demonstration_can_fire()
    if unfireable:
        # Beside the failure table rather than only in the exit code, for the same reason the
        # inert list is: nothing appears in a report to point at a gate that was never exercised.
        lines += [
            "",
            f"**{len(unfireable)} gated metric(s) have no demonstration case**: "
            f"{', '.join(unfireable)}. Each is scored only over cases written to pass, so a "
            "version of it that stopped measuring and answered perfectly would move nothing here.",
        ]
    inert = report.inert_demonstrations()
    if inert:
        # In the report, not only in the exit code: a gate that stopped firing is invisible by
        # construction — nothing appears in the failure table to point at.
        lines += [
            "",
            f"**{len(inert)} demonstration case(s) no longer fails any gate**: "
            f"{', '.join(inert)}. Each was declared `expect_pass: false` to prove a gate can "
            "fire; a gate that stopped firing is lost coverage, not a green build.",
        ]
    return "\n".join(lines) + "\n"


def _baseline_check(report: EvalReport) -> int:
    """Score `report` against the committed baseline and print the per-metric numbers.

    Returns the process exit code — non-zero when a metric worsened past the noise band, or when the
    two sides scored different case-sets (see `compare_to_baseline`).
    """
    baseline = load_baseline(settings.eval_baseline_path)
    try:
        comparison = compare_to_baseline(report, baseline, settings.eval_drift_epsilon)
    except CaseSetMismatchError as exc:
        print(exc)
        return 1
    print(render_comparison(comparison), end="")
    return 1 if comparison.worsened() else 0


def main(argv: list[str] | None = None) -> int:
    """CLI: score the versioned case-set and print the citable report.

    Run as `python -m chemclaw.evals.harness [case_dir] [--case-set-version V] [--strict]
    [--baseline]`.

    By default it reports for humans and exits zero whenever the case-set loaded, since
    demonstration cases fail by design. `--strict` exits non-zero on a regression (a failed gated
    metric outside the demonstrations), on an inert demonstration
    (`EvalReport.inert_demonstrations`) and on a gated metric no case fails
    (`EvalReport.gates_no_demonstration_can_fire`). `--baseline` compares against
    `data/evals/baseline.json` offline and exits non-zero only on a worsening move (see
    `baseline.render_comparison`). With both flags, `--strict` decides the exit code first and the
    comparison is still printed.

    Returns non-zero in every mode when the case-set cannot be loaded or scored, so a vacuous run
    never exits green.
    """
    parser = argparse.ArgumentParser(
        prog="chemclaw.evals.harness", description="Score the versioned eval case-set."
    )
    parser.add_argument("case_dir", nargs="?", default=settings.eval_case_dir)
    # An option, not a positional: `--baseline` needs the version but not the case directory, and a
    # positional would force restating `eval_case_dir` and bypass its ENV override.
    parser.add_argument(
        "--case-set-version",
        default="unversioned",
        help="the case-set version this run scored (must match the baseline's under `--baseline`)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when a gated metric fails (what a CI quality gate needs)",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help=(
            "compare the run's aggregates against the committed baseline and exit non-zero when a "
            "metric worsened past the drift band (requires `version` to name the baseline's "
            "case-set)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        report = run_eval(load_eval_cases(args.case_dir), args.case_set_version)
    except EvalCaseError as exc:
        print(exc)
        return 1
    print(render_report(report), end="")
    baseline_code = _baseline_check(report) if args.baseline else 0
    if args.strict and (
        report.regressions()
        or report.inert_demonstrations()
        or report.gates_no_demonstration_can_fire()
    ):
        return 1
    return baseline_code


if __name__ == "__main__":
    raise SystemExit(main())
