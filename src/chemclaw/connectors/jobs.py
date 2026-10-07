"""One generated agent tool per declared job: the only way a conversation launches durable work.

Each tool authorizes the trigger, demands an actor, derives a deterministic workflow id, starts the
workflow, announces the launch and returns the id; the workflow class and id inputs are manifest
data (`JobSpec`). It is registered through `register_tool` under the manifest's `name`, so audit,
per-tool authorization, profile narrowing and the prose-contract check apply unchanged (D-118).

Its single parameter is a pydantic model built from the declared `params` (or referenced by
`params_model`), so the model sees a typed, per-field-documented schema rather than an open dict.
"""

import asyncio
import logging
from collections.abc import Callable
from importlib import import_module
from typing import Any

from pydantic import BaseModel, Field, create_model
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from chemclaw.agent.authz import authorize_trigger, require_actor
from chemclaw.agent.tool_framing import defanged_payload
from chemclaw.connectors.manifest import JobParamType, JobSpec
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.ids import stable_hash
from chemclaw.core.manifest_io import check_driver_module
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.plan_context import get_current_plan_link
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect
from chemclaw.core.tool_registry import CapabilityTool
from chemclaw.core.turn_signals import record_job_started
from chemclaw.durable.connector_job import (
    ConnectorJobInput,
    ConnectorJobResult,
    ConnectorJobWorkflow,
    envelope_from_result,
    failure_reason,
)

logger = logging.getLogger(__name__)

# Declared parameter types mapped to annotations; closed so every type is one a JSON-schema-driven
# model fills reliably.
_PARAM_ANNOTATIONS: dict[JobParamType, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "string[]": list[str],
    "number[]": list[float],
    "object": dict[str, Any],
}


class ConnectorJobError(ChemclawError):
    """A declared job cannot be built (a bad `params_model` reference) or launched as asked.

    A `ValueError` subclass so one `except ValueError` at an entry point catches it. Covers both a
    misconfigured deployment (read by an operator) and a refused launch such as a missing reason
    (read by the model).
    """


def _camel(value: str) -> str:
    """`bo-campaign`/`start_campaign` → `BoCampaign`/`StartCampaign` — for a readable model name."""
    return "".join(part.capitalize() for part in value.replace("-", "_").split("_") if part)


def resolve_params_model(reference: str) -> type[BaseModel]:
    """Import the pydantic model a `module:Attribute` reference names, for a structured job input.

    Lets a job whose input is a rich domain object reuse its validated model instead of re-declaring
    it in YAML. Only the type is imported. The reference is held to
    `CHEMCLAW_MANIFEST_DRIVER_PACKAGES` first, because the import runs module code in the chat
    process and a manifest is data.

    Raises:
        ConnectorJobError: When the package is not allowed, the module or attribute does not exist,
            or the attribute is not a pydantic model.
    """
    check_driver_module(reference, ConnectorJobError, "params_model")
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


def _resolve_job_hook(reference: str, field: str) -> Any:
    """Import the `module:function` a job's `field` names and return it, or raise naming both.

    Shared by `precondition` and `unavailable_reason`, and held to the same package allow-list as
    `resolve_params_model`, since what they name is called.

    Raises:
        ConnectorJobError: When the package is not allowed, the module or attribute does not
            exist, or the attribute is not callable.
    """
    check_driver_module(reference, ConnectorJobError, field)
    module_name, _, attribute = reference.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise ConnectorJobError(f"{field} {reference!r}: cannot import {module_name!r}") from exc
    hook = getattr(module, attribute, None)
    if hook is None:
        raise ConnectorJobError(f"{field} {reference!r}: {module_name!r} has no {attribute!r}")
    if not callable(hook):
        raise ConnectorJobError(f"{field} {reference!r} is not callable")
    return hook


def resolve_precondition(reference: str) -> Callable[[Any], None]:
    """Import the `module:function` a job's `precondition` names, for the pre-launch domain check.

    Resolved at build time (and by `make connector-validate`) so a typo is found before a chemist
    finds it.

    Raises:
        ConnectorJobError: See `_resolve_job_hook`.
    """
    check: Callable[[Any], None] = _resolve_job_hook(reference, "precondition")
    return check


def unavailable_reason(job: JobSpec) -> str | None:
    """Why this deployment cannot run `job` at all, or `None` when it can, asked at this moment.

    Unlike `precondition`, no argument can change the answer, so the launcher is withheld from the
    model (`registry.job_tools`) rather than offered and refused. Read at call time because it
    depends on configuration.

    Raises:
        ConnectorJobError: The declared reference does not resolve (see `_resolve_job_hook`).
    """
    if job.unavailable_reason is None:
        return None
    reason: str | None = _resolve_job_hook(job.unavailable_reason, "unavailable_reason")()
    return reason


# One generated params class per (connector, job definition): `create_model` returns a fresh class
# each call, and a spec built from one class fails `isinstance`/`model_validate` against another.
# Keyed on the serialized definition so reloaded manifests get a matching class (a frozen `JobSpec`
# is unhashable because of its list fields).
_PARAMS_MODELS: dict[tuple[str, str], type[BaseModel]] = {}


def _params_model(connector: str, job: JobSpec) -> type[BaseModel]:
    """The pydantic model for one job's launch arguments: referenced, or generated from `params`.

    Generated fields carry their manifest `description`. An optional param defaults to `None` and
    widens to `T | None`, so "may be omitted" and "may be absent" agree. Memoized via
    `_PARAMS_MODELS`.
    """
    key = (connector, job.model_dump_json())
    cached = _PARAMS_MODELS.get(key)
    if cached is not None:
        return cached
    model = _build_params_model(connector, job)
    _PARAMS_MODELS[key] = model
    return model


def _build_params_model(connector: str, job: JobSpec) -> type[BaseModel]:
    """Construct the params model for `job` — the uncached half of `_params_model`."""
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


# The `rationale` argument, documented once for every generated job tool, written for the model so
# the stored reason is a usable sentence.
_RATIONALE_DOC = [
    "    rationale: Why this run is worth doing, in a sentence or two a chemist would recognise:",
    "        the question it should answer and what prompted it (whose request, which earlier",
    "        result). It is stored with the run and stamped onto any note the run records, so a",
    "        later session — or a chemist reading that note — can tell why it was done. Say what",
    "        the run is *for*; do not restate the arguments.",
]


def _docstring(job: JobSpec) -> str:
    """Assemble the tool docstring the model reads: summary, description, then the arguments.

    The `Args:` section is rendered from the same declared params as the schema, so the two cannot
    drift.
    """
    lines = [job.summary]
    if job.description:
        lines.extend(["", job.description.strip()])
    lines.extend(["", "Args:"])
    if job.params:
        lines.append("    params: The job's launch arguments.")
        lines.extend(f"        {param.name}: {param.description}" for param in job.params)
    elif job.params_model is not None:
        # A referenced model documents its own fields in its JSON schema.
        lines.append("    params: The job's launch arguments; see the field docs.")
    else:
        lines.append("    params: This job takes no arguments; pass an empty object.")
    lines.extend(_RATIONALE_DOC)
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
    """The deterministic id of one connector job run: the idempotency key.

    Public because it is a contract: a duplicate launch must resolve to this id, and in-flight
    histories depend on it staying stable.
    """
    return f"{connector}-{job}-{stable_hash([connector, job, payload])}"


def require_funded_ceiling(connector: str, job: JobSpec) -> None:
    """Refuse a job whose manifest grants it a runtime the operator has not funded.

    `awaits_answer` removes a job's execution ceiling, so a manifest (which is data) could grant
    itself unbounded runtime; the operator must name the job in `connector_jobs_awaiting_answer`.
    Checked in the shared pre-flight (`prepare_job_launch`) so every launcher, including the
    template job step, is covered; and at launch rather than at tool build, so one bad declaration
    refuses only its own job instead of the whole tool surface. `make connector-validate` calls it
    directly.

    Args:
        connector: The owning connector's name, half of the name the operator grants.
        job: The declared job.

    Raises:
        ConnectorJobError: The job declares `awaits_answer` and the deployment has not named it in
            `connector_jobs_awaiting_answer`.
    """
    qualified = f"{connector}.{job.name}"
    if job.awaits_answer and qualified not in settings.connector_jobs_awaiting_answer_list:
        raise ConnectorJobError(
            f"job {qualified!r} declares `awaits_answer: true`, which runs it with no wall-clock "
            f"ceiling at all rather than the deployment's {settings.connector_job_timeout_seconds}s"
            "; it is refused by default because a manifest is data. Add it to "
            "CHEMCLAW_CONNECTOR_JOBS_AWAITING_ANSWER — which *replaces* the default rather than "
            "extending it, so list every job you mean to fund, including any this release already "
            "ships."
        )


def prepare_job_launch(connector: str, job: JobSpec, params: Any) -> dict[str, Any]:
    """Everything that must be true before a job's durable work starts, and the payload it yields.

    Validate, check funding and availability, authorize the expensive trigger, run the declared
    precondition, serialize. The single pre-flight shared by every launcher (the agent tool below
    and `durable.template_activities.authorize_job_step`), so no launcher can skip a step.

    Args:
        connector: The owning connector's name, for the refusal message only.
        job: The declared job.
        params: The launch arguments, as a `dict` or an already-built params model.

    Returns:
        The validated launch payload, JSON-ready, as the workflow input's `payload`.

    Raises:
        AuthorizationError: The job is `expensive` and the ambient user is not entitled to it.
        ValidationError: `params` does not satisfy the job's declared schema.
        Exception: Whatever the declared precondition raises to refuse the launch.
    """
    # Validate here, because a tool body receives the decoded JSON object, not a constructed model.
    # `model_validate` also accepts an already-built model.
    spec = _params_model(connector, job).model_validate(params)
    # First, so an unfunded job is refused before running its precondition (bundle code).
    require_funded_ceiling(connector, job)
    # A template step names its job by string and bypasses `registry.job_tools`' filter, so refuse
    # here too, before authorization.
    reason = unavailable_reason(job)
    if reason is not None:
        raise ConnectorJobError(f"{connector}.{job.name} is unavailable here: {reason}")
    # Authorize against the turn's user before any durable work, so a planned todo or template step
    # cannot start a costly run outside the user's entitlements.
    if job.expensive:
        authorize_trigger(job.name)
    # Then the job's own domain guard, if it declared one, for the reason `JobSpec.precondition`
    # records: this is the only replay-safe place such a check can live.
    if job.precondition:
        resolve_precondition(job.precondition)(spec)
    payload: dict[str, Any] = spec.model_dump(mode="json", exclude_none=True)
    return payload


def build_job_tool(connector: str, job: JobSpec) -> CapabilityTool:
    """Build the agent tool that launches one declared connector job.

    The returned coroutine's `__name__` is the job name (also the authorization and profile key),
    its docstring is the model-facing description, and its single parameter is the params model.
    Building refuses nothing; `prepare_job_launch` refuses at launch.

    Args:
        connector: The owning connector's name, part of the workflow id and reported in the
            push-back payload.
        job: The declared job.

    Returns:
        An async tool function, unregistered; `chemclaw.connectors.registry` registers it.
    """
    params_model = _params_model(connector, job)

    async def launch(
        params: params_model,  # type: ignore[valid-type]
        rationale: str,
    ) -> str | ConnectorJobResult:
        # Reject-if-absent: a durable run with no recorded reason is refused, as a `ValueError` the
        # model can correct in the same turn. Checked here, not in `prepare_job_launch`, because the
        # template job step has no model to author a reason and records the template run instead.
        if not rationale.strip():
            raise ConnectorJobError(
                f"{job.name}: rationale must say why this run is being started — it is stored with "
                "the run and is the only record of what question it was meant to answer"
            )
        # The single shared pre-flight: validate, authorize, check the precondition.
        payload = prepare_job_launch(connector, job, params)
        workflow_id = job_workflow_id(connector, job.name, payload)
        # `require_actor` is the core rule: under Entra, refuse durable work with no user.
        requested_by = require_actor()
        plan_step, plan_hash = get_current_plan_link()
        # `connect()` frames an unreachable broker as a retryable `SubsystemUnavailableError`; do
        # not re-wrap it as `ConnectorJobError`, which is registered non-retryable.
        client = await connect()
        try:
            handle = await client.start_workflow(
                ConnectorJobWorkflow.run,
                ConnectorJobInput(
                    connector=connector,
                    job=job.name,
                    workflow=job.workflow,
                    task_queue=bundle_queue(connector),
                    payload=payload,
                    # Outside `payload`, hence outside `workflow_id`: identical requests with
                    # different reasons still rejoin one run.
                    rationale=rationale.strip(),
                    requested_by=requested_by,
                    session_id=get_current_session_id() or "",
                    correlation_id=get_current_correlation_id() or "",
                    # Bound per tool call by `agent.plan_link.stamp_plan_link`; ambient so the model
                    # cannot author an audit join key. Empty off the harness path.
                    plan_step=plan_step,
                    plan_hash=plan_hash,
                    publish_to_graph=job.publish_to_graph,
                    # The job's declared ceiling, or `None`; the worker starting the child clamps it
                    # to the deployment maximum
                    # (`durable/connector_job.py::child_execution_timeout`).
                    timeout_seconds=job.timeout_seconds,
                    # Copied from the manifest because a workflow may not read `connector.yaml` off
                    # disk.
                    awaits_answer=job.awaits_answer,
                    # What this job changes outside this deployment, copied from the manifest for
                    # the same reason.
                    effect_system=job.effect.system if job.effect else "",
                    effect_reversal=job.effect.reversal if job.effect else "",
                    # Read here, not in the workflow: a config read in workflow code would change
                    # the scheduled approval child on replay.
                    effect_approver=settings.effect_approval_role,
                    # Read here for the same replay reason; `open_pending_request_activity` applies
                    # the ceiling.
                    effect_approval_days=settings.effect_approval_deadline_days,
                ),
                id=workflow_id,
                task_queue=settings.background_task_queue,
                # Only a failed run may re-execute under the same id; a completed one is rejoined,
                # not recomputed.
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            # The identical job is already running or done: idempotency working. A finished run is
            # rejoined (a re-ask feels like a cache hit); a running one falls through to its id.
            rejoined = client.get_workflow_handle(workflow_id, result_type=ConnectorJobResult)
            if job.inline_wait_seconds is not None:
                existing = await _await_briefly(
                    rejoined, job.inline_wait_seconds, job.name, workflow_id
                )
                if existing is not None:
                    return existing
            # Announce only if the server says the run is still going, so a later `job_completed`
            # can clear it and the second requester is not left polling by hand.
            if await _still_running(rejoined):
                record_job_started(workflow_id, job.name)
            return workflow_id
        except Exception as exc:
            # Any other start failure (no worker registered, an RPC timeout, a serialization error)
            # must not reach the model raw. The server may have been reached, so this says only that
            # most likely nothing started, and to check before relaunching a job that writes.
            raise ConnectorJobError(
                f"the {job.name!r} job could not be confirmed as started ({type(exc).__name__}); "
                "most likely nothing was queued, but this call cannot promise that either way. "
                f"Check `get_durable_job_status({workflow_id!r})` before relaunching, and if it "
                "truly did not start, the same call will work once the fault clears."
            ) from exc
        # Counted as soon as `start_workflow` returns, because the inline-answer branch below
        # returns early. A rejoined run counts nothing: it starts nothing.
        record_metric(lambda m: m.increment("chemclaw_jobs_started_total"))
        if job.inline_wait_seconds is not None:
            finished = await _await_briefly(handle, job.inline_wait_seconds, job.name, workflow_id)
            if finished is not None:
                # It answered inside the turn, so there is no background work to announce and
                # nothing for the chemist to poll — the result *is* the tool's return value.
                return finished
        # Announced on a genuine start so the turn's event stream shows the launch while it is still
        # streaming.
        record_job_started(handle.id, job.name)
        return handle.id

    launch.__name__ = job.name
    launch.__qualname__ = job.name
    launch.__doc__ = _docstring(job)
    return launch


async def _still_running(handle: Any) -> bool:
    """Whether a run this launcher rejoined is still executing, per the server.

    Best-effort: a failed describe skips the announcement, never turns the rejoin into an error.
    `RUNNING` only, since a run that ended badly will never emit the `job_completed` that clears an
    announced row.
    """
    try:
        description = await handle.describe()
    except Exception:
        # Not an error, but a broker that keeps failing `describe()` silently disables rejoin
        # announcements, so count it.
        record_metric(lambda m: m.increment("chemclaw_rejoin_describe_failed_total"))
        logger.debug("could not describe rejoined run %s; not announcing it", handle.id)
        return False
    return bool(description.status == WorkflowExecutionStatus.RUNNING)


async def failed_job_reason(handle: Any) -> str:
    """Why a durable run that did not complete ended the way it did, in one readable sentence.

    The single answer used by the status tool, the in-turn wait and the push-back. `handle.result()`
    on a closed run is a history read, so this is one round trip. `WorkflowFailureError` covers
    failed, cancelled, terminated and timed-out runs alike, so it is the only clause; its
    `__cause__` carries the real reason. Transport errors (`RPCError`) propagate: a broker fault is
    not this run's failure reason.

    Args:
        handle: A `WorkflowHandle` for a run believed to have ended badly. Typed `Any` because the
            handle's own generic parameters differ per caller and none of them is used here.

    Returns:
        The failure's own sentence, or `""` if the run actually completed.
    """
    try:
        await handle.result()
    except WorkflowFailureError as exc:
        # `cause` depends on how the run ended; `failure_reason` falls back to the type name when
        # there is no message.
        return failure_reason(exc.__cause__ or exc)
    return ""


async def _await_briefly(
    handle: Any, budget: float, job_name: str, workflow_id: str
) -> ConnectorJobResult | None:
    """Wait up to `budget` seconds for a started job, or `None` if it is still running.

    `None` means "not finished yet", never "failed": a workflow failure raises `ConnectorJobError`
    with the run's own reason. The guard lives here, the only function that awaits, so both the
    fresh and the rejoined branch get it.

    Cancel-safe: `asyncio.wait_for` cancels only the waiter, so an abandoned turn leaves the run to
    complete, cache and push back. The result is validated through `envelope_from_result` (the same
    decode the agent's waiters use) and neutralised before return, because it goes straight into the
    model's context unframed and carries bundle- and requester-authored text.
    """
    try:
        finished = await asyncio.wait_for(handle.result(), budget)
    except TimeoutError:
        return None
    except WorkflowFailureError as exc:
        # A failure inside the turn must say why, or the model reads it as "proceed". Use
        # `__cause__`: the client wraps every failure in a generic `WorkflowFailureError`, and
        # `failure_reason` walks the workflow-side frames below it.
        raise ConnectorJobError(
            f"the {job_name!r} job ran and failed: {failure_reason(exc.__cause__ or exc)}"
        ) from exc
    return defanged_payload(envelope_from_result(workflow_id, finished))
