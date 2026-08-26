"""One generated agent tool per declared job — the four bespoke adapters, written once.

`agents/qm_tools.py::submit_qm_job` and the three launchers in `agent/durable_tools.py` were the
same handful of lines four times: authorize the trigger, demand an actor, derive a deterministic
workflow id, start the workflow, announce the launch, return the id. Only
the workflow class and the id derivation differed — and both are now manifest data
(`JobSpec.workflow`, plus the job name and the arguments the id hashes). So the adapter becomes
a factory: one function built per declared job, with the shared body written in exactly one
place. The QM launcher was the last of the four to go (D-118); this is now the *only* way a
durable capability is launched from a conversation.

The generated tool is a *first-class* tool, not a special case. It is registered through the
same `chemclaw.core.tool_registry.register_tool` a hand-written tool uses, keyed by the manifest's
`name`, which is what makes every existing mechanism apply to it untouched: the audit middleware
wraps it, the per-tool authorization gate addresses it by that name (`tool_role_gates`, and the
built-in `DEFAULT_WRITE_TOOL_GATES` for a job that writes), a profile can narrow it away, and
`chemclaw.cli.validate_prose_contract` sees it when checking the agent's prose.

Why a generated pydantic model rather than a `dict` parameter: a tool's JSON schema is derived
from its signature, and a single pydantic-model parameter is already the in-repo idiom for a
structured argument (`start_optimization_campaign(spec: CampaignSpec)`). Building that model
from the declared
`params` gives the model a real, typed, per-field-documented schema — an untyped `dict[str, Any]`
would advertise "pass anything", which is precisely how a model calls a tool wrongly.
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
from chemclaw.connectors.manifest import JobParamType, JobSpec
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric
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


class ConnectorJobError(ChemclawError):
    """A declared job cannot be built (a bad `params_model` reference) or launched as asked.

    A `ValueError` subclass for the same reason `ConnectorError` is: one `except ValueError` at an
    entry point should catch all of them.

    It covers both a misconfigured deployment and a caller that asked for something this seam
    refuses — today, a launch with no stated reason (D-157). Both are "this job is not going to
    run, and here is the sentence explaining why", and the audience differs only in who reads it:
    an operator for the first, the model itself for the second.
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


# One generated params class per (connector, job definition), because a *class* is an identity and
# two of them for one job is a bug waiting to be found. `create_model` returns a fresh class every
# call, so `isinstance` and `model_validate` reject a spec built from the other one — which is
# exactly what happened the moment the pre-flight moved into `prepare_job_launch` and started
# generating its own. Keyed on the job's serialized definition rather than its name so a test that
# reloads the manifests with different content gets a matching class rather than a stale one; the
# `JobSpec` itself cannot be the key because pydantic's frozen hash trips over its list fields.
_PARAMS_MODELS: dict[tuple[str, str], type[BaseModel]] = {}


def _params_model(connector: str, job: JobSpec) -> type[BaseModel]:
    """The pydantic model for one job's launch arguments — referenced, or generated from `params`.

    A generated field carries its manifest `description`, so the JSON schema documents every
    argument for the model reading it. An optional param defaults to `None` and widens to `T |
    None`, because "the caller may omit this" and "the value may be absent" must agree — a
    required-typed field with a `None` default would validate a payload the workflow cannot use.

    Memoized: see `_PARAMS_MODELS`. A referenced `params_model` is already a single class by
    construction and needs no help, but it goes through the same lookup so callers never have to
    know which kind of job they hold.
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


# The `rationale` argument, documented once for every generated job tool (D-157). Written at the
# model rather than at the developer, because this is the text that decides whether the stored
# reason is a usable sentence or a restatement of the arguments.
_RATIONALE_DOC = [
    "    rationale: Why this run is worth doing, in a sentence or two a chemist would recognise:",
    "        the question it should answer and what prompted it (whose request, which earlier",
    "        result). It is stored with the run and stamped onto any note the run proposes, so a",
    "        later session — or the human reviewing that note — can tell why it was done. Say what",
    "        the run is *for*; do not restate the arguments.",
]


def _docstring(job: JobSpec) -> str:
    """Assemble the tool docstring the model reads: summary, description, then the arguments.

    The tool description is derived from the docstring, so this *is* the job's model-facing
    documentation. The `Args:` section is rendered from the same declared params the schema is
    built
    from, so a parameter can never be documented in the prose but missing from the signature — the
    drift a hand-written adapter invites.
    """
    lines = [job.summary]
    if job.description:
        lines.extend(["", job.description.strip()])
    lines.extend(["", "Args:"])
    if job.params:
        lines.append("    params: The job's launch arguments.")
        lines.extend(f"        {param.name}: {param.description}" for param in job.params)
    elif job.params_model is not None:
        # A referenced model documents its own fields (their descriptions travel in the JSON
        # schema derived from it), so repeating them here would be a second, drift-prone
        # copy.
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
    """The deterministic id of one connector job run — the idempotency key (D-011).

    Public because it is the *contract*, not an implementation detail: a duplicate launch must
    resolve to this id, and Stage B's migration test asserts the ids the four bespoke adapters
    produced are reproduced exactly, so no in-flight workflow history is orphaned.
    """
    return f"{connector}-{job}-{stable_hash([connector, job, payload])}"


def prepare_job_launch(connector: str, job: JobSpec, params: Any) -> dict[str, Any]:
    """Everything that must be true before a job's durable work starts, and the payload it yields.

    Validate → authorize the expensive trigger → run the declared precondition → serialize. One
    function because there is more than one launcher: the agent tool below, and the template
    workflow's job step (`chemclaw.durable.template_activities.authorize_job_step`).

    **It is one function because it was two, and one of them was empty** (D-168). The template's
    `ResolvedJob` carried the connector, workflow and queue and dropped `expensive` and
    `precondition` on the floor, so a template naming an expensive job started it for anyone
    entitled to
    run the *template*, and a job's own domain guard — the one `JobSpec.precondition` documents as
    having no other replay-safe home — never ran on that path at all. Duplicating the four steps
    would have fixed today's instance and left the next launcher to rediscover it.

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
    # **Validate here, because nothing upstream does** (D-138). The params model's JSON schema is
    # published, but the tool body is handed the decoded JSON *object* — a plain `dict` — rather
    # than a constructed model. Until this call existed every declared job died on `'dict'
    # object has no attribute 'model_dump'` the first time a chemist asked for one, and the
    # precondition below was handed a dict whose attributes it could not read. Accept an
    # already-built model too: a caller that holds one (a test, a template step) is not wrong, and
    # `model_validate` is the one entry point that takes either.
    spec = _params_model(connector, job).model_validate(params)
    # Authorize the expensive trigger against the turn's user *before* any durable work (F4-T5), so
    # an autonomously-planned todo — or a template step — cannot start a costly run outside the
    # user's entitlements.
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

    The returned coroutine function is what gets advertised: its `__name__` is the manifest's job
    name (which is also the authorization key and the profile-narrowing key), its docstring is
    the model-facing description, and its single parameter is the generated params model.

    Args:
        connector: The owning connector's name — part of the workflow id, and reported in the
            push-back payload so a completion can be traced back to its capability.
        job: The declared job.

    Returns:
        An async tool function, unregistered — the registry call belongs to the caller that
        knows which connectors are enabled (`chemclaw.connectors.registry`).
    """
    params_model = _params_model(connector, job)

    async def launch(
        params: params_model,  # type: ignore[valid-type]
        rationale: str,
    ) -> str | ConnectorJobResult:
        # Reject-if-absent, the polarity `require_actor` established (F4-T3): a durable run with no
        # recorded reason is the gap D-157 exists to close, and a blank string accepted here
        # would reopen it silently for every job in the system. Raised as a `ValueError` the model
        # reads and can correct in the same turn, before any durable work is started.
        #
        # Checked *here* rather than inside `prepare_job_launch`, because `rationale` is an argument
        # of this tool and not a property of the job: the template job step shares the pre-flight
        # but has no model to author a sentence, and it records the template run instead (D-168).
        if not rationale.strip():
            raise ConnectorJobError(
                f"{job.name}: rationale must say why this run is being started — it is stored with "
                "the run and is the only record of what question it was meant to answer"
            )
        # Validate, authorize and check the domain precondition — all of it in `prepare_job_launch`,
        # which is the *only* pre-flight and is shared with the template job step (D-168). It used
        # to live inline here, which is why the template path had none of it.
        payload = prepare_job_launch(connector, job, params)
        workflow_id = job_workflow_id(connector, job.name, payload)
        # `require_actor` is the core rule (F4-T3): under Entra, refuse durable work with no user.
        requested_by = require_actor()
        # An unreachable broker is framed by `connect()` itself, as `SubsystemUnavailableError` —
        # one client, one message, and `chemclaw.agent.tool_authz.surface_domain_errors` hands it to
        # the model verbatim. This site used to re-frame it as a `ConnectorJobError`, which was the
        # same sentence maintained twice and, worse, mislabelled a *retryable* outage as bad data:
        # `ConnectorJobError` is registered non-retryable in `chemclaw.durable.publish`, and this
        # tool does run inside an activity on the template path (`durable.template_activities`).
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
                    # Deliberately outside `payload`, and therefore outside `workflow_id`: two
                    # chemists asking for the identical campaign with differently-worded reasons
                    # must still rejoin one run rather than each paying for it (D-011).
                    rationale=rationale.strip(),
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
            rejoined = client.get_workflow_handle(workflow_id, result_type=ConnectorJobResult)
            if job.inline_wait_seconds is not None:
                existing = await _await_briefly(
                    rejoined, job.inline_wait_seconds, job.name, workflow_id
                )
                if existing is not None:
                    return existing
            # Announced only when the run is *still going*, which is a question the server can
            # answer rather than one this branch has to guess at. It used to guess: the comment
            # here said an announcement was withheld because the run "may already be finished" and
            # a surface would draw a row that never clears — true, and it left the other case with
            # nothing at all. A second chemist asking for a job the first started got no
            # `job_started`, so no `job_completed` could be matched to it either, and
            # `agent/job_results.py` had nothing to wait on: they were told "in progress" and had
            # to poll by hand forever. `describe()` is exactly the distinction, one round trip on
            # a path that has already given up on being fast.
            if await _still_running(rejoined):
                record_job_started(workflow_id, job.name)
            return workflow_id
        except Exception as exc:
            # `connect()` above already frames the broker being unreachable; this is the
            # remaining gap the 2026-08-02 incident exposed one call later — a task queue with no
            # worker registered, a transient RPC timeout, a payload serialization error — all of
            # which reached the model raw as "Error: Function failed." and reproduced the exact
            # retry storm the `connect()` framing exists to prevent. Unlike that case, a connected
            # client may have reached the server before failing, so this cannot promise nothing
            # started —
            # it says only what it actually knows: most likely nothing did, but check before
            # relaunching a job that writes.
            raise ConnectorJobError(
                f"the {job.name!r} job could not be confirmed as started ({type(exc).__name__}); "
                "most likely nothing was queued, but this call cannot promise that either way. "
                f"Check `get_durable_job_status({workflow_id!r})` before relaunching, and if it "
                "truly did not start, the same call will work once the fault clears."
            ) from exc
        # Counted here rather than after the inline wait, and that placement is the 2026-08-05
        # review's finding: `start_workflow` has returned, so a workflow *did* start, and the
        # branch below returns without reaching any later statement whenever the job answers
        # inside the turn. Five of the seven declared jobs carry `inline_wait_seconds` (every
        # `calc` job), so counting after the wait meant the common case was never counted at all
        # while `chemclaw_job_runtime_seconds_total` — booked from the job record, which is
        # written either way — kept counting its runtime. Starts and runtime were being read off
        # the same dashboard and only one of them was true. The re-joined path above still counts
        # nothing, correctly: it returns an existing id without starting anything.
        record_metric(lambda m: m.increment("chemclaw_jobs_started_total"))
        if job.inline_wait_seconds is not None:
            finished = await _await_briefly(handle, job.inline_wait_seconds, job.name, workflow_id)
            if finished is not None:
                # It answered inside the turn, so there is no background work to announce and
                # nothing for the chemist to poll — the result *is* the tool's return value.
                return finished
        # Announced only on a *genuine* start that is still running: the turn's event stream shows
        # the launch while the turn is still streaming (D-042). A re-joined run is deliberately
        # silent — it may already be finished, and no surface would ever get the matching
        # `job_completed` event to clear the row it drew.
        #
        # There used to be a second announcement here, opening a harness todo that recorded the
        # plan as blocked on this id. Its stated purpose was so the framework's `todos_remaining`
        # loop predicate saw "waiting" rather than re-invoking the model with nothing new (D-040).
        # That predicate is gone with the framework — the graph's loop ends when the model stops
        # calling tools, so an open todo cannot drive one — and with it the reason for the todo.
        record_job_started(handle.id, job.name)
        return handle.id

    launch.__name__ = job.name
    launch.__qualname__ = job.name
    launch.__doc__ = _docstring(job)
    return launch


async def _still_running(handle: Any) -> bool:
    """Whether a run this launcher rejoined is still executing, per the server.

    Best-effort by construction: this is asked only to decide whether to *announce* a rejoined run,
    so a describe that fails means the announcement is skipped and the caller still returns the id.
    Raising here would turn a successful idempotent rejoin into a tool error over a question the
    caller could live without an answer to.

    `RUNNING` alone, not "not completed": a run that failed, was cancelled or timed out will never
    emit the `job_completed` a surface needs to clear the row an announcement draws, which is the
    hazard the silent branch was built around and is still right about.
    """
    try:
        description = await handle.describe()
    except Exception:
        # See the docstring: an unanswered question here is not an error.
        logger.debug("could not describe rejoined run %s; not announcing it", handle.id)
        return False
    return bool(description.status == WorkflowExecutionStatus.RUNNING)


async def _await_briefly(
    handle: Any, budget: float, job_name: str, workflow_id: str
) -> ConnectorJobResult | None:
    """Wait up to `budget` seconds for a started job, or `None` if it is still running.

    `None` means "not finished yet", never "failed": a genuine workflow failure is framed here as a
    `ConnectorJobError` carrying the run's own reason, so the tool reports the error rather than
    silently degrading to a job id the chemist would poll forever waiting for a run already dead.

    **The framing lives here rather than at the call site, and that placement is the 2026-08-05
    review's finding.** It was written at one of the two sites that await: the freshly-started
    branch had it, the *re-joined* branch — a job already running when a second chemist asks for it
    — did not, so a rejoined run that failed raised a raw `WorkflowFailureError`, which is
    neither a `ChemclawError` nor a `SubsystemUnavailableError` and so reaches the model as
    "Error: Function failed.". That is the fourth appearance of one defect, and copying the guard to
    a second call site would only have set up the fifth. A guard on the *only* function that awaits
    cannot be forgotten by a third caller.

    The wait is cancel-safe by construction — `asyncio.wait_for` cancels only the *waiter*, and
    the workflow it is waiting on keeps running on its worker. So a turn that times out or is
    abandoned mid-wait leaves a durable run that still completes, still caches its result and
    still pushes back to the session. That is the property that makes this safe to do inside a
    conversation at all.

    The result is *validated*, not cast: the envelope is the connector contract, and a
    bundle-authored workflow returning some other shape should fail here by name rather than hand
    the model an unlabelled dict to interpret. It validates through `envelope_from_result`, the
    same decode the agent's two waiters use — this site used to call `model_validate` itself and
    so answered the identical bad result with a raw pydantic `ValidationError`, which
    `_sanitize_tool_errors` passes through as a written domain message because it is a `ValueError`.
    Two collectors, one bad input, two different things said to the chemist.
    """
    try:
        finished = await asyncio.wait_for(handle.result(), budget)
    except TimeoutError:
        return None
    except WorkflowFailureError as exc:
        # A job that fails *inside* the turn must say why, in words the model can relay. The
        # pattern is always the same — a failure that reaches the model wordless is not read as
        # "this failed", it is read as "proceed" — so a real failure gets a real sentence, and
        # `failure_reason` gives it the same one the session push-back carries.
        #
        # `exc.__cause__` and not `exc`: the client wraps every workflow failure in
        # `WorkflowFailureError("Workflow execution failed")`, and `failure_reason` only skips the
        # two workflow-side frames (see its docstring — the client type is deliberately not named
        # inside the workflow sandbox). Measured chain for the live failure: WorkflowFailureError →
        # ChildWorkflowError → ActivityError → "unknown ALPB solvent '2-methyltetrahydrofuran';
        # common valid names are …" → the tblite internals. Passing `exc` straight in stops at the
        # first frame and reports "Workflow execution failed", the generic sentence this replaces.
        raise ConnectorJobError(
            f"the {job_name!r} job ran and failed: {failure_reason(exc.__cause__ or exc)}"
        ) from exc
    return envelope_from_result(workflow_id, finished)
