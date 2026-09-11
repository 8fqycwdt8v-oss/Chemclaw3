"""Templates as files: discovery, validation, and the tool that starts one.

The same shape as every other extension seam here, deliberately — discovered by folder, validated by
a pydantic model, enabled by one config token, checked in CI — so a template is one more thing an
author drops in a directory rather than a new mechanism to learn.

Starting a template reuses the durable-job machinery rather than inventing a second one: each
template becomes a generated `run_<name>` tool that starts `TemplateWorkflow`, with the same
deterministic id, the same `require_actor`, the same dry-run gate and the same launch signal a
connector job gets. The one thing it does *not* reuse is `ConnectorJobWorkflow` — a template is
not a connector's job, it is core's own sequencer, so wrapping one in the other would be a wrapper
around a wrapper with nothing in between.
"""

import logging
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, create_model
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from chemclaw.agent.authz import require_actor
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_roles
from chemclaw.core.ids import stable_hash
from chemclaw.core.manifest_io import read_manifest
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect
from chemclaw.core.tool_registry import CapabilityTool
from chemclaw.core.turn_signals import record_job_started
from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow
from chemclaw.templates.manifest import InputType, Template

logger = logging.getLogger(__name__)

# The declared input types, mapped to annotations for the generated tool's params model — the same
# closed set a connector job's inline params use, so an author meets one vocabulary, not two.
_INPUT_ANNOTATIONS: dict[InputType, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "string[]": list[str],
    "number[]": list[float],
    "object": dict[str, Any],
}


class TemplateError(ChemclawError):
    """A template file is malformed, or an enabled template does not exist.

    A `ChemclawError` (so a `ValueError`) and registered in
    `chemclaw.durable.publish._BAD_DATA_TYPES` by its own class name: Temporal matches
    non-retryable error types by exact name, not isinstance, so a raw `ValueError` subclass would
    still retry across an activity boundary.
    """


def _load(path: Path) -> Template:
    """Parse and validate one template file, whose stem is its name."""
    raw = read_manifest(path, TemplateError)
    if "name" in raw:
        raise TemplateError(
            f"{path}: a template's name is its filename; remove the 'name' key so the two "
            "cannot disagree"
        )
    try:
        return Template(name=path.stem, **raw)
    except ValidationError as exc:
        raise TemplateError(f"{path}: invalid template: {exc}") from exc


@cache
def _discovered_in(dirs: tuple[str, ...]) -> dict[str, Template]:
    """Every template found under `dirs`, by name, validated. Cached on `dirs`, like connectors.

    **Keyed on the directories because they are the input.** This was `@cache` on a zero-argument
    `discovered()` reading `settings.templates_dirs` itself, so the key omitted the only thing the
    answer depends on and a test repointing `templates_dir` poisoned every later test in the
    process. See `chemclaw.connectors.registry._discovered_in`, which carries the whole argument.

    Two distinctness rules, because the file name and the *tool* name are different namespaces and
    only the second is the one a turn uses. `tool_name` folds a hyphen to an underscore, so
    `probe-x.yaml` and `probe_x.yaml` are two templates and one `run_probe_x` — distinct by the
    first rule, colliding under the second. Left to be discovered at build time, that is not a
    mis-run template but a dead deployment: `register_tool` raises the first time the agent is
    built, so **every** turn fails, with a message naming neither file. It is refused here instead,
    where both files can be named.
    """
    found: dict[str, Template] = {}
    claimed: dict[str, str] = {}
    for directory in dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.yaml")):
            template = _load(path)
            if template.name in found:
                raise TemplateError(f"{path}: template {template.name!r} is already defined")
            generated = tool_name(template)
            if generated in claimed:
                raise TemplateError(
                    f"{path}: template {template.name!r} generates tool {generated!r}, which "
                    f"template {claimed[generated]!r} already claims — a hyphen and an underscore "
                    "are the same character in a tool name, and the second registration raises "
                    "on every turn"
                )
            claimed[generated] = template.name
            found[template.name] = template
    return found


def discovered() -> dict[str, Template]:
    """Every discovered template by name, validated.

    The settings read is here rather than inside the cache, so a `templates_dir` changed
    mid-process is seen on the next call instead of being answered from the old directory's entry.
    """
    return _discovered_in(tuple(settings.templates_dirs))


def forget_discovered() -> None:
    """Drop the cache so the next `discovered()` re-reads templates from disk.

    **The one case a directory-keyed cache cannot see on its own**: new manifests written into a
    directory this registry has *already* discovered. The key is the directory tuple, so it is
    unchanged and the entry still answers. Repointing `templates_dir` needs no clearing at all,
    because that is a different key.

    A named function rather than `discovered.cache_clear`, which is what this was for a few hours.
    An attribute assigned onto a function object is invisible to `mypy`: the definition needed a
    `# type: ignore[attr-defined]` and **every one of the 35 call sites became an error**, so the
    suppression at the definition bought silence in one place and noise in thirty-five. The tree
    already had the right idiom for a test-isolation reset — `forget_reachability`,
    `forget_vector_store`, `forget_open_warehouses` — and this is it.
    """
    _discovered_in.cache_clear()


def enabled() -> list[Template]:
    """The templates this deployment turns on; empty enable-list means every discovered one.

    **`templates_enabled` is the only filter here, and deliberately still is.** Whether a
    template's steps resolve against the connectors this deployment actually runs is a separate
    question, asked at launch by `unrunnable_reason` rather than here — withdrawing the launcher
    would break every profile that names it, which is measured in that docstring.
    """
    found = discovered()
    names = settings.templates_enabled_list
    if not names:
        return list(found.values())
    unknown = sorted(set(names) - found.keys())
    if unknown:
        raise TemplateError(
            f"templates_enabled names unknown template(s) {unknown}; discovered: {sorted(found)}"
        )
    return [found[name] for name in names]


def tool_name(template: Template) -> str:
    """The advertised name of the tool that runs `template`.

    Prefixed rather than bare so a template cannot collide with a tool or a connector job — those
    share one namespace (it is the authorization key), and a template named `screen_hazards`
    silently shadowing the real screen is not a failure anyone would enjoy debugging.

    It is **not** injective over template names: the hyphen-to-underscore fold means two distinct
    templates can generate one tool. `discovered` is where that is refused.
    """
    return f"run_{template.name.replace('-', '_')}"


def _params_model(template: Template) -> type[BaseModel]:
    """Build the params model for a template's declared inputs (the generated tool's schema)."""
    fields: dict[str, Any] = {}
    for item in template.inputs:
        annotation = _INPUT_ANNOTATIONS[item.type]
        if item.required:
            fields[item.name] = (annotation, Field(description=item.description))
        else:
            fields[item.name] = (
                annotation | None,
                Field(default=None, description=item.description),
            )
    camel = "".join(part.capitalize() for part in template.name.replace("-", "_").split("_"))
    return create_model(
        f"{camel}Inputs",
        __doc__=f"Inputs for the {template.name!r} template.",
        **fields,
    )


def _docstring(template: Template) -> str:
    """The generated tool's docstring: summary, description, inputs, and how to follow up."""
    lines = [template.summary]
    if template.description:
        lines.extend(["", template.description.strip()])
    lines.extend(
        [
            "",
            "Runs a fixed, auditable sequence of steps as a durable job — the order is defined by "
            f"the {template.name!r} template, not chosen per call.",
        ]
    )
    if template.inputs:
        lines.extend(["", "Args:", "    params: The template's inputs."])
        lines.extend(f"        {item.name}: {item.description}" for item in template.inputs)
    lines.extend(
        [
            "",
            "Returns:",
            "    The job id to poll with `get_durable_job_status`. Re-running with identical",
            "    inputs returns the existing job id rather than starting a second run.",
        ]
    )
    return "\n".join(lines)


def run_workflow_id(template: Template, inputs: dict[str, Any]) -> str:
    """The deterministic id of one template run — the idempotency key, as for a connector job."""
    return f"template-{template.name}-{stable_hash([template.name, inputs])}"


async def _still_running(handle: Any) -> bool:
    """Whether a run this launcher rejoined is still executing, per the server.

    Best-effort by construction: this is asked only to decide whether to *announce* a rejoined run,
    so a describe that fails means the announcement is skipped and the caller still returns the id.
    Raising here would turn a successful idempotent rejoin into a tool error over a question the
    caller could live without an answer to.

    **The same question `connectors/jobs.py` asks, and deliberately not the same function.**
    `templates -> connectors` is not an edge this architecture has (`tests/test_layering.py`), and
    a template launcher must not acquire one to ask a broker whether a run is open; the shared home
    for it would be `core` or `durable`, which is a move rather than a fix and is recorded as such.
    The counter is shared, because it counts the same event on the same dashboard.
    """
    try:
        description = await handle.describe()
    except Exception:
        record_metric(lambda m: m.increment("chemclaw_rejoin_describe_failed_total"))
        logger.debug("could not describe rejoined template run %s; not announcing it", handle.id)
        return False
    return bool(description.status == WorkflowExecutionStatus.RUNNING)


def unrunnable_reason(template: Template) -> str:
    """Why this deployment cannot run `template`, or `""` when it can.

    **The runtime half of `make template-validate`.** That gate has always been able to say a
    template names a tool, a job or a profile that does not exist — at a deployment with no `calc`
    bundle it reports *"template 'bond-strength-survey' step 'survey' runs unknown job
    'survey_bond_strengths'; declared jobs: []"* and exits 1 — and nothing at run time asked it.
    `enabled()` filters on `templates_enabled` alone, so all nine `run_*` launchers were bound
    whatever the connector set, a chemist could start a bond-dissociation survey into a fleet that
    does not exist, and the system prompt then told the model to report the id as work in progress
    and poll it.

    **How badly that ends depends on who polls the queue, and both endings are bad.** Where nothing
    polls `background-jobs` — a dev process with no worker, or a fleet scaled to zero — the run
    reports `running` for as long as anybody asks, and `find_past_jobs` finds nothing, because a
    run that reaches no step records none. Under the Helm chart it does not: measured on the
    rendered manifests, `deployment-workers.yaml` always emits a background worker at
    `workers.background.replicas` (1, pinned), so the run starts, reaches the step, and
    `authorize_job_step` fails it non-retryably — a wasted launch and a named failure some minutes
    later rather than a promise that never resolves. Neither is worth starting.

    **This refuses the launch; it does not withdraw the tool.** Not registering an unrunnable
    launcher is the obvious answer and it is measurably worse: `data/profiles/computation.yaml`
    names eight of them and `safety.yaml` names the ninth, and `chemclaw_agent`'s
    `_reject_unknown_tool_names` *raises* when a profile lists a tool the surface does not provide.
    Measured with only the `results` bundle enabled, withdrawing them takes two shipped profiles
    from "one procedure is unavailable" to "every turn on this profile fails at build" — the dead
    deployment `discovered()`'s own docstring argues against, arrived at from the other side. A
    refusal that names the missing job is also the more useful answer: absence tells the model
    nothing, and this tells it (and the operator reading the log) exactly which capability is
    missing.

    `step_problems` is the gate's own function rather than a second reading of the same rule —
    two copies of "what resolves" is the defect class this repository keeps finding. Resolved
    without signatures, because the argument half of that check imports every bundle's server
    module for 14 s and a launch must not pay it; an empty signature map makes the argument check
    silent, which is what it already is for every tool it cannot resolve.

    Imported lazily because `chemclaw.agent.chemclaw_agent` imports this module at import time —
    the edge is `templates -> agent`, which the architecture has, and taking it at module scope
    would be a cycle rather than a layering violation.

    Args:
        template: The template a launcher is about to start.

    Returns:
        The problems, one per line, or `""` when every step resolves.
    """
    from chemclaw.agent.template_surface import TemplateSurface, step_problems

    problems = step_problems(template, TemplateSurface.resolve(with_signatures=False))
    return "\n".join(f"  - {problem}" for problem in problems)


def build_template_tool(template: Template) -> CapabilityTool:
    """Build the agent tool that starts one template run."""
    params_model = _params_model(template)

    async def launch(params: params_model) -> str:  # type: ignore[valid-type]
        # **Validate here, because nothing upstream does.** The annotation above is a pydantic model
        # and its JSON schema is published, but the body is handed the decoded JSON *object* — a
        # plain `dict`. The `cast` this replaces was a static no-op, so every template run died on
        # `'dict' object has no attribute 'model_dump'` the first time a chemist asked for one, and
        # the shipped `hazard-briefing` template had never once executed from a conversation.
        #
        # `connectors/jobs.py` learned this as D-138 and was fixed there; the template seam kept the
        # assumption because the fix was applied where the bug was seen rather than everywhere it
        # lived. `model_validate` is the one entry point that accepts either a dict or an
        # already-built model, so a caller holding one (a test, a step) is still not wrong.
        spec = params_model.model_validate(params)
        # **Before the actor, the client and the start** — see `unrunnable_reason`. A template
        # whose steps do not resolve against this deployment's surface cannot produce anything, so
        # the only honest outcome is a refusal that names what is missing — rather than a workflow
        # that fails several minutes in, or never resolves at all where nothing polls the queue.
        #
        # WARNING rather than only raising, because the two readers need different things: the
        # model is told a capability is missing, and an operator needs to see *which bundle* to
        # enable without reading a chat transcript.
        blocked = unrunnable_reason(template)
        if blocked:
            logger.warning(
                "template %r cannot run at this deployment; refusing to start it:\n%s",
                template.name,
                blocked,
            )
            raise TemplateError(
                f"the {template.name!r} template cannot run at this deployment, so nothing was "
                f"queued:\n{blocked}\nThis is the connector set this deployment runs, not a bad "
                "request: the same problem `make template-validate` reports. Do not retry it — "
                "use the tools that are available, or ask for the missing capability to be "
                "enabled."
            )
        inputs: dict[str, Any] = spec.model_dump(mode="json", exclude_none=True)
        workflow_id = run_workflow_id(template, inputs)
        requested_by = require_actor()
        client = await connect()
        try:
            handle = await client.start_workflow(
                TemplateWorkflow.run,
                TemplateRunInput(
                    # The resolved template, pinned into the run — an edit afterwards cannot change
                    # what is already executing (`workflows.template_job`).
                    template=template,
                    inputs=inputs,
                    requested_by=requested_by,
                    roles=sorted(get_current_roles()),
                    session_id=get_current_session_id() or "",
                ),
                id=workflow_id,
                task_queue=settings.background_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                # The run-level ceiling, the same one `ConnectorJobWorkflow` gives the children it
                # starts (`durable/connector_job.py`) and for the same reason. There was none, so
                # an N-step template's only bound was `template_step_timeout_seconds` × N — a
                # product nothing declares, that grows silently when an author adds a step, and
                # that no operator can read off any setting. A per-step timeout bounds a wedged
                # *step*; only this bounds a wedged *procedure*.
                execution_timeout=timedelta(seconds=settings.template_run_timeout_seconds),
            )
        except WorkflowAlreadyStartedError:
            # The identical run is already going, or already done: the idempotency contract
            # succeeding. **Announced when it is still going**, which is the half this branch used
            # to skip — it returned the id and told nobody, so no `JobSignal` reached the turn, the
            # session's `started_jobs` stayed empty, `agent/job_results.py` had nothing to wait on,
            # and the second chemist to ask for a running template was told "in progress" with no
            # row a later `job_completed` could clear. That is the defect `connectors/jobs.py`
            # documents having fixed for jobs, one seam over, unfixed here.
            #
            # `RUNNING` and not "not completed", for that launcher's reason: a run that failed, was
            # cancelled or timed out will never emit the completion an announced row waits for.
            if await _still_running(client.get_workflow_handle(workflow_id)):
                record_job_started(workflow_id, f"template:{template.name}")
            return workflow_id
        except Exception as exc:
            # `connect()` above frames an unreachable broker; this is the call *after* it — a
            # queue with no worker, a transient RPC timeout, a serialization error — which escaped
            # raw, so `surface_domain_errors` classified an `RPCError` as neither a `ChemclawError`
            # nor a transport failure and the model was handed `unexpected_error_result()`. The
            # sibling launcher's framing, with its promise kept as narrow: a connected client may
            # have reached the server before failing, so this cannot say nothing started.
            raise TemplateError(
                f"the {template.name!r} template could not be confirmed as started "
                f"({type(exc).__name__}); most likely nothing was queued, but this call cannot "
                f"promise that either way. Check `get_durable_job_status({workflow_id!r})` before "
                "relaunching, and if it truly did not start, the same call will work once the "
                "fault clears."
            ) from exc

        record_job_started(handle.id, f"template:{template.name}")
        return handle.id

    launch.__name__ = tool_name(template)
    launch.__qualname__ = launch.__name__
    launch.__doc__ = _docstring(template)
    return launch


def template_tools() -> list[CapabilityTool]:
    """One generated launcher per enabled template, each refusing what it cannot run.

    Every enabled template gets a launcher whatever the connector set — see `enabled` — and the
    launcher checks `unrunnable_reason` before it queues anything.
    """
    return [build_template_tool(template) for template in enabled()]


def template_tool_names() -> list[str]:
    """The advertised name of every enabled template's tool, for the validators to check."""
    return sorted(tool_name(template) for template in enabled())
