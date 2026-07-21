"""Eval harness: run a metric set over a versioned case-set → citable report (2b.2).

The harness runs each case's named metrics and collects `MetricResult`s into a report
that renders to Markdown a human can cite (every row carries its case id and the
metric's provenance). Cases are versioned frontmatter files under `eval_case_dir`, so
the case-set lives in Git and changes to it are reviewable. They are loaded here rather
than through `kg.note` deliberately: an eval case is a structured evaluation payload
(`output`/`reference`), not a relational graph note, so it neither uses the note schema
nor lives under `knowledge_dir` (where `kg-validate` would reject it).
"""

import sys
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from pydantic import BaseModel, Field, ValidationError

from chemclaw.config import settings
from chemclaw.errors import ChemclawError
from evals.metric import EvalCase, get_metric


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

    def failed(self) -> list[ScoredResult]:
        """Gated results that did not pass (a regression, treated like a test failure)."""
        return [r for r in self.results if r.passed is False]


class EvalCaseError(ChemclawError):
    """A case file could not be read or is not a valid eval case (G4)."""


def run_eval(cases: list[EvalCase], case_set_version: str) -> EvalReport:
    """Score every case by its named metrics into a versioned report.

    A metric failure is re-raised with the case and metric that triggered it, so a bad
    case names itself instead of surfacing as an opaque error deep in a metric (G4).
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
    return EvalReport(case_set_version=case_set_version, results=results)


def load_eval_cases(directory: str) -> list[EvalCase]:
    """Load eval cases from `*.md` frontmatter files under `directory`, id-sorted.

    Each file's frontmatter carries `id`, `metrics`, `output`, and optional `reference`
    (the Markdown body is free-form rationale). A malformed file raises `EvalCaseError`
    naming the path, so a broken case-set fails loudly, not silently (G4). A missing
    directory or one with zero cases also raises: an empty case-set would score nothing
    and let the quality gate pass vacuously.
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
    """Render the report as a citable Markdown table (case id + provenance per row)."""
    lines = [
        f"# Eval report (case-set {report.case_set_version})",
        "",
        "| Case | Metric | Value | Unit | Pass | Provenance |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in report.results:
        gate = "—" if r.passed is None else ("pass" if r.passed else "**FAIL**")
        unit = r.unit or ""
        lines.append(
            f"| {r.case_id} | {r.result_metric} | {r.value:.4g} | {unit} | {gate} "
            f"| {r.provenance} |"
        )
    failed = report.failed()
    lines += ["", f"**{len(failed)} gated metric(s) failed** of {len(report.results)} scored."]
    return "\n".join(lines) + "\n"


def main() -> int:
    """CLI: score the versioned case-set and print the citable report.

    Run as `python -m evals.harness [case_dir] [version]`. This prints the report for
    humans; regression gating (which case must pass/fail) is pinned by the test suite,
    so a demonstration case that is expected to fail its gate does not fail the CLI.
    Returns non-zero when the case-set cannot be loaded or scored (missing, empty, or
    broken — G4), so a vacuous or unscorable run never exits green.
    """
    case_dir = sys.argv[1] if len(sys.argv) > 1 else settings.eval_case_dir
    version = sys.argv[2] if len(sys.argv) > 2 else "unversioned"
    try:
        report = run_eval(load_eval_cases(case_dir), version)
    except EvalCaseError as exc:
        print(exc)
        return 1
    print(render_report(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
