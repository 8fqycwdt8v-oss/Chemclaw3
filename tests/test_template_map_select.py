"""The `map` and `select` steps: a bounded fan-out and a deterministic cut.

The contract half runs offline: what a map may reference and wrap, how a select orders, what the
run ceiling charges a map, and what an agent-authored map may hold. The durable half drives
`TemplateWorkflow` on the time-skipping server with recording stand-ins: a tool map runs every item
and drops a failure, a job map's children are named by item index, and a list longer than the map
declared is refused before anything is dispatched.
"""

import math
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from chemclaw.agent.template_surface import (
    TemplateSurface,
    run_ceiling_problems,
    step_ceiling,
    step_problems,
)
from chemclaw.agent.workflow_tools import WorkflowStep, _document
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobInput, ConnectorJobResult
from chemclaw.durable.job_record import JobRecord
from chemclaw.durable.template_activities import ToolStepInput, authorize_job_step
from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow
from chemclaw.templates.composed import authored_problems, job_steps, unapproved_jobs
from chemclaw.templates.manifest import MapStep, SelectStep, Template
from chemclaw.templates.registry import discovered
from chemclaw.templates.resolve import UnresolvedReference, select
from chemclaw.templates.schedule import schedule
from tests.temporal_env import pydantic_client, start_env_or_skip

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "connectors"
_CANDIDATES = {"name": "candidates", "type": "string[]", "description": "SMILES to screen."}


def _template(steps: list[dict[str, Any]], **overrides: Any) -> Template:
    """A template over one `candidates` list input, with `steps` as given."""
    return Template.model_validate(
        {
            "name": "funnel",
            "summary": "A funnel.",
            "inputs": [_CANDIDATES],
            "steps": steps,
            **overrides,
        }
    )


def _map(
    step_id: str = "screen",
    *,
    over: str = "${inputs.candidates}",
    inner: dict[str, Any] | None = None,
    max_items: int = 8,
    concurrency: int = 4,
) -> dict[str, Any]:
    """A map step over `over`, binding `candidate`, wrapping a tool call by default."""
    return {
        "id": step_id,
        "kind": "map",
        "over": over,
        "as": "candidate",
        "max_items": max_items,
        "concurrency": concurrency,
        "step": inner
        or {
            "kind": "tool",
            "tool": "compute_electronic_properties",
            "arguments": {"smiles": "${candidate}"},
        },
    }


def _select(step_id: str = "shortlist", **fields: Any) -> dict[str, Any]:
    """A select step over the screen's result, top 2 by `result.score`, unless overridden."""
    return {
        "id": step_id,
        "kind": "select",
        "from": "${steps.screen.result}",
        "by": "result.score",
        "top": 2,
        **fields,
    }


# --- the contract ---------------------------------------------------------------------------


def test_a_map_binds_its_item_for_the_step_it_wraps_and_the_wrapped_step_shares_its_id() -> None:
    """The item variable resolves inside the map; the inner step needs no id of its own."""
    template = _template([_map()])
    (step,) = template.steps
    assert isinstance(step, MapStep)
    assert step.step.id == "screen"
    assert step.as_ == "candidate"


@pytest.mark.parametrize(
    ("where", "steps"),
    [
        (
            "outside any map",
            [
                {
                    "id": "one",
                    "kind": "tool",
                    "tool": "compute_xtb_energy",
                    "arguments": {"smiles": "${candidate}"},
                }
            ],
        ),
        (
            "inside a map that binds another name",
            [_map(inner={"kind": "tool", "tool": "t", "arguments": {"smiles": "${other}"}})],
        ),
    ],
)
def test_an_item_variable_no_map_binds_is_malformed(where: str, steps: list[Any]) -> None:
    """`${candidate}` is legal only where a map binds `candidate`; anywhere else it is a typo."""
    with pytest.raises(ValidationError, match="malformed reference"):
        _template(steps)


@pytest.mark.parametrize(
    "over", ["${candidate}", "candidates", "${inputs.candidates} and more", "${steps.x.result}"]
)
def test_a_map_runs_over_one_reference_to_an_input_or_an_earlier_result(over: str) -> None:
    """`over` is one whole reference to something already in scope, never text or an item."""
    with pytest.raises(ValidationError):
        _template([_map(over=over)])


@pytest.mark.parametrize("name", ["inputs", "steps"])
def test_a_map_item_may_not_shadow_a_scope(name: str) -> None:
    """An item called `inputs` or `steps` would make every reference ambiguous."""
    step = _map()
    step["as"] = name
    with pytest.raises(ValidationError, match="may not name its item"):
        _template([step])


@pytest.mark.parametrize(
    "inner",
    [
        _map("nested"),
        {"kind": "agent", "prompt": "think about ${candidate}"},
        {"kind": "select", "from": "${inputs.candidates}", "by": "x"},
    ],
)
def test_a_map_wraps_only_a_tool_or_a_job(inner: dict[str, Any]) -> None:
    """No nested map and no per-item reasoning turn: the type refuses both, not a depth counter."""
    with pytest.raises(ValidationError):
        _template([_map(inner=inner)])


@pytest.mark.parametrize(("field", "value"), [("max_items", 0), ("concurrency", 0)])
def test_a_map_declares_a_positive_bound(field: str, value: int) -> None:
    """An unbounded map cannot be written: both numbers are required to be at least one."""
    step = _map()
    step[field] = value
    with pytest.raises(ValidationError):
        _template([step])


def test_a_where_value_is_a_literal() -> None:
    """A reference in `where` would be compared as text, so it is refused at load time."""
    step = _select(where=[{"field": "result.score", "op": "gt", "value": "${inputs.candidates}"}])
    with pytest.raises(ValidationError, match="malformed reference"):
        _template([_map(), step])


def test_a_map_and_a_select_depend_on_what_they_read_and_not_on_the_item() -> None:
    """The item is local, so it adds no edge; `over`, `from` and inner references do."""
    template = _template(
        [
            _map(),
            _select(),
            _map(
                "refine",
                over="${steps.shortlist.result}",
                inner={"kind": "tool", "tool": "t", "arguments": {"smiles": "${candidate.item}"}},
            ),
        ]
    )
    assert [[step.id for step in wave] for wave in schedule(template)] == [
        ["screen"],
        ["shortlist"],
        ["refine"],
    ]


# --- select is arithmetic ---------------------------------------------------------------------


def _entries(*scores: float | None) -> list[dict[str, Any]]:
    """Map-shaped entries carrying one `score` each, in this order."""
    return [
        {"index": index, "item": f"c{index}", "result": {"score": score}}
        for index, score in enumerate(scores)
    ]


def _step(**fields: Any) -> SelectStep:
    """A validated select step with `fields` applied."""
    return SelectStep.model_validate(_select(**fields))


def test_select_keeps_the_top_k_and_ties_keep_list_order_in_either_direction() -> None:
    """A tie never depends on timing: the earlier item wins, ascending or descending."""
    items = _entries(1.0, 3.0, 3.0, 2.0, 3.0)
    assert [e["index"] for e in select(items, _step(top=3))] == [1, 2, 4]
    assert [e["index"] for e in select(items, _step(order="asc", top=2))] == [0, 3]
    assert [e["index"] for e in select(items, _step(top=None))] == [1, 2, 4, 3, 0]
    # Pure: the same input gives the same answer, item for item.
    assert select(items, _step(top=3)) == select(list(items), _step(top=3))


def test_select_filters_by_where_and_leaves_out_a_null_value() -> None:
    """A null is missing data, not the smallest value; `where` filters before the cut."""
    items = _entries(5.0, None, 1.0, 4.0)
    where = [{"field": "result.score", "op": "lt", "value": 5}]
    assert [e["index"] for e in select(items, _step(top=None, where=where))] == [3, 2]
    assert [e["index"] for e in select(items, _step(top=None))] == [0, 3, 2]


def test_select_refuses_a_field_that_does_not_exist_and_a_value_that_is_not_a_list() -> None:
    """A misspelled field is a typo, not data: it raises rather than selecting nothing."""
    with pytest.raises(UnresolvedReference, match="scor"):
        select(_entries(1.0), _step(by="result.scor"))
    with pytest.raises(UnresolvedReference, match="not a list"):
        select({"score": 1.0}, _step())


def test_select_refuses_values_it_cannot_order() -> None:
    """Mixed types have no order; guessing one would be a ranking nobody chose."""
    items = [{"result": {"score": 1.0}}, {"result": {"score": "high"}}]
    with pytest.raises(ValueError, match="cannot order"):
        select(items, _step())


# --- the run ceiling ----------------------------------------------------------------------------


def _job_map(max_items: int, concurrency: int) -> dict[str, Any]:
    """A map over a job, as the shipped funnel's refine stage is."""
    return _map(
        "refine",
        inner={"kind": "job", "job": "refine_ensemble", "arguments": {"smiles": "${candidate}"}},
        max_items=max_items,
        concurrency=concurrency,
    )


def test_a_map_costs_its_inner_step_once_per_batch() -> None:
    """Ceiling = per-item ceiling x ceil(max_items / concurrency), from the declared bound."""
    ceilings = settings.template_step_ceilings()
    (step,) = _template([_map(max_items=9, concurrency=4)]).steps
    assert step_ceiling(step, ceilings) == ceilings["tool"][0] * math.ceil(9 / 4)
    (select_step,) = _template([_map(), _select()]).steps[1:]
    assert step_ceiling(select_step, ceilings) == 0.0


def test_a_job_map_that_cannot_finish_inside_the_run_ceiling_is_refused_before_it_starts() -> None:
    """One batch of jobs fits a run; two do not, and the gate says so before any workflow id."""
    assert run_ceiling_problems(_template([_job_map(max_items=3, concurrency=3)])) == []
    (problem,) = run_ceiling_problems(_template([_job_map(max_items=6, concurrency=3)]))
    assert "template_run_timeout_seconds" in problem
    assert "refine=" in problem


def test_a_map_above_the_deployments_bounds_is_refused_at_the_gate_and_at_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config bounds are the surface's question, so they apply to both callers of it."""
    monkeypatch.setattr(settings, "template_map_max_items", 4)
    monkeypatch.setattr(settings, "orchestrator_max_parallel_children", 2)
    surface = TemplateSurface(
        tools={"compute_electronic_properties"}, jobs=set(), profiles=set(), signatures={}
    )
    problems = step_problems(_template([_map(max_items=8, concurrency=4)]), surface)
    assert any("template_map_max_items=4" in p for p in problems)
    assert any("orchestrator_max_parallel_children=2" in p for p in problems)
    assert step_problems(_template([_map(max_items=4, concurrency=2)]), surface) == []


def test_the_shipped_funnel_fits_its_run_ceiling() -> None:
    """`screen-then-refine` maps, selects and maps again, and still finishes inside one run."""
    template = discovered()["screen-then-refine"]
    assert [step.kind for step in template.steps] == ["map", "select", "map", "agent"]
    assert run_ceiling_problems(template) == []


# --- what an agent may compose -------------------------------------------------------------------


def test_an_authored_map_may_wrap_only_a_read_and_a_mapped_job_waits_for_approval() -> None:
    """A map is judged by the step it wraps; a select runs nothing and is always allowed."""
    writes = frozenset({"compute_electronic_properties"})
    writing = _template([_map(), _select()])
    (problem,) = authored_problems(writing, writes)
    assert "compute_electronic_properties" in problem
    assert authored_problems(writing, frozenset()) == []

    with_job = _template([_job_map(max_items=3, concurrency=3)])
    assert authored_problems(with_job, writes) == []
    assert job_steps(with_job) == ["refine"]
    assert unapproved_jobs(with_job, "", "fingerprint") != []


def test_compose_workflow_writes_a_map_and_a_select() -> None:
    """The tool's flat step shape becomes the manifest's map and select, item named `item`."""
    document = _document(
        "screen",
        "Screen then cut.",
        [],
        [
            WorkflowStep(id="find", tool="search_compounds", arguments={"query": "amines"}),
            WorkflowStep(
                id="screen",
                tool="lookup_compound",
                over="${steps.find.result.hits}",
                arguments={"query": "${item}"},
                max_items=5,
            ),
            WorkflowStep(id="top", select="${steps.screen.result}", by="result.score", top=2),
            WorkflowStep(id="say", prompt="Report ${steps.top.result}"),
        ],
    )
    assert [step.kind for step in document.steps] == ["tool", "map", "select", "agent"]
    _find, screen, top, _say = document.steps
    assert isinstance(screen, MapStep) and isinstance(top, SelectStep)
    assert (screen.as_, screen.max_items, screen.concurrency) == ("item", 5, 5)
    assert top.top == 2


# --- the durable half ---------------------------------------------------------------------------


@activity.defn(name="record_job")
async def _keep_record(record: JobRecord) -> None:
    """Keep the run's record, which carries the degradations a dropped item writes."""
    _RECORDS.append(record)


@activity.defn(name="completed_steps")
async def _nothing_to_resume(request: Any) -> dict[str, Any]:
    """Answer the resume read with "nothing", as a first run of an id gets."""
    return {}


_RECORDS: list[JobRecord] = []
_TOOL_CALLS: list[str] = []


@activity.defn(name="run_tool_step")
async def _scored_tool(step: ToolStepInput) -> dict[str, Any]:
    """Score a candidate by its length, and fail on `poison`, as a bad structure would."""
    smiles = str(step.arguments["smiles"])
    _TOOL_CALLS.append(smiles)
    if smiles == "poison":
        raise ApplicationError("unparseable SMILES", non_retryable=True)
    return {"score": float(len(smiles))}


async def _run(template: Template, inputs: dict[str, Any], **workers: Any) -> Any:
    """Run `template` to its end on the time-skipping server, with the stand-ins above."""
    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[TemplateWorkflow, *workers.get("workflows", [])],
            activities=[_scored_tool, _keep_record, _nothing_to_resume, authorize_job_step],
        ):
            return await client.execute_workflow(
                TemplateWorkflow.run,
                TemplateRunInput(template=template, inputs=inputs, requested_by="tester"),
                id=f"map-select-{template.name}",
                task_queue=settings.background_task_queue,
                execution_timeout=timedelta(seconds=300),
            )


async def test_a_tool_map_runs_every_item_drops_a_failure_and_selects_deterministically() -> None:
    """Every item is tried; the failure is named, not fatal; the cut follows the scores."""
    _RECORDS.clear()
    _TOOL_CALLS.clear()
    template = _template(
        [
            _map(inner={"kind": "tool", "tool": "t", "arguments": {"smiles": "${candidate}"}}),
            _select(top=2),
        ]
    )
    candidates = ["CC", "poison", "CCCC", "CCO", "CCCC"]
    result = await _run(template, {"candidates": candidates})

    assert sorted(_TOOL_CALLS) == sorted(candidates)
    screened = result.steps["screen"]
    assert [entry["index"] for entry in screened] == [0, 2, 3, 4]
    assert [entry["item"] for entry in screened] == ["CC", "CCCC", "CCO", "CCCC"]
    # Two tied at the top: list order breaks the tie.
    assert [entry["index"] for entry in result.steps["shortlist"]] == [2, 4]
    (record,) = _RECORDS
    assert "[1]" in record.result["degraded"]["screen"]


async def test_a_list_longer_than_the_map_declared_is_refused_before_anything_runs() -> None:
    """The ceiling was sized from `max_items`; a longer list fails the step with nothing spent."""
    _RECORDS.clear()
    _TOOL_CALLS.clear()
    template = _template(
        [
            _map(
                inner={"kind": "tool", "tool": "t", "arguments": {"smiles": "${candidate}"}},
                max_items=2,
                concurrency=2,
            )
        ]
    )
    with pytest.raises(WorkflowFailureError):
        await _run(template, {"candidates": ["C", "CC", "CCC"]})
    assert _TOOL_CALLS == []
    (record,) = _RECORDS
    assert "max_items=2" in (record.failure_reason or "")


_CHILD_IDS: list[str] = []


@workflow.defn(name="ConnectorJobWorkflow", sandboxed=False)
class _NamedJobWrapper:
    """Stands in for the job wrapper: keeps its own workflow id, and fails on `poison`."""

    @workflow.run
    async def run(self, job: ConnectorJobInput) -> ConnectorJobResult:
        """Record the child id this item ran under; answer with the subject's length."""
        _CHILD_IDS.append(workflow.info().workflow_id)
        subject = str(job.payload["subject"])
        if subject == "poison":
            raise ApplicationError("the job failed on this item", non_retryable=True)
        return ConnectorJobResult(summary=subject, data={"score": len(subject)})


@pytest.fixture
def fixture_bundle(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the registry at the test bundle, whose one job takes a `subject` string."""
    monkeypatch.setattr(settings, "manifest_driver_packages", "tests")
    monkeypatch.setattr(settings, "connectors_dir", str(_FIXTURE_DIR))
    monkeypatch.setattr(settings, "connectors_enabled", "")
    yield "run_fixture_job"


async def test_a_job_map_runs_children_named_by_item_index(fixture_bundle: str) -> None:
    """Each item is a child `child_workflow_id(f"{step}-{index}")`; a failed child is dropped."""
    _RECORDS.clear()
    _CHILD_IDS.clear()
    template = _template(
        [
            _map(
                "refine",
                inner={
                    "kind": "job",
                    "job": fixture_bundle,
                    "arguments": {"subject": "${candidate}"},
                },
                max_items=4,
                concurrency=2,
            ),
            {
                **_select(by="result.data.score", top=1),
                "from": "${steps.refine.result}",
            },
        ]
    )
    result = await _run(
        template, {"candidates": ["CC", "poison", "CCCC"]}, workflows=[_NamedJobWrapper]
    )

    # `<parent id>-<parent run id>-refine-<index>`: the run id keeps a re-execution's children
    # distinct under `REJECT_DUPLICATE`, the index keeps the items apart.
    named = [re.fullmatch(r"map-select-funnel-([0-9a-f-]{36})-refine-(\d+)", c) for c in _CHILD_IDS]
    assert all(named), _CHILD_IDS
    assert sorted(match.group(2) for match in named if match) == ["0", "1", "2"]
    assert len({match.group(1) for match in named if match}) == 1
    assert [entry["index"] for entry in result.steps["refine"]] == [0, 2]
    assert [entry["item"] for entry in result.steps["shortlist"]] == ["CCCC"]
    (record,) = _RECORDS
    assert "[1]" in record.result["degraded"]["refine"]
