"""A workflow file GitHub refuses to load is a workflow that never runs, and nothing says so.

A job-status function (`cancelled()` etc.) is legal only in an `if:`; anywhere else GitHub rejects
the whole file, so schedules stop firing and every push records an unactionable failed run. This
finds every `${{ }}` expression outside an `if:` and refuses a status function in it — one narrow
context rule, over every workflow in the directory.
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
