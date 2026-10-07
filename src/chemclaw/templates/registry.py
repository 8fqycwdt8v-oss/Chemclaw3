"""Templates as files: discovery, validation, and the tool that starts one.

The same shape as every other extension seam: discovered by folder, validated by a pydantic model,
enabled by config, checked in CI. Each template becomes a generated `run_<name>` tool that starts
`TemplateWorkflow` with the same deterministic id, `require_actor`, dry-run gate and launch signal a
connector job gets. It is not wrapped in `ConnectorJobWorkflow`, because a template is core's own
sequencer, not a connector's job.
"""

import logging
from collections.abc import Mapping
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
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

# Declared input types mapped to annotations for the generated tool's params model; the same closed
# set connector job params use.
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

    Registered by class name in `chemclaw.durable.publish._BAD_DATA_TYPES`, because Temporal matches
    non-retryable error types by exact name.
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

    Keyed on the directories because they are the input, so repointing `templates_dir` is a
    different cache entry.

    File names and tool names are both checked for distinctness: `tool_name` folds hyphens to
    underscores, so `probe-x.yaml` and `probe_x.yaml` would collide as `run_probe_x` and make
    `register_tool` fail every turn. It is refused here, where both files can be named.
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

    Settings are read outside the cache, so a changed `templates_dir` is seen on the next call.
    """
    return _discovered_in(tuple(settings.templates_dirs))


def forget_discovered() -> None:
    """Drop the cache so the next `discovered()` re-reads templates from disk.

    Needed only when new manifests are written into an already-discovered directory; repointing
    `templates_dir` is a different key. A named function, like `forget_reachability`, rather than
    exposing `cache_clear`, which mypy cannot see on the wrapper.
    """
    _discovered_in.cache_clear()


def enabled() -> list[Template]:
    """The templates this deployment turns on; empty enable-list means every discovered one.

    Filters on `templates_enabled` only: this is every launcher the tree can bind, read by
    validators and the prose contract. Which a turn actually binds is `bound()`.
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


def withheld_reason(template: Template) -> list[str]:
    """The opt-in capabilities whose absence withholds `template`'s launcher, or `[]` to bind it.

    Withheld only when its steps name a tool or job a bundle declares but this deployment does not
    bind (`agent/template_surface.unbound_opt_in_references`) and no profile lists the launcher. The
    first saves prompt prefix for an unusable capability; the second matters because withdrawing a
    profile-named launcher makes every turn on that profile fail at build. A tool nothing declares
    is a broken template and keeps its refusal-at-launch. Imported lazily to avoid an import cycle
    with `chemclaw.agent.chemclaw_agent`.
    """
    from chemclaw.agent.template_surface import profile_named_tools, unbound_opt_in_references

    missing = unbound_opt_in_references(template)
    # The profile half is asked only when the first fires, since it reads every profile file.
    if not missing or tool_name(template) in profile_named_tools():
        return []
    return missing


def bound() -> list[Template]:
    """The enabled templates whose launchers this deployment binds — `enabled()` minus the withheld.

    Not cached: the connector enable-list and profile files can change, and the per-turn caller is
    already bounded by its own once-per-process registry.
    """
    return [template for template in enabled() if not withheld_reason(template)]


def tool_name(template: Template) -> str:
    """The advertised name of the tool that runs `template`.

    Prefixed so a template cannot shadow a tool or connector job, which share the authorization
    namespace. Not injective (hyphens fold to underscores); `discovered` refuses collisions.
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
        # `forbid`, so a misspelled optional input is refused instead of silently dropped.
        __config__=ConfigDict(extra="forbid"),
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


def run_workflow_id(template: Template, inputs: dict[str, Any], scope: str = "") -> str:
    """The deterministic id of one template run — the idempotency key, as for a connector job.

    For a `data/templates/` file the name is the procedure, so name plus inputs is the key and
    identical requests share one run. A composed workflow's name is one chemist's and its steps
    change, so `scope` (owner and fingerprint) enters the id, prefixed `composed-`; otherwise a
    different document with the same name would rejoin another's run. Empty `scope` gives the
    unscoped id.
    """
    if not scope:
        return f"template-{template.name}-{stable_hash([template.name, inputs])}"
    return f"composed-{template.name}-{stable_hash([template.name, inputs, scope])}"


def _family(scope: str) -> str:
    """What a run of this kind is called on the session's started-jobs list.

    Composed runs are labelled as such, so a document the model just wrote is not mistaken for a
    reviewed file of the same name.
    """
    return "composed" if scope else "template"


async def _still_running(handle: Any) -> bool:
    """Whether a run this launcher rejoined is still executing, per the server.

    Best-effort: it only decides whether to announce a rejoined run, so a failed describe skips the
    announcement rather than failing the tool. Not shared with `connectors/jobs.py` because
    `templates -> connectors` is not a permitted import edge; the counter is shared.
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

    The runtime half of `make template-validate`, using the gate's own `step_problems` (without
    signatures, which would import every bundle's server module) plus `run_ceiling_problems`. It
    refuses the launch rather than withdrawing the tool, because withdrawing a profile-named
    launcher fails every turn on that profile, and a refusal naming the missing capability is more
    useful than absence. Imported lazily to avoid an import cycle.
    """
    from chemclaw.agent.template_surface import (
        TemplateSurface,
        run_ceiling_problems,
        step_problems,
    )

    # Two deployment facts: whether the steps resolve here, and whether
    # `template_run_timeout_seconds` can hold them. The second matters more because Temporal
    # terminates an over-ceiling run without its workflow code running, leaving no failure row or
    # session event.
    problems = [
        *step_problems(template, TemplateSurface.resolve(with_signatures=False)),
        *run_ceiling_problems(template),
    ]
    return "\n".join(f"  - {problem}" for problem in problems)


async def start_template_run(
    template: Template, inputs: Mapping[str, Any] | BaseModel, scope: str = ""
) -> str:
    """Start one run of `template` and return its job id, rejoining an identical run in flight.

    Shared by the generated `run_<name>` tools and `agent/workflow_tools.run_composed_workflow`, so
    both get the same resolvability refusal, input validation (`extra="forbid"`), deterministic id
    (see `run_workflow_id` for `scope`), run ceiling, idempotent rejoin and `JobSignal`. The
    template is pinned into the workflow input, so a later edit cannot change a running run.

    Raises:
        TemplateError: When this deployment cannot run the template, or the broker could not confirm
        the start.
    """
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
    # Dumped to a mapping first: `_params_model` builds a new class on every call, and
    # `model_validate` rejects an instance of another call's class.
    raw = inputs.model_dump(mode="json") if isinstance(inputs, BaseModel) else dict(inputs)
    try:
        resolved = (
            _params_model(template).model_validate(raw).model_dump(mode="json", exclude_none=True)
        )
    except ValidationError as exc:
        raise TemplateError(
            f"the inputs for the {template.name!r} template are not what it declares "
            f"({exc.error_count()} problem(s)): "
            + "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
                for error in exc.errors()
            )
            + f". It declares: {[item.name for item in template.inputs]}."
        ) from exc
    workflow_id = run_workflow_id(template, resolved, scope)
    requested_by = require_actor()
    client = await connect()
    try:
        handle = await client.start_workflow(
            TemplateWorkflow.run,
            TemplateRunInput(
                # Pinned into the run, so a later edit cannot change what is executing.
                template=template,
                inputs=resolved,
                requested_by=requested_by,
                roles=sorted(get_current_roles()),
                session_id=get_current_session_id() or "",
                # Pinned here so the bound the run enforces is the one `run_ceiling_problems` sized
                # it with.
                max_parallel_steps=settings.orchestrator_max_parallel_children,
            ),
            id=workflow_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            # The run-level ceiling, as `ConnectorJobWorkflow` gives its children: a per-step
            # timeout bounds a wedged step; only this bounds a wedged procedure.
            execution_timeout=timedelta(seconds=settings.template_run_timeout_seconds),
        )
    except WorkflowAlreadyStartedError:
        # The identical run is already going or done: idempotency succeeding. Still announced while
        # `RUNNING`, so the turn gets a `JobSignal` and a later `job_completed` has a row to clear.
        # Not for failed, cancelled or timed-out runs, which will never complete.
        if await _still_running(client.get_workflow_handle(workflow_id)):
            record_job_started(workflow_id, f"{_family(scope)}:{template.name}")
        return workflow_id
    except Exception as exc:
        # Frames a failure after `connect()` (no worker, RPC timeout, serialization error) as a
        # domain error. A connected client may have reached the server, so this cannot claim nothing
        # started.
        raise TemplateError(
            f"the {template.name!r} template could not be confirmed as started "
            f"({type(exc).__name__}); most likely nothing was queued, but this call cannot "
            f"promise that either way. Check `get_durable_job_status({workflow_id!r})` before "
            "relaunching, and if it truly did not start, the same call will work once the "
            "fault clears."
        ) from exc

    record_job_started(handle.id, f"{_family(scope)}:{template.name}")
    return handle.id


def build_template_tool(template: Template) -> CapabilityTool:
    """Build the agent tool that starts one template run."""
    params_model = _params_model(template)

    async def launch(params: params_model) -> str:  # type: ignore[valid-type]
        # The body receives decoded JSON (a plain dict), not the annotated model;
        # `start_template_run` validates it, so every caller is covered.
        return await start_template_run(template, params)

    launch.__name__ = tool_name(template)
    launch.__qualname__ = launch.__name__
    launch.__doc__ = _docstring(template)
    return launch


def template_tools(*, declared: bool = False) -> list[CapabilityTool]:
    """One generated launcher per bound template, each refusing what it cannot run.

    A launcher is bound unless `withheld_reason` withholds it, and still checks `unrunnable_reason`
    before queueing.

    Args:
        declared: Build every enabled template's launcher, withheld or not, for readers asking about
        the tree rather than this deployment (the prose contract).
    """
    return [build_template_tool(template) for template in (enabled() if declared else bound())]


def template_tool_names(*, declared: bool = False) -> list[str]:
    """The advertised name of every bound template's tool, or every enabled one's if `declared`."""
    return sorted(tool_name(template) for template in (enabled() if declared else bound()))
