"""A generated job launcher is an ordinary tool — including everything that gates an ordinary tool.

The value of generating these is only real if the generated tool is not a special case, so most
of this file is about *sameness*: the schema the model sees is a proper typed one, the name is
the authorization key, the expensive-trigger gate and the dry-run gate both fire, an actor is
demanded before any durable work, and re-launching the identical job returns the existing id
instead of paying twice. Those are the five properties the four hand-written adapters had, now
asserted once against the factory that replaced them.

Temporal is faked at the client seam (`chemclaw.connectors.jobs`) because none of this is
about
Temporal's behavior — `test_connector_job_workflow.py` covers that against a real server. What
is under test here is what happens *before* the workflow starts, which is where the gates live.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from chemclaw.agent.authz import AuthorizationError, side_effecting_tools
from chemclaw.agent.tool_authz import DryRunRefusal, refuse_writes_on_dry_run
from chemclaw.agent.turn_flags import reset_dry_run, set_dry_run
from chemclaw.connectors.jobs import (
    ConnectorJobError,
    build_job_tool,
    job_workflow_id,
    resolve_params_model,
)
from chemclaw.connectors.manifest import JobSpec
from chemclaw.connectors.registry import enabled
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.turn_signals import JobSignal
from chemclaw.durable.connector_job import ConnectorJobInput
from tests.middleware import run_middleware, tool_request
from tests.signals import collect_signals

_SPEC = JobSpec.model_validate(
    {
        "name": "run_calculation",
        "workflow": "CalculationWorkflow",
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
    """A started-workflow handle, carrying the id the tool returns and the run's server status.

    `describe()` is what the rejoin path asks before announcing a run it did not start; the status
    is settable per test because the whole point of that branch is that a *running* rejoin and a
    *finished* one must be treated differently.
    """

    def __init__(self, workflow_id: str, status: Any = WorkflowExecutionStatus.RUNNING) -> None:
        self.id = workflow_id
        self.status = status
        self.described = 0

    async def describe(self) -> Any:
        """The one server round trip the rejoin path takes, counted so a test can pin "once"."""
        self.described += 1
        if isinstance(self.status, BaseException):
            raise self.status
        return SimpleNamespace(status=self.status)


class _FakeClient:
    """Records what `start_workflow` was called with, or raises a scripted error instead."""

    def __init__(self, error: Exception | None = None, status: Any = None) -> None:
        self.error = error
        self.status = WorkflowExecutionStatus.RUNNING if status is None else status
        self.calls: list[dict[str, Any]] = []
        self.handles: list[_FakeHandle] = []

    async def start_workflow(self, _run: Any, arg: Any, **kwargs: Any) -> _FakeHandle:
        """Capture the call; raise the scripted error if one was set (the duplicate-submit case)."""
        self.calls.append({"input": arg, **kwargs})
        if self.error is not None:
            raise self.error
        return _FakeHandle(str(kwargs["id"]))

    def get_workflow_handle(self, workflow_id: str, **_kwargs: Any) -> _FakeHandle:
        """The rejoin path a duplicate submit takes (sync in the real client too)."""
        handle = _FakeHandle(workflow_id, self.status)
        self.handles.append(handle)
        return handle


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Install a fake Temporal client behind the tool's `connect()` seam."""
    fake = _FakeClient()

    async def _connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    return fake


def _params(tool: Any, **values: Any) -> BaseModel:
    """Build the tool's generated params model from `values` — the *convenient* caller's shape.

    Deliberately not described as "what MAF does": it is not, and believing it was is what let
    every declared job ship broken (D-138). The two tests below drive the framework's real
    invocation path instead; this helper stays because most tests here are about the gates, not
    the argument shape, and a constructed model keeps those readable.
    """
    model: type[BaseModel] = tool.__annotations__["params"]
    return model(**values)


class _DryRunContext:
    """The slice of `FunctionInvocationContext` the dry-run gate touches."""

    def __init__(self, tool: str) -> None:
        self.function = SimpleNamespace(name=tool)
        self.arguments: dict[str, Any] = {}
        self.result: Any = None


def _launch(tool: Any, rationale: str = "why the tests run it", **values: Any) -> str:
    """Call the generated tool with `values` and a stated reason.

    A sync wrapper because the suite has no pytest-asyncio: each test drives one event loop
    through `asyncio.run`, which is the convention everywhere else here. The rationale has a
    default so the tests that are about something else stay about that; the ones that are about
    the reason itself (D-157) pass their own.
    """
    return str(asyncio.run(tool(_params(tool, **values), rationale)))


async def _alaunch(tool: Any, rationale: str = "why the tests run it", **values: Any) -> str:
    """`_launch` without the `asyncio.run`, for a caller that already owns the loop.

    Which is every test that also wants the tool's *signals*: those only exist while a graph is
    streaming (`tests/signals.collect_signals`), so the call has to happen inside that stream
    rather than in its own event loop.
    """
    return str(await tool(_params(tool, **values), rationale))


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
            "summary": "Start a campaign.",
            "params_model": "chemclaw.science.bo.problem:CampaignSpec",
        }
    )
    from chemclaw.science.bo.problem import CampaignSpec

    assert build_job_tool("bo", referenced).__annotations__["params"] is CampaignSpec


def test_an_unresolvable_model_reference_fails_with_a_named_error() -> None:
    """Caught by `make connector-validate`, not when a chemist first calls the tool."""
    with pytest.raises(ConnectorJobError, match="cannot import"):
        resolve_params_model("no.such.module:Thing")
    with pytest.raises(ConnectorJobError, match="has no"):
        resolve_params_model("chemclaw.science.bo.problem:NotAThing")
    with pytest.raises(ConnectorJobError, match="not a pydantic model"):
        resolve_params_model("chemclaw.science.bo.problem:require_rounds_within_ceiling")


def test_launching_starts_the_declared_workflow_on_the_bundles_own_queue(
    client: _FakeClient,
) -> None:
    """The workflow type name binds the run to a connector; the queue is derived from the bundle.

    `connector-calc` appears in no manifest (D-150) — it is `bundle_queue("calc")`, computed at
    dispatch. Asserting it on the launch payload is what keeps that derivation honest, since a
    wrong queue is not an error anywhere: the job would start and then wait forever.
    """
    tool = build_job_tool("calc", _SPEC)
    job_id = _launch(tool, smiles="CCO")
    (call,) = client.calls
    payload: ConnectorJobInput = call["input"]
    assert payload.workflow == "CalculationWorkflow"
    assert payload.task_queue == "connector-calc"
    assert payload.connector == "calc" and payload.job == "run_calculation"
    assert payload.payload == {"smiles": "CCO"}  # the omitted optional param is not sent
    assert job_id == job_workflow_id("calc", "run_calculation", {"smiles": "CCO"})


def test_launching_works_when_the_argument_arrives_as_the_raw_json_object(
    client: _FakeClient,
) -> None:
    """The shape the framework actually passes is a `dict`, and it must launch (D-138).

    Every other launch test in this file hands the tool a constructed model, which is why all of
    them passed while every declared job — `compute_reaction_energy`, `compare_solvents`,
    `start_optimization_campaign`, `sample_conformers` — failed on its first real use with
    `'dict' object has no attribute 'model_dump'`. The parameter's annotation is a pydantic model
    and its JSON schema is published, but the body is handed the decoded JSON object; nothing
    between the wire and the tool builds the model.
    """
    tool = build_job_tool("calc", _SPEC)
    job_id = str(asyncio.run(tool({"smiles": "CCO", "cycles": 3}, "why the tests run it")))
    (call,) = client.calls
    payload: ConnectorJobInput = call["input"]
    assert payload.payload == {"smiles": "CCO", "cycles": 3}
    assert job_id == job_workflow_id("calc", "run_calculation", {"smiles": "CCO", "cycles": 3})


def test_the_raw_object_is_validated_rather_than_passed_through(client: _FakeClient) -> None:
    """Accepting a dict must not mean accepting *any* dict — the schema still has to hold.

    The failure this guards against is the lazy repair: dropping the model and forwarding whatever
    arrived. The declared type would then be advertised to the model and enforced nowhere, and a
    mistyped argument would reach the workflow instead of the tool call.
    """
    tool = build_job_tool("calc", _SPEC)
    with pytest.raises(ValidationError):
        asyncio.run(tool({"smiles": "CCO", "cycles": "not-an-integer"}, "why the tests run it"))
    with pytest.raises(ValidationError):
        asyncio.run(tool({"cycles": 3}, "why the tests run it"))  # the required param is missing
    assert client.calls == []  # nothing durable was started on an invalid launch


def test_launching_survives_the_framework_s_own_invocation_path(client: _FakeClient) -> None:
    """End-to-end through the framework's own dispatcher, not our idea of it.

    The test above encodes today's observed behaviour (a `dict` arrives). This one encodes the
    property that actually matters and survives the framework changing its mind: whatever the
    dispatcher hands the body, a launch driven through it starts the declared workflow. It ran
    through MAF's `tool(...).invoke()` before the rebuild and runs through LangChain's
    `StructuredTool.ainvoke` now — the same question of the engine that is actually wired.
    """
    from langchain_core.tools import StructuredTool

    fn = build_job_tool("calc", _SPEC)
    invocable = StructuredTool.from_function(
        coroutine=fn, name=fn.__name__, description="Start a calculation."
    )
    asyncio.run(
        invocable.ainvoke(
            {"params": {"smiles": "CCO"}, "rationale": "confirm the reported barrier"}
        )
    )
    (call,) = client.calls
    payload: ConnectorJobInput = call["input"]
    assert payload.payload == {"smiles": "CCO"}
    # The reason travels the framework's path too, and stays *out* of the hashed payload.
    assert payload.rationale == "confirm the reported barrier"


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

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    tool = build_job_tool("calc", _SPEC)
    job_id, signals = asyncio.run(collect_signals(lambda: _alaunch(tool, smiles="CCO")))
    assert job_id == job_workflow_id("calc", "run_calculation", {"smiles": "CCO"})
    # A *running* rejoin is announced, and that is the second chemist's only route to the run:
    # see the two tests below for the pair this splits into.
    assert signals == [JobSignal(job_id=job_id, kind="run_calculation")]


def test_a_rejoined_run_that_is_still_going_is_announced_to_the_second_chemist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chemist B asks for a job chemist A already started, and hears about it (BACKLOG §3).

    The rejoin used to be silent in both directions, justified by "it may already be finished" —
    true of one case and false of the other, and the cost fell entirely on the second asker: no
    turn-stream `job_started`, therefore no `job_completed` a surface could match to it, and
    nothing for `agent/job_results.py` to wait on. They were told "in progress" and had to poll by
    hand forever. `describe()` answers the question the comment was guessing at.
    """
    fake = _FakeClient(
        error=WorkflowAlreadyStartedError("already", "CalculationWorkflow", run_id=None),
        status=WorkflowExecutionStatus.RUNNING,
    )

    async def _connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    tool = build_job_tool("calc", _SPEC)
    job_id, signals = asyncio.run(collect_signals(lambda: _alaunch(tool, smiles="CCO")))
    assert signals == [JobSignal(job_id=job_id, kind="run_calculation")]
    # One round trip, not one per branch: the describe is the *only* thing this path added.
    assert [handle.described for handle in fake.handles] == [1]


@pytest.mark.parametrize(
    "status",
    [
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.FAILED,
        WorkflowExecutionStatus.TERMINATED,
        RuntimeError("the broker dropped the describe"),
    ],
)
def test_a_rejoined_run_that_is_not_running_is_still_silent(
    monkeypatch: pytest.MonkeyPatch, status: Any
) -> None:
    """The half the old silence was right about, kept — including when the server will not say.

    A finished, failed or terminated run will never emit the `job_completed` that clears the row an
    announcement draws, so announcing one is worse than saying nothing. A describe that *fails* is
    the same case for a different reason: this is a best-effort question asked only to decide
    whether to speak, so an unanswered one must not become a tool error on a successful rejoin.

    Parametrized across all four because the previous version of this branch held one axis constant
    — every rejoin finished — which is why the running case went unnoticed for as long as it did.
    """
    fake = _FakeClient(
        error=WorkflowAlreadyStartedError("already", "CalculationWorkflow", run_id=None),
        status=status,
    )

    async def _connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    tool = build_job_tool("calc", _SPEC)
    job_id, signals = asyncio.run(collect_signals(lambda: _alaunch(tool, smiles="CCO")))
    assert job_id == job_workflow_id("calc", "run_calculation", {"smiles": "CCO"})
    assert signals == []


def test_a_generic_start_workflow_failure_is_framed_not_raised_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap one call after `connect()`: a launch fault must not reach MAF as a raw exception.

    `connect()` succeeding and `start_workflow` then failing (an unregistered task queue, a
    transient RPC timeout, a bad payload) is exactly the "Error: Function failed." symptom the
    `connect()` framing was written to prevent, one call later. Unlike a `connect()` failure, this
    cannot promise nothing started — so the message says only what it knows, and points at
    `get_durable_job_status` rather than overclaiming.
    """
    fake = _FakeClient(error=RuntimeError("task queue has no registered worker"))

    async def _connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    tool = build_job_tool("calc", _SPEC)
    with pytest.raises(ConnectorJobError, match="could not be confirmed as started") as excinfo:
        _launch(tool, smiles="CCO")
    message = str(excinfo.value)
    assert "run_calculation" in message
    assert "get_durable_job_status" in message
    assert "RuntimeError" in message


def test_a_duplicate_submit_is_unaffected_by_the_generic_failure_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`WorkflowAlreadyStartedError` must still resolve to the existing id, not the new framing.

    The generic `except Exception` added alongside it must sit *after* this one, or the
    idempotent-relaunch contract silently becomes an error.
    """
    fake = _FakeClient(
        error=WorkflowAlreadyStartedError("already", "CalculationWorkflow", run_id=None)
    )

    async def _connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    tool = build_job_tool("calc", _SPEC)
    job_id = _launch(tool, smiles="CCO")
    assert job_id == job_workflow_id("calc", "run_calculation", {"smiles": "CCO"})


def test_a_fresh_start_is_announced_to_the_streaming_turn(client: _FakeClient) -> None:
    """The launch reaches the UI immediately instead of silence until the push-back (D-042)."""
    tool = build_job_tool("calc", _SPEC)
    job_id, signals = asyncio.run(collect_signals(lambda: _alaunch(tool, smiles="CCO")))
    assert signals == [JobSignal(job_id=job_id, kind="run_calculation")]


def test_a_launch_with_no_ambient_session_does_not_crash(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI path: harness on, but no live `TurnSession` to hold a plan."""
    monkeypatch.setattr("chemclaw.core.config.settings.harness_enabled", True)
    tool = build_job_tool("calc", _SPEC)
    assert _launch(tool, smiles="CCO") == job_workflow_id(
        "calc", "run_calculation", {"smiles": "CCO"}
    )


def test_a_generated_launcher_is_covered_by_the_dry_run_gate() -> None:
    """A declared job is refused on a dry run without the factory checking for itself.

    The launcher used to test `is_dry_run()` in its own body. That worked and did not scale: every
    write the three hand-written checks did not cover ran on a `dry_run: true` turn, including the
    two that push a branch to the knowledge repository. The check now lives at the tool-invocation
    boundary over `side_effecting_tools()`, so what has to hold here is that a generated job's name
    is *in* that set — which it is by construction, since every declared job is durable work.
    """
    declared = {job.name for manifest in enabled() for job in manifest.jobs}
    assert declared, "no connector declares a job; this test would prove nothing"
    assert declared <= side_effecting_tools()

    ran = False

    async def _body() -> None:
        nonlocal ran
        ran = True

    async def _handler(_request: Any) -> Any:
        return await _body()

    token = set_dry_run(True)
    try:
        for job_name in sorted(declared):
            with pytest.raises(DryRunRefusal):
                asyncio.run(
                    run_middleware(refuse_writes_on_dry_run, tool_request(job_name), _handler)
                )
    finally:
        reset_dry_run(token)
    assert ran is False  # nothing durable was started


def test_an_expensive_job_is_authorized_before_any_durable_work(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`authorize_trigger` fires for `expensive: true`, so a plan cannot outrun entitlements.

    Enforcement is only active under Entra, so the gate is switched on here with the same two
    config tokens a real deployment sets — asserting the wiring, not re-testing
    `chemclaw.agent.authz`.
    """
    expensive = JobSpec.model_validate({**_SPEC.model_dump(exclude_none=True), "expensive": True})
    tool = build_job_tool("calc", expensive)
    monkeypatch.setattr("chemclaw.core.config.settings.entra_required", True)
    monkeypatch.setattr("chemclaw.core.config.settings.entra_expensive_actions", "run_calculation")
    monkeypatch.setattr("chemclaw.core.config.settings.entra_privileged_roles", "calc-operator")
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
    monkeypatch.setattr("chemclaw.core.config.settings.entra_required", True)
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


def test_the_run_is_stamped_with_the_plan_step_it_was_launched_for(client: _FakeClient) -> None:
    """The ambient plan link (D-2026-08-27) travels onto the input — never authored by the model."""
    from chemclaw.core.plan_context import reset_current_plan_link, set_current_plan_link

    tool = build_job_tool("calc", _SPEC)
    token = set_current_plan_link("run the conformer search", "plan-hash-1")
    try:
        _launch(tool, smiles="CCO")
    finally:
        reset_current_plan_link(token)
    payload: ConnectorJobInput = client.calls[0]["input"]
    assert payload.plan_step == "run the conformer search"
    assert payload.plan_hash == "plan-hash-1"


def test_a_launch_outside_any_plan_stamps_the_empty_link(client: _FakeClient) -> None:
    """A template step or CLI call says "not launched from a plan step", never a stale one."""
    tool = build_job_tool("calc", _SPEC)
    _launch(tool, smiles="CCO")
    payload: ConnectorJobInput = client.calls[0]["input"]
    assert payload.plan_step == ""
    assert payload.plan_hash == ""


# --- inline_wait_seconds: one tool for the fast and the slow case (D-114) ----------------


_INLINE_SPEC = JobSpec.model_validate(
    {
        **_SPEC.model_dump(exclude_none=True),
        "name": "compute_something",
        "inline_wait_seconds": 5,
    }
)


class _ResultHandle(_FakeHandle):
    """A handle whose `result()` resolves, hangs, or raises — the three outcomes of the wait."""

    def __init__(
        self, workflow_id: str, outcome: Any, status: Any = WorkflowExecutionStatus.RUNNING
    ) -> None:
        super().__init__(workflow_id, status)
        self.outcome = outcome

    async def result(self) -> Any:
        """Resolve to the envelope, raise the scripted failure, or never finish."""
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is None:
            await asyncio.Event().wait()  # still running when the budget expires
        return self.outcome


class _ResultClient(_FakeClient):
    """A client whose started (or existing) workflow has a scripted `result()`."""

    def __init__(self, outcome: Any, error: Exception | None = None, status: Any = None) -> None:
        super().__init__(error, status)
        self.outcome = outcome
        self.rejoined: list[str] = []

    async def start_workflow(self, _run: Any, arg: Any, **kwargs: Any) -> _ResultHandle:
        """Record the launch, then behave as `_FakeClient` does but with a result-bearing handle."""
        self.calls.append({"input": arg, **kwargs})
        if self.error is not None:
            raise self.error
        return _ResultHandle(str(kwargs["id"]), self.outcome)

    def get_workflow_handle(self, workflow_id: str, **_kwargs: Any) -> _ResultHandle:
        """The rejoin path a duplicate submit takes (sync in the real client too)."""
        self.rejoined.append(workflow_id)
        handle = _ResultHandle(workflow_id, self.outcome, self.status)
        self.handles.append(handle)
        return handle


def _install(monkeypatch: pytest.MonkeyPatch, client: _ResultClient) -> _ResultClient:
    """Put `client` behind the tool's `connect()` seam."""

    async def _connect() -> _ResultClient:
        return client

    monkeypatch.setattr("chemclaw.connectors.jobs.connect", _connect)
    return client


_ENVELOPE = {"summary": "Computed it.", "data": {"value": 1.5}}


def test_a_quick_job_returns_its_result_inside_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the whole mechanism: fast work is an answer, not a job id to poll.

    Before this, a tool decided by *predicting* its cost, which is what kept the chemistry cost
    model — and so the capability's dependency closure — inside the agent's process.
    """
    fake = _install(monkeypatch, _ResultClient(_ENVELOPE))
    tool = build_job_tool("calc", _INLINE_SPEC)
    result = asyncio.run(tool(_params(tool, smiles="CCO"), "why the tests run it"))
    assert result.summary == "Computed it."
    assert result.data == {"value": 1.5}
    assert len(fake.calls) == 1  # it really did start a durable run


def test_a_slow_job_falls_back_to_a_job_id_and_announces_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the budget the tool hands back an id — and only then is the job announced.

    The announcement is the tell that the two paths are genuinely different: a run that answered
    inside the turn has no background work for the surface to show as pending.
    """
    fake = _install(monkeypatch, _ResultClient(None))  # never finishes
    spec = JobSpec.model_validate(
        {**_INLINE_SPEC.model_dump(exclude_none=True), "inline_wait_seconds": 0.05}
    )
    tool = build_job_tool("calc", spec)
    result, published = asyncio.run(
        collect_signals(lambda: tool(_params(tool, smiles="CCO"), "why the tests run it"))
    )
    signals = [signal for signal in published if isinstance(signal, JobSignal)]
    assert result == fake.calls[0]["id"]
    assert [s.job_id for s in signals] == [fake.calls[0]["id"]]


def test_a_failed_job_raises_rather_than_degrading_to_a_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction that matters: "not finished" is a job id, "failed" is an error.

    Swallowing the failure would hand back an id for a run that is already dead, and the chemist
    would poll a corpse.
    """
    _install(monkeypatch, _ResultClient(RuntimeError("the SCF diverged")))
    tool = build_job_tool("calc", _INLINE_SPEC)
    with pytest.raises(RuntimeError, match="SCF diverged"):
        asyncio.run(tool(_params(tool, smiles="CCO"), "why the tests run it"))


def test_re_asking_a_finished_job_returns_its_result_not_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeat of a cheap calculation should feel like a cache hit, which is what it is.

    The idempotent id means the second ask hits `WorkflowAlreadyStartedError`; rejoining the
    finished run is what turns that into an answer rather than a poll.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError

    already = WorkflowAlreadyStartedError("exists", "CalculationWorkflow", run_id=None)
    fake = _install(monkeypatch, _ResultClient(_ENVELOPE, error=already))
    tool = build_job_tool("calc", _INLINE_SPEC)
    result = asyncio.run(tool(_params(tool, smiles="CCO"), "why the tests run it"))
    assert result.summary == "Computed it."
    assert fake.rejoined == [job_workflow_id("calc", "compute_something", {"smiles": "CCO"})]


def test_a_job_without_the_budget_still_returns_only_an_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opting out is the default: no `inline_wait_seconds` means no wait and no behaviour change."""
    fake = _install(monkeypatch, _ResultClient(_ENVELOPE))
    tool = build_job_tool("calc", _SPEC)  # the spec without the budget
    result = asyncio.run(tool(_params(tool, smiles="CCO"), "why the tests run it"))
    assert result == fake.calls[0]["id"]


def test_a_launch_must_say_why_it_is_being_started(client: _FakeClient) -> None:
    """A durable run with no recorded reason is the gap D-157 closes; blank must not slip past.

    Refused *before* `start_workflow`, so the correction costs nothing: the model reads the error
    and re-calls with a reason in the same turn, and no expensive run was started meanwhile.
    """
    tool = build_job_tool("calc", _SPEC)
    for blank in ("", "   ", "\n"):
        with pytest.raises(ConnectorJobError, match="must say why"):
            asyncio.run(tool(_params(tool, smiles="CCO"), blank))
    assert client.calls == []


def test_the_reason_reaches_the_run_without_entering_its_identity(client: _FakeClient) -> None:
    """The rationale is recorded, and two differently-worded asks for one job stay one run.

    The second half is why it is not in `payload`: the id hashes the payload, so a reason folded
    in there would turn "the same campaign, explained differently" into a second expensive run
    (D-011).
    """
    tool = build_job_tool("calc", _SPEC)
    first = _launch(tool, "the reviewer questioned the barrier", smiles="CCO")
    second = _launch(tool, "sanity-checking last week's number", smiles="CCO")
    assert first == second
    assert [call["input"].rationale for call in client.calls] == [
        "the reviewer questioned the barrier",
        "sanity-checking last week's number",
    ]
    # Stored trimmed, so trailing whitespace from a model's formatting is not part of the record.
    _launch(tool, "  padded reason  ", smiles="CCC")
    assert client.calls[-1]["input"].rationale == "padded reason"


def test_the_model_is_told_what_the_reason_is_for() -> None:
    """The docstring is the only place the model learns what to put there, so it must say."""
    doc = build_job_tool("calc", _SPEC).__doc__ or ""
    assert "rationale:" in doc
    assert "do not restate the arguments" in doc.lower()
