"""Every launcher hands the job wrapper the same `JobTerms`, filled in one place.

The wrapper's inputs that come from a job's manifest or the deployment's configuration all default
to "nothing declared" on the wire, so a launcher that builds them by hand and misses one is not an
error but a different job — an irreversible effect run without its approval. These tests hold the
two halves of the rule: the terms are one model, and every `ConnectorJobInput` built in `src/`
takes them whole from it.
"""

import ast
import importlib
from pathlib import Path

from chemclaw.durable.connector_job import ConnectorJobInput, JobTerms

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: The wrapper inputs that describe one launch rather than the job or the deployment: where it
#: runs, what it was asked with, and the turn it came from.
_PER_LAUNCH = {
    "connector",
    "job",
    "workflow",
    "task_queue",
    "payload",
    "rationale",
    "requested_by",
    "session_id",
    "correlation_id",
    "plan_step",
    "plan_hash",
}


def test_every_wrapper_input_is_either_per_launch_or_a_job_term() -> None:
    """A new wrapper input must be sorted into one of the two kinds, so it is never half-carried.

    A field describing the job or the deployment belongs on `JobTerms`, where every launcher picks
    it up through `job_terms`; one added beside the per-launch fields would be missed by whichever
    launcher its author did not think of.
    """
    terms = set(JobTerms.model_fields)
    assert terms, "JobTerms has no fields; this test has stopped asking anything"
    assert set(ConnectorJobInput.model_fields) - terms == _PER_LAUNCH


def _wrapper_input_calls() -> list[tuple[Path, ast.Call]]:
    """Every `ConnectorJobInput(...)` call in the source tree, with its file."""
    calls: list[tuple[Path, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ConnectorJobInput":
                calls.append((path, node))
    return calls


def test_every_launcher_takes_the_terms_whole() -> None:
    """Each `ConnectorJobInput` in `src/` unpacks a `.terms()` and names no term by hand."""
    calls = _wrapper_input_calls()
    launchers = {path.relative_to(_SRC).as_posix() for path, _ in calls}
    # The chat tool, the template sequencer and the hypothesis tournament: if this set shrinks the
    # scan has gone blind, and if it grows the new launcher is checked below like the others.
    assert {
        "connectors/jobs.py",
        "durable/template_job.py",
        "durable/hypothesis_tournament.py",
    } <= launchers
    terms = set(JobTerms.model_fields)
    for path, call in calls:
        where = f"{path.relative_to(_SRC)}:{call.lineno}"
        unpacked = [
            keyword.value
            for keyword in call.keywords
            if keyword.arg is None
            and isinstance(keyword.value, ast.Call)
            and getattr(keyword.value.func, "attr", "") == "terms"
        ]
        assert len(unpacked) == 1, f"{where} builds ConnectorJobInput without `**<...>.terms()`"
        by_hand = sorted({keyword.arg for keyword in call.keywords if keyword.arg} & terms)
        assert not by_hand, f"{where} sets {by_hand} by hand instead of through JobTerms"


def _carriers() -> set[str]:
    """The names of the models that carry terms from a resolving activity to a launching workflow.

    Derived from `JobTerms`' subclasses once every launcher's module is imported, so a new carrier
    is checked without being listed here.
    """
    for module in (
        "chemclaw.durable.hypothesis_tournament",
        "chemclaw.durable.template_activities",
    ):
        importlib.import_module(module)
    return {cls.__name__ for cls in JobTerms.__subclasses__()} - {ConnectorJobInput.__name__}


def test_every_resolved_launch_is_filled_by_job_terms() -> None:
    """Each carrier built with a payload in `src/` unpacks `job_terms(...).terms()`.

    A carrier built without a payload is a refusal, which launches nothing and needs no terms.
    """
    carriers = _carriers()
    assert {"ResolvedJob", "_GroundedJob"} <= carriers
    filled = 0
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") in carriers):
                continue
            if "payload" not in {keyword.arg for keyword in node.keywords}:
                continue
            where = f"{path.relative_to(_SRC)}:{node.lineno}"
            from_job_terms = [
                keyword.value
                for keyword in node.keywords
                if keyword.arg is None
                and isinstance(keyword.value, ast.Call)
                and isinstance(keyword.value.func, ast.Attribute)
                and isinstance(keyword.value.func.value, ast.Call)
                and getattr(keyword.value.func.value.func, "id", "") == "job_terms"
            ]
            assert from_job_terms, f"{where} builds a launch without `**job_terms(...).terms()`"
            filled += 1
    assert filled >= 2, "found fewer resolved launches than the two known; the scan went blind"
