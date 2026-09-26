"""A workflow file GitHub refuses to load is a workflow that never runs, and nothing says so.

`.github/workflows/mutants.yml` carried `cancelled()` in a step's `env:` from 2026-09-22 until this
file. A job-status function is legal only in a job's or a step's `if:`, and GitHub validates the
whole file before it reads the triggers — so the weekly schedule stopped firing and every push, on
every branch, recorded a failed `mutants` run with no jobs and the one line "This run likely failed
because of a workflow file issue". That is a red row nobody can act on from the run page, next to
green `ci` checks, and it reads as noise until someone runs `actionlint`.

The class is checkable offline with the YAML parser the suite already has: find every `${{ }}`
expression outside an `if:` and refuse a status function in it. Narrow on purpose — it is the one
context rule that has bitten this repository, not a reimplementation of GitHub's context table —
and the scope is derived from the directory, so a new workflow is covered without being named.
"""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# GitHub's job-status check functions. Each is "only available in `jobs.<job_id>.if` and
# `jobs.<job_id>.steps.if`" per its context-availability table.
_STATUS_FUNCTION = re.compile(r"\b(success|failure|cancelled|always)\s*\(\s*\)")
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def _misplaced_status_calls(node: Any, path: str = "") -> Iterator[str]:
    """Every `<key path>: <function>()` where a status function sits in a non-`if` expression."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "if":
                continue
            yield from _misplaced_status_calls(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _misplaced_status_calls(value, f"{path}[{index}]")
    elif isinstance(node, str):
        for expression in _EXPRESSION.findall(node):
            for call in _STATUS_FUNCTION.findall(expression):
                yield f"{path}: {call}()"


def _workflow_files() -> list[Path]:
    """Every workflow GitHub would load from this checkout."""
    return sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])


def test_the_scope_is_the_workflows_directory_and_it_is_not_empty() -> None:
    """A guard over zero files passes forever; the mutation workflow is the one that broke."""
    assert _WORKFLOWS / "mutants.yml" in _workflow_files()


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda path: path.name)
def test_a_status_function_appears_only_where_github_allows_one(workflow: Path) -> None:
    """`cancelled()` in an `env:` value made GitHub reject `mutants.yml` outright."""
    misplaced = list(_misplaced_status_calls(yaml.safe_load(workflow.read_text())))
    assert not misplaced, (
        f"{workflow.name} calls a job-status function outside an `if:`, which GitHub refuses "
        f"when it loads the file — the workflow then never runs: {misplaced}. Read "
        "`${{ job.status }}` instead where a step needs the outcome as a value."
    )


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"steps": [{"if": "${{ failure() || cancelled() }}", "run": "x"}]}, []),
        ({"steps": [{"if": "always()", "env": {"A": "${{ job.status }}"}}]}, []),
        (
            {"steps": [{"env": {"OUTCOME": "${{ cancelled() && 'c' || 'f' }}"}}]},
            [".steps[0].env.OUTCOME: cancelled()"],
        ),
        ({"jobs": {"j": {"env": {"A": "${{ always( ) }}"}}}}, [".jobs.j.env.A: always()"]),
        (
            {"steps": [{"with": {"body": "done: ${{\n success() }}"}}]},
            [".steps[0].with.body: success()"],
        ),
        # A status word outside an expression is prose, not a call GitHub evaluates.
        ({"steps": [{"run": "echo 'on failure() we file an issue'"}]}, []),
    ],
)
def test_the_scan_refuses_a_misplaced_call_and_only_that(
    document: dict[str, Any], expected: list[str]
) -> None:
    """Both directions, including the exact shape that broke `mutants.yml`."""
    assert list(_misplaced_status_calls(document)) == expected
