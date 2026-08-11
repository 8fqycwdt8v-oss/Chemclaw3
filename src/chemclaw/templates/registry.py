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
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, create_model
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from chemclaw.agent.authz import require_actor
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import get_current_roles
from chemclaw.core.ids import stable_hash
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect
from chemclaw.core.tool_registry import CapabilityTool
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
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TemplateError(f"{path}: unreadable or malformed YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TemplateError(f"{path}: must contain a YAML mapping, got {type(raw).__name__}")
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
def discovered() -> dict[str, Template]:
    """Every discovered template by name, validated. Cached for the process, like connectors."""
    found: dict[str, Template] = {}
    for directory in settings.templates_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.yaml")):
            template = _load(path)
            if template.name in found:
                raise TemplateError(f"{path}: template {template.name!r} is already defined")
            found[template.name] = template
    return found


def enabled() -> list[Template]:
    """The templates this deployment turns on; empty enable-list means every discovered one."""
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
            )
        except WorkflowAlreadyStartedError:
            return workflow_id
        from chemclaw.core.turn_signals import record_job_started

        record_job_started(handle.id, f"template:{template.name}")
        return handle.id

    launch.__name__ = tool_name(template)
    launch.__qualname__ = launch.__name__
    launch.__doc__ = _docstring(template)
    return launch


def template_tools() -> list[CapabilityTool]:
    """One generated launcher per enabled template."""
    return [build_template_tool(template) for template in enabled()]


def template_tool_names() -> list[str]:
    """The advertised name of every enabled template's tool, for the validators to check."""
    return sorted(tool_name(template) for template in enabled())
