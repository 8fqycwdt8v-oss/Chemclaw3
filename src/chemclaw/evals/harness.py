"""Eval harness: run a metric set over a versioned case-set → citable report (2b.2).

The harness runs each case's named metrics and collects `MetricResult`s into a report
that renders to Markdown a human can cite (every row carries its case id and the
metric's provenance). Cases are versioned frontmatter files under `eval_case_dir`, so
the case-set lives in Git and changes to it are reviewable. They are loaded here rather
than through `chemclaw.kg.note` deliberately: an eval case is a structured evaluation payload
(`output`/`reference`), not a relational graph note, so it neither uses the note schema
nor lives under `knowledge_dir` (where `kg-validate` would reject it).
"""

import argparse
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from pydantic import BaseModel, Field, ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.evals.baseline import (
    CaseSetMismatchError,
    compare_to_baseline,
    load_baseline,
    render_comparison,
)
from chemclaw.evals.metric import EvalCase, get_metric


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
    # Per case, whether its gates were declared expected-to-pass. Carried on the report so
    # `regressions()` needs no second pass over the cases, and so a serialized report is still
    # interpretable — a bare list of failures cannot say which were meant to happen.
    expect_pass: dict[str, bool] = Field(default_factory=dict)

    def failed(self) -> list[ScoredResult]:
        """Gated results that did not pass (a regression, treated like a test failure)."""
        return [r for r in self.results if r.passed is False]

    def _demonstrations(self) -> set[str]:
        """Case ids declared `expect_pass: false` — the cases that exist to fail."""
        return {case_id for case_id, expected in self.expect_pass.items() if not expected}

    def regressions(self) -> list[ScoredResult]:
        """Failures that were not supposed to happen — `failed()` minus the demonstration cases.

        The distinction `--strict` needs, and the one whose absence is why `make eval` could not
        gate: two shipped cases exist to *demonstrate* a gate firing, so a command that treated
        every failure as a regression would have been red from the day they were written.
        """
        return [r for r in self.failed() if r.case_id not in self._demonstrations()]

    def inert_demonstrations(self) -> list[str]:
        """Demonstration cases that no longer fail anything — the other half of `expect_pass`.

        `regressions()` can only ever detect a *failure*, and it suppresses failures per case, so
        the one thing it structurally cannot see is a gate that stops firing: loosen a threshold or
        break a metric and the by-design failure simply leaves the set, taking the coverage with it
        and leaving the command green. Measured on the shipped case-set — raising
        `eval_efactor_max`/`eval_pmi_max` to 1000 dropped `pharma-solvent-heavy` from the failures
        (4 → 2), `regressions()` stayed empty, `--strict` still exited 0, and the whole
        green-chemistry gate was inert with no signal.

        So `expect_pass: false` is read as an **assertion** that at least one of the case's gated
        metrics fails, not as a mute on its failures. At least one, not all: a demonstration may
        legitimately carry a passing metric beside the failing one — `retrieval_precision` stays
        1.0 in the case whose `retrieval_recall` is the point.

        Id-sorted, so the list reads the same on every run.
        """
        failing = {r.case_id for r in self.results if r.passed is False}
        return sorted(self._demonstrations() - failing)


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
    return EvalReport(
        case_set_version=case_set_version,
        results=results,
        expect_pass={case.id: case.expect_pass for case in cases},
    )


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


def _cell(text: str) -> str:
    """Escape Markdown table delimiters so cell content cannot split its row.

    Provenance legitimately contains literal pipes (the set-cardinality/absolute-value
    notation of `precision`/`recall`/`prediction_error`), which would otherwise shift
    values under the wrong headers of the citable table (G5).
    """
    return text.replace("|", "\\|")


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
        unit = _cell(r.unit or "")
        lines.append(
            f"| {_cell(r.case_id)} | {_cell(r.result_metric)} | {r.value:.4g} | {unit} | {gate} "
            f"| {_cell(r.provenance)} |"
        )
    failed = report.failed()
    regressions = report.regressions()
    demonstrated = len(failed) - len(regressions)
    summary = f"**{len(failed)} gated metric(s) failed** of {len(report.results)} scored"
    if demonstrated:
        # Named rather than merely subtracted: a reader seeing "3 failed" in a green build needs to
        # know which of them are the case-set demonstrating that a gate can fire at all.
        summary += f" — {demonstrated} of them by design, {len(regressions)} regression(s)"
    lines += ["", summary + "."]
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

    Returns the process exit code — non-zero when a metric worsened past the noise band, or when
    the two sides scored different case-sets (see `compare_to_baseline`).
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

    **Two modes, because there are two audiences.** By default this reports for humans and returns
    zero whenever the case-set loaded, so a demonstration case that is *expected* to fail its gate
    does not fail the command. That is deliberate and stays the default. But `.github/workflows/`
    labelled `make eval` "the scientific quality gates" while it could not fail on a science
    regression — the real gate is a pinned assertion in `tests/test_evals.py` — so a reader trusted
    the wrong step. `--strict` makes the labelled step gate what it claims to: a failed gated
    metric is a non-zero exit.

    **And so is a gate that stopped firing.** `expect_pass: false` is an assertion rather than a
    mute, so `--strict` also fails when a declared demonstration passes everything — see
    `EvalReport.inert_demonstrations`. Without that half, loosening a threshold silently removes
    coverage and the command stays green, which is the failure the strict mode exists to prevent
    read from the other direction.

    **`--baseline` answers a third question: "did anything get *worse* than last time?"** The gates
    `--strict` reads are absolute lines — they cannot see an `f1` sliding from 0.95 to 0.70 as long
    as both clear the floor. The committed `data/evals/baseline.json` is what can, and until now the
    only code that read it was `durable/eval_drift.py`, a Temporal workflow that is off by default —
    so the recorded baseline could not be scored against without a broker. `--baseline` runs exactly
    that comparison as an ordinary offline command and prints the numbers (see
    `baseline.render_comparison`). It exits non-zero only on a move in the *worsening* direction,
    because a command that failed on an improvement is a command people learn to re-run.

    The two flags compose: with both, `--strict` decides the exit code first (a hard gate failure
    outranks a drift), and the comparison is still printed so one run yields both readings.

    Returns non-zero when the case-set cannot be loaded or scored (missing, empty, or broken — G4)
    in any mode, so a vacuous or unscorable run never exits green.
    """
    parser = argparse.ArgumentParser(
        prog="chemclaw.evals.harness", description="Score the versioned eval case-set."
    )
    parser.add_argument("case_dir", nargs="?", default=settings.eval_case_dir)
    # An option rather than a second positional (which it used to be): `--baseline` needs the
    # version and *not* the case directory, and a positional cannot be skipped — the caller would
    # have had to restate `eval_case_dir`, silently defeating its `CHEMCLAW_EVAL_CASE_DIR` override.
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
    if args.strict and (report.regressions() or report.inert_demonstrations()):
        return 1
    return baseline_code


if __name__ == "__main__":
    raise SystemExit(main())
