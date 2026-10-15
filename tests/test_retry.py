"""Every activity is retried under the class it declared, and every class is bounded.

An activity states an `ActivityClass`; `durable/retry.py` turns it into a `RetryPolicy`; every
dispatch passes `retry_for(that activity)`. The bad-data list is non-retryable by exact class name
(Temporal matches by name), every class but `QUEUED_TOOL` is bounded, and a real dev server proves
the two behaviours that matter: a bad-data failure costs one attempt, and a transient one is
retried to the class's bound and then fails with its reason.
"""

import ast
import asyncio
import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity, workflow
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chemclaw.agent.authz import AuthorizationError
from chemclaw.agent.profile_discovery import ProfileError
from chemclaw.connectors.contract import ContractMismatch
from chemclaw.connectors.registry import ConnectorError
from chemclaw.core.config import Settings, settings
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.durable.registry import ActivityClass, activity_class_of, activity_in_class
from chemclaw.durable.retry import (
    _BAD_DATA_TYPES,
    _DECLARED_RETRYABLE,
    child_workflow_once,
    child_workflow_retry,
    policy_for,
    retry_for,
)
from chemclaw.ingest.sources.registry import DataSourceError
from chemclaw.templates.registry import TemplateError
from chemclaw.templates.resolve import UnresolvedReference
from tests.temporal_env import pydantic_client, start_local_env_or_skip

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

# The bounded classes: every one gives up after a number of attempts.
_BOUNDED = [one for one in ActivityClass if one is not ActivityClass.QUEUED_TOOL]


def _import_first_party_tree() -> None:
    """Import every module under `chemclaw` so every subclass of an error base is defined.

    Shared by both completeness walks below (`ChemclawError`'s and `AuthorizationError`'s): a
    subclass declared in a module nobody has imported yet is invisible to `__subclasses__()`.
    """
    package = importlib.import_module("chemclaw")
    for module_info in pkgutil.walk_packages(package.__path__, prefix="chemclaw."):
        importlib.import_module(module_info.name)


def test_bad_data_retry_lists_every_bad_data_type_by_name() -> None:
    """Every bad-data error name crossing an activity boundary is non-retryable.

    Includes pydantic's `ValidationError` (a `ValueError` subclass with its own class name)
    and the ORD/eval format errors, which were previously missing and so retried.
    """
    names = set(_BAD_DATA_TYPES)
    assert {
        "ValueError",
        "ValidationError",
        "ChemclawError",
        "OrdFormatError",
        "NoteError",
        "EvalCaseError",
    } <= names


def test_every_chemclaw_error_subclass_is_listed_non_retryable() -> None:
    """Every `ChemclawError` subclass is listed non-retryable by name.

    Temporal matches by exact class name, not `isinstance`, so a subclass is not covered until its
    own name is in `_BAD_DATA_TYPES`. Walks every first-party module so all subclasses are defined.
    """
    # `walk_packages` reaches every module under `chemclaw` (D-148), so a new subclass anywhere
    # in the tree is still defined by the time the assertion runs.
    _import_first_party_tree()

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    registered = set(_BAD_DATA_TYPES) | _DECLARED_RETRYABLE

    missing = names(ChemclawError) - registered
    assert not missing, f"ChemclawError subclasses not registered in _BAD_DATA_TYPES: {missing}"
    # And an exemption must never also be listed — a name in both sets is a contradiction the
    # policy would resolve silently (the list wins, and the "retryable" claim becomes false).
    assert not _DECLARED_RETRYABLE & set(_BAD_DATA_TYPES)
    # Nor may the list name a class twice: a duplicate changes no behaviour, so it survives review,
    # and a classification register with two entries for one class has two answers for it.
    duplicated = sorted({name for name in _BAD_DATA_TYPES if _BAD_DATA_TYPES.count(name) > 1})
    assert not duplicated, f"_BAD_DATA_TYPES lists {duplicated} more than once"


def test_every_authorization_error_subclass_is_listed_non_retryable() -> None:
    """Every `AuthorizationError` subclass is listed non-retryable.

    `AuthorizationError` is deliberately not a `ChemclawError`, so the walk above never visits it or
    its subclasses (`DryRunRefusal`, `PlanNotApprovedError`).
    """

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    _import_first_party_tree()
    missing = names(AuthorizationError) - set(_BAD_DATA_TYPES)
    assert not missing, f"AuthorizationError subclasses not registered: {missing}"


def test_no_subsystem_outage_error_is_listed_non_retryable() -> None:
    """No `SubsystemUnavailableError` is listed non-retryable, on purpose.

    It means the infrastructure is not answering, the failure a retry fixes; registering it would
    make workflows give up on a broker restart. The walks above fail on a missing subclass, so this
    fails on a present one, with the reason in the message.
    """
    _import_first_party_tree()

    def names(cls: type) -> set[str]:
        return {cls.__name__}.union(*(names(sub) for sub in cls.__subclasses__()), set())

    listed = set(_BAD_DATA_TYPES)
    registered = names(SubsystemUnavailableError) & listed
    assert not registered, (
        f"{registered} is registered non-retryable, but an unreachable subsystem is retryable by "
        "definition — a retry is the fix, not a wasted attempt. If a genuinely non-retryable error "
        "needs a home, it does not belong under SubsystemUnavailableError."
    )


@pytest.mark.parametrize(
    "error_cls",
    [
        ConnectorError,
        ContractMismatch,
        DataSourceError,
        TemplateError,
        UnresolvedReference,
        ProfileError,
        AuthorizationError,
    ],
)
def test_bad_data_class_crosses_an_activity_boundary_as_non_retryable(
    error_cls: type[Exception],
) -> None:
    """A bad-data class crosses an activity boundary as non-retryable.

    `DefaultFailureConverter` sets `ApplicationError.type` to the exact class name, never an
    ancestor's. `AuthorizationError` is a plain `Exception`, yet registered by name it is classified
    the same way. Drives the real SDK converter (no server needed) rather than inspecting the class
    hierarchy.
    """
    converter = DefaultFailureConverter()
    payload_converter = DefaultPayloadConverter()
    failure = Failure()

    converter.to_failure(error_cls("boom"), payload_converter, failure)

    assert failure.application_failure_info.type == error_cls.__name__
    assert error_cls.__name__ in _BAD_DATA_TYPES


def test_the_note_write_policy_shares_the_bad_data_types() -> None:
    """A bad note fails fast rather than burning the bounded note-write retries."""
    policy = policy_for(ActivityClass.NOTE_WRITE)
    assert policy.maximum_attempts == settings.note_write_max_attempts
    assert "NoteError" in (policy.non_retryable_error_types or [])


def test_the_governed_bound_is_narrower_than_the_shared_ones() -> None:
    """The governed retry bound is narrower than every other class's.

    An agent step replays the whole turn, re-running every tool the failed attempt already ran, side
    effects included. Strictly less than the other bounded classes, so equal settings cannot undo
    the narrowing. The bad-data list is shared: the policies differ in how many transient attempts,
    never in which failures are transient.
    """
    policy = policy_for(ActivityClass.GOVERNED)

    assert policy.maximum_attempts == settings.agent_step_max_attempts
    for other in (
        ActivityClass.IO,
        ActivityClass.REMOTE,
        ActivityClass.COMPUTE,
        ActivityClass.CALC,
    ):
        assert (policy.maximum_attempts or 0) < (policy_for(other).maximum_attempts or 0)
        assert policy.non_retryable_error_types == policy_for(other).non_retryable_error_types


def test_the_calculation_retry_waits_out_a_full_backend_without_spinning_at_it() -> None:
    """The calculation retry waits out a full backend without spinning at it.

    `CalcBusyError` is retryable, but a slot is held for a whole calculation. The cap is one
    `calc_server_timeout_seconds` and the first interval is that cap divided by the doublings the
    attempt budget allows, so the last retry waits one calculation. The type list matches
    the other classes'.
    """
    # Called outside a workflow, so the schedule is the nominal one — the jitter is per-run and
    # has no run here. `test_two_runs_refused_together_do_not_come_back_together` drives that half.
    policy = policy_for(ActivityClass.CALC)

    assert policy.maximum_attempts == settings.activity_max_attempts
    assert policy.non_retryable_error_types == list(_BAD_DATA_TYPES)
    assert policy.backoff_coefficient == 2.0

    cap = timedelta(seconds=settings.calc_server_timeout_seconds)
    assert policy.maximum_interval == cap
    # The whole schedule, rebuilt the way Temporal computes it, so the assertion is about elapsed
    # patience rather than about one field. At the shipped defaults: 112.5 s, 225 s, 450 s, 900 s.
    waits = []
    interval = policy.initial_interval or timedelta()
    for _ in range((policy.maximum_attempts or 1) - 1):
        waits.append(min(interval, cap))
        interval *= policy.backoff_coefficient
    assert waits[-1] == cap, "the last retry must wait a whole calculation, not a fraction of one"
    # Nineteen minutes is the measured CREST search this has to outlast.
    total = sum(waits, timedelta())
    assert total > timedelta(minutes=19)
    # The slack is measured against the composite, not one attempt: on the path these retries exist
    # for, a job waits for a slot, is refused at capacity, backs off and then runs, all within the
    # parent ceiling (`D-2026-09-05-a-refusal-for-capacity-is-not-a-refusal-of-the-question`).
    longest, _ = settings.longest_bundle_activity
    composite = timedelta(hours=1.98) + total + timedelta(seconds=longest)
    assert composite < timedelta(seconds=settings.connector_job_timeout_seconds), (
        "a job that queues, is refused for capacity, backs off and then runs must still finish "
        "inside the ceiling its parent gives it"
    )


@workflow.defn
class _RetrySpacingWorkflow:
    """Report the first retry interval this run would use, from inside a real workflow context."""

    @workflow.run
    async def run(self, activity_class: str) -> float:
        """The first interval, in seconds — jittered per run for the classes that spread."""
        interval = policy_for(ActivityClass(activity_class)).initial_interval
        return interval.total_seconds() if interval else 0.0


def test_runs_refused_together_do_not_come_back_together_where_a_dependency_is_shared() -> None:
    """Runs refused together do not come back together, for the classes that lean on a dependency.

    Temporal applies no jitter of its own, and a burst refused at once would otherwise retry in
    lockstep. The calculation backend and a remote dependency spread (two runs differ, only
    downward, since upward jitter would be swallowed by `maximum_interval`); a sweep that owns its
    work and a Postgres write keep the nominal interval.
    """
    nominal = {
        ActivityClass.CALC: settings.calc_server_timeout_seconds
        / 2 ** max(settings.activity_max_attempts - 2, 0),
        ActivityClass.REMOTE: settings.slow_retry_initial_seconds,
        ActivityClass.COMPUTE: settings.slow_retry_initial_seconds,
        ActivityClass.IO: settings.io_retry_initial_seconds,
    }

    async def _run() -> dict[ActivityClass, list[float]]:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="retry-spacing",
                workflows=[_RetrySpacingWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                return {
                    one: [
                        await client.execute_workflow(
                            _RetrySpacingWorkflow.run,
                            one.value,
                            id=f"spacing-{one.value}-{n}",
                            task_queue="retry-spacing",
                        )
                        for n in range(2)
                    ]
                    for one in nominal
                }

    drawn = asyncio.run(_run())

    for spread in (ActivityClass.CALC, ActivityClass.REMOTE):
        first, second = drawn[spread]
        assert first != second, f"{spread}: two runs drew the same interval; not per-run"
        for seconds in (first, second):
            assert nominal[spread] * 0.75 <= seconds <= nominal[spread]
    for steady in (ActivityClass.COMPUTE, ActivityClass.IO):
        assert drawn[steady] == [nominal[steady]] * 2


def test_no_provider_transient_name_is_listed_non_retryable() -> None:
    """No LLM provider transient error name is listed non-retryable.

    Those failures succeed on retry; `agent_step_max_attempts` is the lever for duplicate turns.
    `_BAD_DATA_TYPES` matches bare class names and is shared by every activity, so one entry would
    reclassify these failures across unrelated workflows too.
    """
    provider_transient = {
        "InternalServerError",
        "OverloadedError",
        "APIConnectionError",
        "RateLimitError",
    }

    listed = provider_transient & set(_BAD_DATA_TYPES)

    assert not listed, (
        f"{listed} is registered non-retryable, but a provider 503/429/connection failure is "
        "transient by definition — the identical call succeeds once the provider recovers. If this "
        "was added to stop a retried agent step duplicating notes, the lever is "
        "agent_step_max_attempts, not the bad-data list, which every other activity shares."
    )


@pytest.mark.parametrize("activity_class", _BOUNDED)
def test_every_class_but_the_queued_tool_is_bounded_and_refuses_bad_data(
    activity_class: ActivityClass,
) -> None:
    """A deterministic failure gives up instead of pinning a worker or hammering a dependency.

    Bounded means a number of attempts, never Temporal's unlimited default; refusing bad data means
    the one shared list, so no class has its own idea of which failures are transient.
    """
    policy = policy_for(activity_class)

    assert policy.maximum_attempts >= 1, f"{activity_class} would retry without end"
    assert policy.non_retryable_error_types == list(_BAD_DATA_TYPES)


def test_the_queued_tool_is_the_one_unbounded_class_and_its_dispatch_is_bounded_in_time() -> None:
    """A full server is asked again until the call's own deadline, so that deadline must exist.

    The only class without an attempt bound; every dispatch of such an activity must therefore pass
    `schedule_to_close_timeout`, which ends the retrying whatever the server does.
    """
    policy = policy_for(ActivityClass.QUEUED_TOOL)
    assert policy.maximum_attempts == 0
    assert policy.non_retryable_error_types == ["QueuedToolFault"]

    unbounded = {
        name for (_, name), declared in _declared_classes().items() if declared == "QUEUED_TOOL"
    }
    unbounded_sites = 0
    for where, tree in _source_trees():
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and _is_dispatch(call)):
                continue
            activity_arg = _activity_argument(call)
            if isinstance(activity_arg, ast.Name) and activity_arg.id in unbounded:
                unbounded_sites += 1
                assert "schedule_to_close_timeout" in _keywords(call), (
                    f"{where}:{call.lineno} dispatches an activity with no attempt bound and no "
                    "`schedule_to_close_timeout`, so nothing ever ends its retrying"
                )
    assert unbounded_sites, "no dispatch of a QUEUED_TOOL activity found; the walk reads nothing"


@pytest.mark.parametrize("activity_class", list(ActivityClass))
def test_every_policy_is_one_the_server_accepts(activity_class: ActivityClass) -> None:
    """The first interval never exceeds the longest, which the server refuses.

    From workflow code a refused policy is a failed workflow task, retried without end.
    """
    policy = policy_for(activity_class)
    assert policy.initial_interval is not None
    assert policy.maximum_interval is not None
    assert timedelta() < policy.initial_interval <= policy.maximum_interval
    assert policy.backoff_coefficient >= 1.0


def _waits(activity_class: ActivityClass) -> list[timedelta]:
    """The pauses between attempts, rebuilt the way Temporal computes them."""
    policy = policy_for(activity_class)
    cap = policy.maximum_interval or timedelta()
    waits = []
    interval = policy.initial_interval or timedelta()
    for _ in range((policy.maximum_attempts or 1) - 1):
        waits.append(min(interval, cap))
        interval *= policy.backoff_coefficient
    return waits


def test_the_io_class_rides_out_a_database_failover_without_waiting_out_a_calculation() -> None:
    """Many attempts, short and capped: about a minute and a half of patience, all of it brisk."""
    policy = policy_for(ActivityClass.IO)
    waits = _waits(ActivityClass.IO)

    assert policy.maximum_attempts == settings.io_retry_max_attempts
    assert (policy.maximum_attempts or 0) > settings.activity_max_attempts
    assert policy.initial_interval == timedelta(seconds=settings.io_retry_initial_seconds)
    assert max(waits) == timedelta(seconds=settings.io_retry_max_interval_seconds)
    assert sum(waits, timedelta()) >= timedelta(seconds=60)


@pytest.mark.parametrize("activity_class", [ActivityClass.REMOTE, ActivityClass.COMPUTE])
def test_the_slow_classes_back_off_from_seconds_to_minutes(activity_class: ActivityClass) -> None:
    """Few attempts, long pauses: a dependency that is down is not asked again every second."""
    policy = policy_for(activity_class)
    waits = _waits(activity_class)

    assert policy.maximum_attempts == settings.activity_max_attempts
    assert policy.initial_interval == timedelta(seconds=settings.slow_retry_initial_seconds)
    assert policy.maximum_interval == timedelta(seconds=settings.slow_retry_max_interval_seconds)
    assert waits[0] > timedelta(seconds=settings.io_retry_initial_seconds)
    assert sum(waits, timedelta()) >= timedelta(minutes=2)


def test_the_child_workflow_policies_are_the_two_the_fan_out_and_the_job_wrapper_use() -> None:
    """A fan-out child is bounded like an activity; a job's child gets one attempt."""
    assert child_workflow_retry().maximum_attempts == settings.activity_max_attempts
    assert child_workflow_retry().non_retryable_error_types == list(_BAD_DATA_TYPES)
    assert child_workflow_once().maximum_attempts == 1


def test_a_first_interval_longer_than_the_longest_is_refused_at_startup() -> None:
    """A mistake in two settings fails the process, not a workflow task, endlessly."""
    with pytest.raises(ValueError, match="io_retry_initial_seconds"):
        Settings(io_retry_initial_seconds=90.0, io_retry_max_interval_seconds=30.0)
    with pytest.raises(ValueError, match="slow_retry_initial_seconds"):
        Settings(slow_retry_initial_seconds=900.0, slow_retry_max_interval_seconds=300.0)


def test_a_fan_out_ceiling_that_holds_one_attempt_but_not_its_retries_is_refused() -> None:
    """The ceiling must contain every attempt of the longest fan-out activity, not just one.

    At 1500 s it clears the one-attempt rule (300 s section plus 30 s overhead) but a section that
    fails all five attempts takes 5 x 300 s plus 10 + 20 + 40 + 80 s of backoff, 1650 s, and the
    child would die as an execution timeout no workflow code sees.
    """
    shortest = settings.fan_out_child_timeout_seconds
    assert shortest > 1650 + settings.activity_timeout_seconds, "the shipped ceiling covers it"
    with pytest.raises(ValueError, match="cover the retries"):
        Settings(fan_out_child_timeout_seconds=1500.0)
    Settings(fan_out_child_timeout_seconds=1700.0)


# --- every activity declares a class, and every dispatch uses it ------------------------------

# Every SDK call that puts an activity on a queue or runs it as a local activity, including the
# `_method`/`_class` forms for class-bound activities.
_DISPATCH_NAMES = {
    "execute_activity",
    "execute_activity_method",
    "execute_activity_class",
    "start_activity",
    "start_activity_method",
    "start_activity_class",
    "execute_local_activity",
    "start_local_activity",
}

# The sites the walk found when this was written. A walk that silently stops finding them would
# pass vacuously, so the floor is the count, and removing a dispatch means lowering it here.
_DISPATCH_SITES_FLOOR = 72


def _source_trees() -> list[tuple[str, ast.Module]]:
    """Every first-party module, parsed, by its path under `src/chemclaw`."""
    return [
        (path.relative_to(_SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_SRC.rglob("*.py"))
    ]


def _is_dispatch(call: ast.Call) -> bool:
    """Whether `call` schedules an activity.

    An interceptor's `self.next.execute_activity(...)` or `self.next.start_activity(...)` delegates
    an activity somebody else already scheduled, with the policy they gave it.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _DISPATCH_NAMES
    if not isinstance(func, ast.Attribute) or func.attr not in _DISPATCH_NAMES:
        return False
    return ast.unparse(func.value) != "self.next"


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    """The keyword arguments of `call` by name."""
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _activity_argument(call: ast.Call) -> ast.expr | None:
    """The expression naming the activity a dispatch schedules."""
    return call.args[0] if call.args else _keywords(call).get("activity")


def _dispatch_offences(tree: ast.Module, where: str) -> tuple[int, list[str]]:
    """The dispatches in `tree` and those whose policy is not `retry_for(<their activity>)`.

    Returns:
        The number of dispatches found, and one message per offender.
    """
    sites = 0
    offences = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and _is_dispatch(call)):
            continue
        sites += 1
        activity_arg = _activity_argument(call)
        policy = _keywords(call).get("retry_policy")
        accepted = (
            activity_arg is not None
            and isinstance(policy, ast.Call)
            and isinstance(policy.func, ast.Name)
            and policy.func.id == "retry_for"
            and len(policy.args) == 1
            and ast.dump(policy.args[0]) == ast.dump(activity_arg)
        )
        if not accepted:
            dispatched = ast.unparse(activity_arg) if activity_arg else "?"
            given = ast.unparse(policy) if policy else "nothing (the SDK default)"
            offences.append(
                f"{where}:{call.lineno} dispatches {dispatched} with retry_policy={given}"
            )
    return sites, offences


# The calls that start a child workflow: a child given no policy runs on the server's default.
_CHILD_NAMES = {"execute_child_workflow", "start_child_workflow"}
_CHILD_POLICIES = {"child_workflow_retry", "child_workflow_once"}


def _call_name(call: ast.Call) -> str:
    """The bare name a call is made through: `workflow.execute_activity(...)` is that name."""
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _enclosing_parameters(tree: ast.Module) -> dict[int, frozenset[str]]:
    """For every node, the parameter names of the function it sits in."""
    found: dict[int, frozenset[str]] = {}

    def visit(node: ast.AST, parameters: frozenset[str]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = parameters
            if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef):
                arguments = child.args
                inner = frozenset(
                    one.arg
                    for one in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
                )
            found[id(child)] = inner
            visit(child, inner)

    visit(tree, frozenset())
    return found


def _states_a_policy(
    call: ast.Call, enclosing: dict[int, frozenset[str]], allowed: set[str]
) -> bool:
    """Whether `call` passes `retry_policy=` as a call to one of `allowed`, or forwards its own.

    A variable called `retry_policy` counts only inside a function that takes a parameter of that
    name (the helper that forwards its caller's), so an arbitrary variable cannot pass.
    """
    policy = _keywords(call).get("retry_policy")
    if isinstance(policy, ast.Call):
        return _call_name(policy) in allowed
    return (
        isinstance(policy, ast.Name)
        and policy.id == "retry_policy"
        and "retry_policy" in enclosing.get(id(call), frozenset())
    )


def _policy_offences(tree: ast.Module, where: str) -> list[str]:
    """A policy built outside `retry.py`, and a `retry_policy=` that is not one of its calls."""
    if where == "durable/retry.py":
        return []
    enclosing = _enclosing_parameters(tree)
    allowed = {"retry_for"} | _CHILD_POLICIES
    offences = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if _call_name(call) == "RetryPolicy":
            offences.append(f"{where}:{call.lineno} constructs a RetryPolicy")
        policy = _keywords(call).get("retry_policy")
        if policy is not None and not _states_a_policy(call, enclosing, allowed):
            offences.append(f"{where}:{call.lineno} passes retry_policy={ast.unparse(policy)}")
    return offences


def _child_offences(tree: ast.Module, where: str) -> list[str]:
    """Child-workflow starts that state neither of the two child policies nor a forwarded one."""
    enclosing = _enclosing_parameters(tree)
    offences = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and _call_name(call) in _CHILD_NAMES):
            continue
        if isinstance(call.func, ast.Attribute) and ast.unparse(call.func.value) == "self.next":
            continue  # an interceptor forwarding a child its workflow already described
        if not _states_a_policy(call, enclosing, _CHILD_POLICIES):
            policy = _keywords(call).get("retry_policy")
            given = ast.unparse(policy) if policy else "nothing (the server's default)"
            offences.append(f"{where}:{call.lineno} starts a child workflow with {given}")
    return offences


def _hole_offences(tree: ast.Module, where: str) -> list[str]:
    """Ways to start an activity or child that the call walks above cannot see.

    An import under another name, a `getattr` with the name as a string, and the name used as a
    value (assigned, passed on) instead of called.
    """
    watched = _DISPATCH_NAMES | _CHILD_NAMES
    called = {id(call.func) for call in ast.walk(tree) if isinstance(call, ast.Call)}
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in watched and alias.asname:
                    offences.append(
                        f"{where}:{node.lineno} imports {alias.name} as {alias.asname}, so its "
                        "calls cannot be recognised"
                    )
        elif isinstance(node, ast.Call):
            if (
                _call_name(node) == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in watched
            ):
                offences.append(f"{where}:{node.lineno} reaches {node.args[1].value} by getattr")
        elif isinstance(node, ast.Attribute | ast.Name) and id(node) not in called:
            name = node.attr if isinstance(node, ast.Attribute) else node.id
            if name in watched and isinstance(node.ctx, ast.Load):
                offences.append(f"{where}:{node.lineno} uses {name} as a value, not a call")
    return offences


def _all_offences(tree: ast.Module, where: str) -> tuple[int, list[str]]:
    """Every rule about how work is started and retried, over one module.

    Returns:
        The number of activity dispatches found, and one message per offence of any rule.
    """
    sites, offences = _dispatch_offences(tree, where)
    return sites, [
        *offences,
        *_policy_offences(tree, where),
        *_child_offences(tree, where),
        *_hole_offences(tree, where),
    ]


def test_every_dispatch_passes_the_policy_of_the_activity_it_dispatches() -> None:
    """No dispatch leans on the SDK's unlimited default or spells a policy of its own.

    `retry_policy=retry_for(<the activity dispatched>)` at every activity site and one of the two
    child policies at every child start, so the class an activity declares is the class it is
    retried under, a policy cannot be copied onto the wrong one, and nothing is started in a way
    the walk cannot see. A policy is spelled once, in `retry.py`.
    """
    total = 0
    offences: list[str] = []
    for where, tree in _source_trees():
        sites, found = _all_offences(tree, where)
        total += sites
        offences.extend(found)

    assert total >= _DISPATCH_SITES_FLOOR, f"the walk found only {total} dispatch sites"
    assert not offences, "work started outside the retry classes:\n" + "\n".join(offences)


def _offences_of(source: str) -> list[str]:
    """Every rule over a snippet nobody wrote, as if it were a module under `src`."""
    return _all_offences(ast.parse(source), "probe")[1]


_TEMPLATE = "async def run():\n    await workflow.{call}({args})\n"

# Each spelling the walk could fall through, and the snippet that uses it.
_REFUSED = {
    "the SDK default": _TEMPLATE.format(call="execute_activity", args="act"),
    "a literal policy": _TEMPLATE.format(
        call="execute_activity", args="act, retry_policy=RetryPolicy(maximum_attempts=1)"
    ),
    "a class helper": _TEMPLATE.format(
        call="start_activity", args="act, retry_policy=policy_for(ActivityClass.IO)"
    ),
    "another activity's policy": _TEMPLATE.format(
        call="execute_local_activity", args="act, retry_policy=retry_for(other)"
    ),
    "a retired name": _TEMPLATE.format(
        call="execute_activity", args="activity=act, retry_policy=BAD_DATA_RETRY"
    ),
    "an aliased import": (
        "from temporalio.workflow import execute_activity as ea\n"
        "async def run():\n    await ea(act, retry_policy=retry_for(act))\n"
    ),
    "a getattr": (
        "async def run():\n"
        "    await getattr(workflow, 'execute_activity')(act, retry_policy=retry_for(act))\n"
    ),
    "a name used as a value": (
        "async def run():\n    go = workflow.execute_activity\n"
        "    await go(act, retry_policy=retry_for(act))\n"
    ),
    "a variable that happens to be called retry_policy": (
        "async def run(policy):\n    retry_policy = policy\n"
        "    await workflow.execute_activity(act, retry_policy=retry_policy)\n"
    ),
    "a child with no policy": _TEMPLATE.format(call="execute_child_workflow", args="W.run"),
    "a child with a literal policy": _TEMPLATE.format(
        call="start_child_workflow", args="W.run, retry_policy=RetryPolicy(maximum_attempts=3)"
    ),
    "a child with an activity's policy": _TEMPLATE.format(
        call="execute_child_workflow", args="W.run, retry_policy=retry_for(act)"
    ),
}

_ACCEPTED = [
    _TEMPLATE.format(call="execute_activity", args="act, retry_policy=retry_for(act)"),
    _TEMPLATE.format(call="execute_local_activity", args="act, retry_policy=retry_for(act)"),
    _TEMPLATE.format(
        call="start_activity", args="activity=mod.act, retry_policy=retry_for(mod.act)"
    ),
    _TEMPLATE.format(
        call="execute_child_workflow", args="W.run, retry_policy=child_workflow_once()"
    ),
    _TEMPLATE.format(
        call="start_child_workflow", args="W.run, retry_policy=child_workflow_retry()"
    ),
    # The helper that forwards its caller's policy: the parameter is its own.
    "async def run_child(retry_policy):\n"
    "    return await workflow.execute_child_workflow(W.run, retry_policy=retry_policy)\n",
]


@pytest.mark.parametrize("name", list(_REFUSED))
def test_the_walk_refuses_each_spelling_it_exists_to_refuse(name: str) -> None:
    """Each hole the walk could fall through is closed, shown by a snippet that uses it."""
    assert _offences_of(_REFUSED[name]), f"{name} got through"


@pytest.mark.parametrize("source", _ACCEPTED)
def test_the_walk_accepts_the_spellings_the_tree_uses(source: str) -> None:
    """The refusals are not so broad that the shipped spellings fail with them."""
    assert _offences_of(source) == [], source


def test_the_interceptor_delegation_is_not_a_dispatch() -> None:
    """`self.next.execute_activity(...)` hands on an activity already scheduled."""
    interceptor = "async def f(self, input):\n    return await self.next.execute_activity(input)\n"
    assert _all_offences(ast.parse(interceptor), "interceptor") == (0, [])


def _declared_classes() -> dict[tuple[str, str], str]:
    """Each `@activity.defn` function, by (path, name), and the class member its decorator names.

    A function whose class is not stated as `ActivityClass.<member>` maps to `""`.
    """
    declared: dict[tuple[str, str], str] = {}
    for where, tree in _source_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            decorators = [ast.unparse(one) for one in node.decorator_list]
            if not any(
                one == "activity.defn" or one.startswith("activity.defn(") for one in decorators
            ):
                continue
            declared[(where, node.name)] = _stated_class(node)
    return declared


def _stated_class(function: ast.AsyncFunctionDef | ast.FunctionDef) -> str:
    """The member a `@durable_activity(queue, ActivityClass.X)` or `@activity_in_class` names."""
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Name):
            continue
        if decorator.func.id == "durable_activity" and len(decorator.args) == 2:
            stated = decorator.args[1]
        elif decorator.func.id == "activity_in_class" and len(decorator.args) == 1:
            stated = decorator.args[0]
        else:
            continue
        if (
            isinstance(stated, ast.Attribute)
            and isinstance(stated.value, ast.Name)
            and stated.value.id == "ActivityClass"
            and stated.attr in ActivityClass.__members__
        ):
            return stated.attr
    return ""


def test_every_activity_declares_its_class() -> None:
    """A new activity without a class is red, here, before it can be dispatched under a default.

    `@durable_activity(queue, ActivityClass.X)` states it where the activity is defined; one with
    no fixed queue uses `@activity_in_class`. Walked over the whole package, because any decorator
    can be dropped in a tidying pass, and compared with what the imported function carries.
    """
    declared = _declared_classes()
    assert len(declared) >= 70, f"only {len(declared)} activities found; the walk reads nothing"

    undeclared = sorted(
        f"{path}::{name}" for (path, name), member in declared.items() if not member
    )
    assert not undeclared, (
        f"{undeclared} are declared with `@activity.defn` and state no `ActivityClass`; add it to "
        "their `@durable_activity(queue, ActivityClass.<class>)`"
    )

    for (path, name), member in declared.items():
        module = importlib.import_module("chemclaw." + path.removesuffix(".py").replace("/", "."))
        function = getattr(module, name)
        assert activity_class_of(function) is ActivityClass[member], f"{path}::{name}"


def test_no_class_exists_that_no_activity_uses() -> None:
    """A class nobody is in is a policy nobody can reach, and a setting nobody would tune."""
    used = set(_declared_classes().values())
    assert used == set(ActivityClass.__members__), (
        f"unused classes: {sorted(set(ActivityClass.__members__) - used)}"
    )


# The classes whose members carry a safety property, pinned by name. A wrong class is otherwise
# silent: every policy is valid, so moving `evaluate_candidates` from CALC to IO leaves the suite
# green while a computed objective is recomputed eight times. Moving an activity into or out of one
# of these fails here, naming it; the other classes are the rest, and need no table.
_PINNED_MEMBERS = {
    # Re-running one repeats an expensive, metered calculation (a persisted result is never
    # recomputed).
    ActivityClass.CALC: {"evaluate_candidates", "run_xtb_calculation"},
    # Re-running one repeats audited side effects: an agent turn's tools, a governed check.
    ActivityClass.GOVERNED: {"run_agent_step", "run_computable_check"},
    # Retried against a full server until the call's own deadline; the only unbounded class.
    ActivityClass.QUEUED_TOOL: {"call_queued_tool"},
    # A call to a system outside the worker and Postgres. The model gateway is the costly one: a
    # retry is billed again, and without jitter every worker asks a provider that is down at once.
    ActivityClass.REMOTE: {
        "compare_hypotheses",
        "critique_hypothesis",
        "deliver_digest_activity",
        "deliver_message_activity",
        "derive_check",
        "draft_angles",
        "drain_reaction_corpus",
        "drain_result_publications",
        "gather_hypothesis_evidence",
        "generate_hypotheses",
        "label_stale_reactions",
        "mirror_commitments_activity",
        "plan_label_sync",
        "read_check_result",
        # Embeds stored chunks through the embedding endpoint, as `reembed_stale_documents` does.
        "reembed_document_batch",
        "reembed_stale_documents",
        "retrieve_section",
        "run_tool_step",
        "sync_document_share",
        "sync_eln_entries",
    },
}

# The activities that call the model gateway, which must stay in `REMOTE` whatever else moves.
_MODEL_GATEWAY_CALLERS = {
    "compare_hypotheses",
    "critique_hypothesis",
    "derive_check",
    "draft_angles",
    "generate_hypotheses",
    "read_check_result",
}


def _members_by_class() -> dict[ActivityClass, set[str]]:
    """The activities of each class, by function name (unique across the tree)."""
    members: dict[ActivityClass, set[str]] = {one: set() for one in ActivityClass}
    seen: set[str] = set()
    for (_, name), member in _declared_classes().items():
        assert name not in seen, f"{name} is defined twice; the pinned table is keyed by name"
        seen.add(name)
        members[ActivityClass[member]].add(name)
    return members


@pytest.mark.parametrize("activity_class", list(_PINNED_MEMBERS))
def test_the_members_of_a_safety_bearing_class_are_exactly_the_pinned_ones(
    activity_class: ActivityClass,
) -> None:
    """Moving an activity into or out of CALC, GOVERNED, QUEUED_TOOL or REMOTE is a visible edit.

    Set equality, so an addition is named as well as a removal: the author of a new activity in
    one of these classes states, here, why it belongs there.
    """
    actual = _members_by_class()[activity_class]
    expected = _PINNED_MEMBERS[activity_class]
    assert actual == expected, (
        f"{activity_class}: no longer in it {sorted(expected - actual)}; "
        f"newly in it {sorted(actual - expected)}. Update `_PINNED_MEMBERS` with the reason."
    )


def test_every_activity_that_calls_the_model_gateway_is_remote() -> None:
    """A model call is billed again on retry and must back off and spread like any dependency."""
    assert _MODEL_GATEWAY_CALLERS <= _members_by_class()[ActivityClass.REMOTE]


def test_a_function_with_no_class_is_refused_by_name() -> None:
    """The lookup fails loudly rather than answering with a default."""

    async def undeclared() -> None:
        return None

    with pytest.raises(LookupError, match="undeclared"):
        retry_for(undeclared)

    declared = activity_in_class(ActivityClass.CALC)(undeclared)
    assert retry_for(declared).maximum_attempts == settings.activity_max_attempts


# --- against a real server ---------------------------------------------------------------------

_ATTEMPTS: dict[str, int] = {}


def _probe(activity_class: ActivityClass, error: type[Exception]) -> Callable[[], Awaitable[None]]:
    """An activity of `activity_class` that always fails with `error`, counting its attempts."""
    name = f"retry_probe_{activity_class.value}_{error.__name__}"

    @activity_in_class(activity_class)
    @activity.defn(name=name)
    async def probe() -> None:
        _ATTEMPTS[name] = _ATTEMPTS.get(name, 0) + 1
        raise error(f"{name} failed on purpose")

    return probe


_PROBES: dict[tuple[ActivityClass, type[Exception]], Callable[[], Awaitable[None]]] = {
    (one, error): _probe(one, error) for one in _BOUNDED for error in (ValueError, ConnectionError)
}


@workflow.defn(name="RetryClassProbe")
class _RetryClassProbe:
    """Dispatch one probe exactly as production does and report how its failure ended."""

    @workflow.run
    async def run(self, probe: str, error: str) -> dict[str, str]:
        """The retry state and cause the workflow sees after the activity gives up."""
        function = _PROBES[
            (ActivityClass(probe), ValueError if error == "ValueError" else ConnectionError)
        ]
        try:
            await workflow.execute_activity(
                function,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_for(function),
            )
        except ActivityError as exc:
            cause = exc.cause
            return {
                "retry_state": exc.retry_state.name if exc.retry_state else "",
                "cause_type": (cause.type or "") if isinstance(cause, ApplicationError) else "",
                "message": str(cause),
            }
        return {"retry_state": "SUCCEEDED", "cause_type": "", "message": ""}


# Attempts a transient failure gets under the settings pinned below.
_TRANSIENT_ATTEMPTS = {
    ActivityClass.IO: 3,
    ActivityClass.NOTE_WRITE: 3,
    ActivityClass.COMPUTE: 3,
    ActivityClass.REMOTE: 3,
    ActivityClass.CALC: 3,
    ActivityClass.GOVERNED: 2,
}


@pytest.mark.timeout(300)
def test_bad_data_costs_one_attempt_and_a_transient_failure_is_retried_to_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad data costs one attempt; a transient failure is retried to the bound, on a real server.

    In every bounded class a `ValueError` is tried once and a `ConnectionError` is retried to the
    class's bound and then fails with its own message.

    The calculation class is the one that matters most: it must never retry a `ValueError`, which
    would recompute (or re-bill) a result that the same input can only fail again. Spacings are
    shrunk through the same settings an operator tunes, so the schedule under test is the shipped
    one scaled down, not a test double.
    """
    for name, value in {
        "io_retry_max_attempts": 3,
        "io_retry_initial_seconds": 0.05,
        "io_retry_max_interval_seconds": 0.1,
        "slow_retry_initial_seconds": 0.05,
        "slow_retry_max_interval_seconds": 0.1,
        "activity_max_attempts": 3,
        "note_write_max_attempts": 3,
        "agent_step_max_attempts": 2,
        "calc_server_timeout_seconds": 0.4,
    }.items():
        monkeypatch.setattr(settings, name, value)
    _ATTEMPTS.clear()

    async def _run() -> dict[tuple[ActivityClass, str], dict[str, str]]:
        outcomes: dict[tuple[ActivityClass, str], dict[str, str]] = {}
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="retry-classes",
                workflows=[_RetryClassProbe],
                activities=list(_PROBES.values()),
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                for activity_class in _BOUNDED:
                    for error in ("ValueError", "ConnectionError"):
                        outcomes[(activity_class, error)] = await client.execute_workflow(
                            _RetryClassProbe.run,
                            args=[activity_class.value, error],
                            id=f"retry-{activity_class.value}-{error}",
                            task_queue="retry-classes",
                        )
        return outcomes

    outcomes = asyncio.run(_run())

    for activity_class in _BOUNDED:
        bad = outcomes[(activity_class, "ValueError")]
        assert _ATTEMPTS[f"retry_probe_{activity_class.value}_ValueError"] == 1, (
            f"{activity_class}: a ValueError must be attempted exactly once"
        )
        assert bad["retry_state"] == "NON_RETRYABLE_FAILURE", (activity_class, bad)
        assert bad["cause_type"] == "ValueError", (activity_class, bad)

        transient = outcomes[(activity_class, "ConnectionError")]
        expected = _TRANSIENT_ATTEMPTS[activity_class]
        assert _ATTEMPTS[f"retry_probe_{activity_class.value}_ConnectionError"] == expected, (
            f"{activity_class}: a transient failure must be attempted exactly {expected} times"
        )
        assert transient["retry_state"] == "MAXIMUM_ATTEMPTS_REACHED", (activity_class, transient)
        assert transient["cause_type"] == "ConnectionError", (activity_class, transient)
        assert "failed on purpose" in transient["message"], (activity_class, transient)
