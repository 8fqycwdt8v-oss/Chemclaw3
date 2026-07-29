"""One generated agent tool per declared job — the four bespoke adapters, written once.

`agents/qm_tools.py::submit_qm_job` and the three launchers in `agents/durable_tools.py` were the
same handful of lines four times: authorize the trigger, refuse under dry-run, demand an actor,
derive a deterministic workflow id, start the workflow, announce the launch, return the id. Only
the workflow class and the id derivation differed — and both are now manifest data
(`JobSpec.workflow`, plus the job name and the arguments the id hashes). So the adapter becomes
a factory: one function built per declared job, with the shared body written in exactly one
place. The QM launcher was the last of the four to go (D-118); this is now the *only* way a
durable capability is launched from a conversation.

The generated tool is a *first-class* tool, not a special case. It is registered through the
same `agents.tool_registry.register_tool` a hand-written tool uses, keyed by the manifest's
`name`, which is what makes every existing mechanism apply to it untouched: the audit middleware
wraps it, the per-tool authorization gate addresses it by that name (`tool_role_gates`, and the
built-in `DEFAULT_WRITE_TOOL_GATES` for a job that writes), a profile can narrow it away, and
`scripts.validate_prose_contract` sees it when checking the agent's prose.

Why a generated pydantic model rather than a `dict` parameter: MAF derives a tool's JSON schema
from its signature, and a single pydantic-model parameter is already the in-repo idiom for a
structured argument (`start_optimization_campaign(spec: CampaignSpec)`). Building that model
from the declared
`params` gives the model a real, typed, per-field-documented schema — an untyped `dict[str, Any]`
would advertise "pass anything", which is precisely how a model calls a tool wrongly.
"""

import asyncio
from collections.abc import Callable
from importlib import import_module
from typing import Any

from pydantic import BaseModel, Field, create_model
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from agents.authz import authorize_trigger, require_actor
from agents.dialogue_tools import dry_run_notice, is_dry_run
from agents.harness_todo import mark_awaiting_job
from agents.identity_context import get_current_correlation_id
from agents.session_context import get_current_session, get_current_session_id
from agents.tool_registry import CapabilityTool
from agents.turn_signals import record_job_started
from chemclaw.config import settings
from chemclaw.ids import stable_hash
from chemclaw.metrics_bridge import record_metric
from chemclaw.temporal_client import connect
from connectors.manifest import JobParamType, JobSpec
from workflows.connector_job import ConnectorJobInput, ConnectorJobResult, ConnectorJobWorkflow

# The declared parameter types, mapped to the annotations the generated model is built from.
# Closed by design (see `JobParamType`): every entry is a type a JSON-schema-driven model can
# fill reliably.
_PARAM_ANNOTATIONS: dict[JobParamType, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "string[]": list[str],
    "number[]": list[float],
    "object": dict[str, Any],
}


# How much of a job's arguments a dry-run notice shows — the same budget as the audit trail's
# and the turn event's tool-argument preview, so one convention covers every "show what was
# called".
_DETAIL_MAX_CHARS = 200


class ConnectorJobError(ValueError):
    """A declared job cannot be built — a bad `params_model` reference.

    A `ValueError` subclass for the same reason `ConnectorError` is: this is a "this deployment
    is misconfigured" failure, and one `except ValueError` at an entry point should catch all of
    them.
    """


def _camel(value: str) -> str:
    """`bo-campaign`/`start_campaign` → `BoCampaign`/`StartCampaign` — for a readable model name."""
    return "".join(part.capitalize() for part in value.replace("-", "_").split("_") if part)


def resolve_params_model(reference: str) -> type[BaseModel]:
    """Import the pydantic model a `module:Attribute` reference names, for a structured job input.

    The full-fidelity alternative to declaring params inline: a job whose input is a rich domain
    object (a nested optimization problem, a list of report sections) already has a validated
    model in code, and re-declaring its shape in YAML would both lose structure and create a
    second source of truth. Referencing it keeps one schema and gives the agent the same typed
    surface a hand-written tool would advertise.

    Only the *type* is imported, never the capability — a shared DTO, which is exactly what
    crosses a process boundary in any client/server split. A reference that does not resolve to
    a pydantic model is a configuration error reported here (and by `make connector-validate`),
    not a confusing failure when the tool is first called.

    Raises:
        ConnectorJobError: When the module or attribute does not exist, or is not a pydantic model.
    """
    module_name, _, attribute = reference.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise ConnectorJobError(
            f"params_model {reference!r}: cannot import {module_name!r}"
        ) from exc
    model = getattr(module, attribute, None)
    if model is None:
        raise ConnectorJobError(f"params_model {reference!r}: {module_name!r} has no {attribute!r}")
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ConnectorJobError(f"params_model {reference!r} is not a pydantic model")
    return model


def resolve_precondition(reference: str) -> Callable[[Any], None]:
    """Import the `module:function` a job's `precondition` names, for the pre-launch domain check.

    Resolved at build time (and by `make connector-validate`), not at call time, so a typo is a
    configuration error a deployment finds before a chemist does.

    Raises:
        ConnectorJobError: When the module or attribute does not exist, or is not callable.
    """
    module_name, _, attribute = reference.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise ConnectorJobError(
            f"precondition {reference!r}: cannot import {module_name!r}"
        ) from exc
    check = getattr(module, attribute, None)
    if check is None:
        raise ConnectorJobError(f"precondition {reference!r}: {module_name!r} has no {attribute!r}")
    if not callable(check):
        raise ConnectorJobError(f"precondition {reference!r} is not callable")
    return check  # type: ignore[no-any-return]


def _params_model(connector: str, job: JobSpec) -> type[BaseModel]:
    """The pydantic model for one job's launch arguments — referenced, or generated from `params`.

    A generated field carries its manifest `description`, so the JSON schema documents every
    argument for the model reading it. An optional param defaults to `None` and widens to `T |
    None`, because "the caller may omit this" and "the value may be absent" must agree — a
    required-typed field with a `None` default would validate a payload the workflow cannot use.
    """
    if job.params_model is not None:
        return resolve_params_model(job.params_model)
    fields: dict[str, Any] = {}
    for param in job.params:
        annotation = _PARAM_ANNOTATIONS[param.type]
        if param.required:
            fields[param.name] = (annotation, Field(description=param.description))
        else:
            fields[param.name] = (
                annotation | None,
                Field(default=None, description=param.description),
            )
    return create_model(
        f"{_camel(connector)}{_camel(job.name)}Params",
        __doc__=f"Launch arguments for the {job.name!r} job served by connector {connector!r}.",
        **fields,
    )


def _docstring(job: JobSpec) -> str:
    """Assemble the tool docstring the model reads: summary, description, then the arguments.

    MAF derives the tool description from the docstring, so this *is* the job's model-facing
    documentation. The `Args:` section is rendered from the same declared params the schema is
    built
    from, so a parameter can never be documented in the prose but missing from the signature — the
    drift a hand-written adapter invites.
    """
    lines = [job.summary]
    if job.description:
        lines.extend(["", job.description.strip()])
    if job.params:
        lines.extend(["", "Args:", "    params: The job's launch arguments."])
        lines.extend(f"        {param.name}: {param.description}" for param in job.params)
    elif job.params_model is not None:
        # A referenced model documents its own fields (their descriptions travel in the JSON
        # schema MAF derives from it), so repeating them here would be a second, drift-prone
        # copy.
        lines.extend(["", "Args:", "    params: The job's launch arguments; see the field docs."])
    lines.extend(["", "Returns:"])
    if job.inline_wait_seconds is not None:
        lines.extend(
            [
                "    The finished result when the calculation completes quickly, or — when it is",
                "    too slow to hold up the conversation — a job id to poll with",
                "    `get_durable_job_status`. Both are normal outcomes: report a job id as work",
                "    in progress, not as a failure. Re-running with identical arguments rejoins",
                "    the existing run rather than paying twice.",
            ]
        )
    else:
        lines.extend(
            [
                "    The job id to poll with `get_durable_job_status`. Re-launching with identical",
                "    arguments returns the existing job id rather than starting a second run.",
            ]
        )
    return "\n".join(lines)


def job_workflow_id(connector: str, job: str, payload: dict[str, Any]) -> str:
    """The deterministic id of one connector job run — the idempotency key (D-011).

    Public because it is the *contract*, not an implementation detail: a duplicate launch must
    resolve to this id, and Stage B's migration test asserts the ids the four bespoke adapters
    produced are reproduced exactly, so no in-flight workflow history is orphaned.
    """
    return f"{connector}-{job}-{stable_hash([connector, job, payload])}"


def build_job_tool(connector: str, job: JobSpec) -> CapabilityTool:
    """Build the agent tool that launches one declared connector job.

    The returned coroutine function is what MAF advertises: its `__name__` is the manifest's job
    name (which is also the authorization key and the profile-narrowing key), its docstring is
    the model-facing description, and its single parameter is the generated params model.

    Args:
        connector: The owning connector's name — part of the workflow id, and reported in the
            push-back payload so a completion can be traced back to its capability.
        job: The declared job.

    Returns:
        An async tool function, unregistered — the registry call belongs to the caller that
        knows which connectors are enabled (`connectors.registry`).
    """
    params_model = _params_model(connector, job)
    precondition = resolve_precondition(job.precondition) if job.precondition else None

    async def launch(params: params_model) -> str | ConnectorJobResult:  # type: ignore[valid-type]
        # **Validate here, because nothing upstream does** (D-138). The annotation above is a
        # pydantic model and MAF publishes its JSON schema, but MAF hands the body the decoded
        # JSON *object* — a plain `dict` — rather than constructing the model from it. Until this
        # call existed every declared job died on `'dict' object has no attribute 'model_dump'`
        # the first time a chemist asked for one, and the precondition below was handed a dict
        # whose attributes it could not read. Accept an already-built model too: a caller that
        # holds one (a test, a template step) is not wrong, and `model_validate` is the one entry
        # point that takes either.
        spec = params_model.model_validate(params)
        # Authorize the expensive trigger against the turn's user *before* any durable work
        # (F4-T5), so an autonomously-planned todo cannot start a costly run outside the user's
        # entitlements.
        if job.expensive:
            authorize_trigger(job.name)
        # Then the job's own domain guard, if it declared one, for the reason
        # `JobSpec.precondition` records: this is the only replay-safe place such a check can
        # live.
        if precondition is not None:
            precondition(spec)
        payload: dict[str, Any] = spec.model_dump(mode="json", exclude_none=True)
        workflow_id = job_workflow_id(connector, job.name, payload)
        if is_dry_run():
            return dry_run_notice(f"start the {job.name} job", _detail(connector, payload))
        # `require_actor` is the core rule (F4-T3): under Entra, refuse durable work with no user.
        requested_by = require_actor()
        client = await connect()
        try:
            handle = await client.start_workflow(
                ConnectorJobWorkflow.run,
                ConnectorJobInput(
                    connector=connector,
                    job=job.name,
                    workflow=job.workflow,
                    task_queue=job.task_queue,
                    payload=payload,
                    requested_by=requested_by,
                    session_id=get_current_session_id() or "",
                    correlation_id=get_current_correlation_id() or "",
                    publish_to_graph=job.publish_to_graph,
                ),
                id=workflow_id,
                task_queue=settings.background_task_queue,
                # Only a *failed* run may re-execute under the same id: the default policy
                # rejects duplicates while a run is open but would silently recompute a
                # completed one.
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            # The identical job is already running or already done: the idempotency contract
            # succeeding. When the job answers inline, the *already finished* case is the common
            # one (a repeat of a cheap calculation) and rejoining it is what makes a re-ask feel
            # like a cache hit rather than a poll; a still-running one falls through to its id.
            if job.inline_wait_seconds is not None:
                existing = await _await_briefly(
                    client.get_workflow_handle(workflow_id, result_type=ConnectorJobResult),
                    job.inline_wait_seconds,
                )
                if existing is not None:
                    return existing
            # Deliberately *not* announced as started — an already-finished run will never emit
            # the matching `job_completed` event, and the surface would show a row that stays
            # "running" forever.
            return workflow_id
        if job.inline_wait_seconds is not None:
            finished = await _await_briefly(handle, job.inline_wait_seconds)
            if finished is not None:
                # It answered inside the turn, so there is no background work to announce and
                # nothing for the chemist to poll — the result *is* the tool's return value.
                return finished
        # Two announcements, both only on a *genuine* start. The turn's event stream shows the
        # launch while the turn is still streaming (D-042); the harness's todo list records that
        # the plan is blocked on this id, so `todos_remaining` sees "waiting" rather than
        # re-invoking the model with nothing new (D-040). A re-joined run is deliberately silent:
        # it may already be finished, and neither surface would ever get the matching
        # `job_completed` event to clear the row it drew.
        await _mark_awaiting_if_harness(handle.id, job.name)
        record_job_started(handle.id, job.name)
        # Counted here rather than at the tool boundary: this is the branch that actually
        # started a workflow. The re-joined path above returns an existing id without
        # starting anything, and counting it would report launches that never happened.
        record_metric(lambda m: m.increment("chemclaw_jobs_started_total"))
        return handle.id

    launch.__name__ = job.name
    launch.__qualname__ = job.name
    launch.__doc__ = _docstring(job)
    return launch


async def _mark_awaiting_if_harness(job_id: str, job_name: str) -> None:
    """Record the harness todo awaiting `job_id`, when the harness's todo list is in play.

    Core's obligation, not a bundle's: a durable launch that leaves the harness's plan open would
    keep `todos_remaining` re-invoking the model with nothing to report, and no connector can see
    the turn's todo state to close that itself. It lived in the hand-written QM launcher until the
    HPC job became a declared job (D-118), which made this the only launcher left to hold it — and
    fixed the gap that every *other* durable job had never had it at all.

    Silent no-op off the harness path (harness disabled, or no live `AgentSession` ambient — e.g.
    the CLI, which runs single-shot): writing to a todo list nothing reads would just be dead state.
    """
    if not settings.harness_enabled:
        return
    session = get_current_session()
    if session is None:
        return
    await mark_awaiting_job(session, job_id, title=f"Await the {job_name} job {job_id}")


async def _await_briefly(handle: Any, budget: float) -> ConnectorJobResult | None:
    """Wait up to `budget` seconds for a started job, or `None` if it is still running.

    `None` means "not finished yet", never "failed": a genuine workflow failure raises, so the
    tool reports the error rather than silently degrading to a job id the chemist would poll
    forever waiting for a run that is already dead.

    The wait is cancel-safe by construction — `asyncio.wait_for` cancels only the *waiter*, and
    the workflow it is waiting on keeps running on its worker. So a turn that times out or is
    abandoned mid-wait leaves a durable run that still completes, still caches its result and
    still pushes back to the session. That is the property that makes this safe to do inside a
    conversation at all.

    The result is *validated*, not cast: the envelope is the connector contract, and a
    bundle-authored workflow returning some other shape should fail here by name rather than hand
    the model an unlabelled dict to interpret.
    """
    try:
        finished = await asyncio.wait_for(handle.result(), budget)
    except TimeoutError:
        return None
    return ConnectorJobResult.model_validate(finished)


def _detail(connector: str, payload: dict[str, Any]) -> str:
    """The argument summary a dry-run notice reports (sorted for stability, bounded in length).

    Bounded because a structured job's payload can be a whole optimization problem, and a
    dry-run notice is a sentence a chemist reads — not a dump. The same reasoning (and the same
    200-char budget) as the tool-argument preview in the audit trail and the turn's
    `ToolCallEvent`.
    """
    if not payload:
        return f"connector {connector}"
    rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(payload.items()))
    if len(rendered) > _DETAIL_MAX_CHARS:
        rendered = rendered[:_DETAIL_MAX_CHARS] + "…"
    return f"connector {connector} with {rendered}"
