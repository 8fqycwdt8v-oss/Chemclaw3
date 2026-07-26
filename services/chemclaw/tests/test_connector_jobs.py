"""A generated job launcher is an ordinary tool — including everything that gates an ordinary tool.

The value of generating these is only real if the generated tool is not a special case, so most
of this file is about *sameness*: the schema the model sees is a proper typed one, the name is
the authorization key, the expensive-trigger gate and the dry-run gate both fire, an actor is
demanded before any durable work, and re-launching the identical job returns the existing id
instead of paying twice. Those are the five properties the four hand-written adapters had, now
asserted once against the factory that replaced them.

Temporal is faked at the client seam (`connectors.jobs.connect`) because none of this is about
Temporal's behavior — `test_connector_job_workflow.py` covers that against a real server. What
is under test here is what happens *before* the workflow starts, which is where the gates live.
"""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from agents.authz import AuthorizationError
from agents.dialogue_tools import reset_dry_run, set_dry_run
from agents.identity_context import reset_current_identity, set_current_identity
from agents.turn_signals import JobSignal, begin_turn, drain, end_turn
from connectors.jobs import ConnectorJobError, build_job_tool, job_workflow_id, resolve_params_model
from connectors.manifest import JobSpec
from workflows.connector_job import ConnectorJobInput

_SPEC = JobSpec.model_validate(
    {
        "name": "run_calculation",
        "workflow": "CalculationWorkflow",
        "task_queue": "connector-calc",
        "summary": "Start a calculation and return its job id.",
        "description": "Long-running, so poll it rather than waiting.",
        "params": [
            {"name": "smiles", "type": "string", "description": "The molecule as SMILES."},
            {
                "name": "cycles",
                "type": "integer",
                "description": "How many cycles.",
                "required": False,
            },
        ],
    }
)


class _FakeHandle:
    """A started-workflow handle, carrying just the id the tool returns."""

    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id


class _FakeClient:
    """Records what `start_workflow` was called with, or raises a scripted error instead."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, _run: Any, arg: Any, **kwargs: Any) -> _FakeHandle:
        """Capture the call; raise the scripted error if one was set (the duplicate-submit case)."""
        self.calls.append({"input": arg, **kwargs})
        if self.error is not None:
            raise self.error
        return _FakeHandle(str(kwargs["id"]))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Install a fake Temporal client behind the tool's `connect()` seam."""
    fake = _FakeClient()

    async def _connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("connectors.jobs.connect", _connect)
    return fake


def _params(tool: Any, **values: Any) -> BaseModel:
    """Build the tool's generated params model from `values` (what MAF does from a tool call)."""
    model: type[BaseModel] = tool.__annotations__["params"]
    return model(**values)


def _launch(tool: Any, **values: Any) -> str:
    """Call the generated tool with `values`, as MAF would.

    A sync wrapper because the suite has no pytest-asyncio: each test drives one event loop
    through `asyncio.run`, which is the convention everywhere else here.
    """
    return str(asyncio.run(tool(_params(tool, **values))))


def test_the_generated_tool_is_named_and_documented_for_the_model() -> None:
    """`__name__` is the advertised name and the authz key; the docstring is what the model sees."""
    tool = build_job_tool("calc", _SPEC)
    assert tool.__name__ == "run_calculation"
    doc = tool.__doc__ or ""
    assert doc.startswith("Start a calculation and return its job id.")
    assert "Long-running, so poll it rather than waiting." in doc
    # Every declared param is documented, from the same declaration the schema is built from.
    assert "smiles: The molecule as SMILES." in doc
    assert "cycles: How many cycles." in doc
    assert "get_durable_job_status" in doc  # the model is told how to follow up


def test_the_schema_is_typed_not_a_free_dict() -> None:
    """A `dict[str, Any]` parameter would advertise "pass anything" — how a model calls wrongly."""
    tool = build_job_tool("calc", _SPEC)
    schema = tool.__annotations__["params"].model_json_schema()
    assert schema["properties"]["smiles"]["type"] == "string"
    assert schema["properties"]["smiles"]["description"] == "The molecule as SMILES."
    assert schema["required"] == ["smiles"]  # the optional param is not required
    with pytest.raises(ValidationError):
        _params(tool, smiles="CCO", cycles="not-an-integer")


def test_a_referenced_model_gives_full_fidelity_for_a_structured_input() -> None:
    """The escape hatch that makes "any tool" true: a domain model is referenced, not copied.

    `CampaignSpec` nests an optimization problem with discriminated feature kinds — a shape the
    closed inline param types cannot express, and re-declaring it in YAML would be a second
    source of truth
    for a schema that already exists and is already validated in code.
    """
    referenced = JobSpec.model_validate(
        {
            "name": "start_campaign",
            "workflow": "BoCampaignWorkflow",
            "task_queue": "connector-bo",
            "summary": "Start a campaign.",
            "params_model": "bo.problem:CampaignSpec",
        }
    )
    from bo.problem import CampaignSpec

    assert build_job_tool("bo", referenced).__annotations__["params"] is CampaignSpec


def test_an_unresolvable_model_reference_fails_with_a_named_error() -> None:
    """Caught by `make connector-validate`, not when a chemist first calls the tool."""
    with pytest.raises(ConnectorJobError, match="cannot import"):
        resolve_params_model("no.such.module:Thing")
    with pytest.raises(ConnectorJobError, match="has no"):
        resolve_params_model("bo.problem:NotAThing")
    with pytest.raises(ConnectorJobError, match="not a pydantic model"):
        resolve_params_model("bo.problem:require_rounds_within_ceiling")


def test_launching_starts_the_declared_workflow_on_the_declared_queue(
    client: _FakeClient,
) -> None:
    """The manifest's `workflow`/`task_queue` are the only thing binding the run to a connector."""
    tool = build_job_tool("calc", _SPEC)
    job_id = _launch(tool, smiles="CCO")
    (call,) = client.calls
    payload: ConnectorJobInput = call["input"]
    assert payload.workflow == "CalculationWorkflow"
    assert payload.task_queue == "connector-calc"
    assert payload.connector == "calc" and payload.job == "run_calculation"
    assert payload.payload == {"smiles": "CCO"}  # the omitted optional param is not sent
    assert job_id == job_workflow_id("calc", "run_calculation", {"smiles": "CCO"})


def test_identical_arguments_produce_the_same_id_and_different_ones_do_not(
    client: _FakeClient,
) -> None:
    """The idempotency key (D-011): re-asking the same question must not pay for it twice."""
    tool = build_job_tool("calc", _SPEC)
    first = _launch(tool, smiles="CCO")
    again = _launch(tool, smiles="CCO")
    other = _launch(tool, smiles="CCC")
    assert first == again
    assert first != other
    # Argument *order* must not change the id — the hash is over canonical JSON, not a call
    # signature.
    assert _launch(tool, smiles="CCO", cycles=None) == first


def test_a_duplicate_submit_returns_the_existing_id_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`WorkflowAlreadyStartedError` is the contract succeeding: this exact job already exists."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    fake = _FakeClient(
        error=WorkflowAlreadyStartedError("already", "CalculationWorkflow", run_id=None)
    )

    async def _connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("connectors.jobs.connect", _connect)
    tool = build_job_tool("calc", _SPEC)
    token = begin_turn()
    try:
        job_id = _launch(tool, smiles="CCO")
        signals = drain()
    finally:
        end_turn(token)
    assert job_id == job_workflow_id("calc", "run_calculation", {"smiles": "CCO"})
    # Deliberately NOT announced as started: an already-finished run will never emit the
    # matching `job_completed` event, so the surface would show a row that stays "running"
    # forever.
    assert signals == []


def test_a_fresh_start_is_announced_to_the_streaming_turn(client: _FakeClient) -> None:
    """The launch reaches the UI immediately instead of silence until the push-back (D-042)."""
    tool = build_job_tool("calc", _SPEC)
    token = begin_turn()
    try:
        job_id = _launch(tool, smiles="CCO")
        signals = drain()
    finally:
        end_turn(token)
    assert signals == [JobSignal(job_id=job_id, kind="run_calculation")]


def test_dry_run_reports_what_it_would_do_and_starts_nothing(client: _FakeClient) -> None:
    """The ambient dry-run gate the three hand-written launchers have, applied by the factory."""
    tool = build_job_tool("calc", _SPEC)
    token = set_dry_run(True)
    try:
        answer = _launch(tool, smiles="CCO")
    finally:
        reset_dry_run(token)
    assert answer.startswith("DRY RUN")
    assert "smiles='CCO'" in answer
    assert client.calls == []  # nothing durable was started


def test_an_expensive_job_is_authorized_before_any_durable_work(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`authorize_trigger` fires for `expensive: true`, so a plan cannot outrun entitlements.

    Enforcement is only active under Entra, so the gate is switched on here with the same two
    config tokens a real deployment sets — asserting the wiring, not re-testing `agents.authz`.
    """
    expensive = JobSpec.model_validate({**_SPEC.model_dump(exclude_none=True), "expensive": True})
    tool = build_job_tool("calc", expensive)
    monkeypatch.setattr("chemclaw.config.settings.entra_required", True)
    monkeypatch.setattr("chemclaw.config.settings.entra_expensive_actions", "run_calculation")
    monkeypatch.setattr("chemclaw.config.settings.entra_privileged_roles", "hpc-operator")
    identity = set_current_identity("user-1", frozenset({"process-chemist"}))
    try:
        with pytest.raises(AuthorizationError):
            _launch(tool, smiles="CCO")
    finally:
        reset_current_identity(identity)
    assert client.calls == []


def test_durable_work_is_refused_without_an_authenticated_actor(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`require_actor` is the F4-T3 core rule: under Entra, no user means no durable job."""
    tool = build_job_tool("calc", _SPEC)
    monkeypatch.setattr("chemclaw.config.settings.entra_required", True)
    with pytest.raises(AuthorizationError, match="requires an authenticated user"):
        _launch(tool, smiles="CCO")
    assert client.calls == []


def test_the_run_is_attributed_to_the_turns_actor(client: _FakeClient) -> None:
    """Attribution travels in the payload, so an audit can name the user behind a durable run."""
    tool = build_job_tool("calc", _SPEC)
    identity = set_current_identity("user-7", frozenset())
    try:
        _launch(tool, smiles="CCO")
    finally:
        reset_current_identity(identity)
    payload: ConnectorJobInput = client.calls[0]["input"]
    assert payload.requested_by == "user-7"
