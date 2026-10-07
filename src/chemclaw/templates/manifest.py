"""The template contract: a fixed sequence of steps, validated before anything can run it.

Everything here makes a bad template impossible to start, rather than discovered halfway through a
durable run. References are strict for the same reason: a `${steps.missing.result}` that became
"None" would feed a null into a calculation.

Deliberately not a template language: no conditionals, loops or expressions, only `${inputs.x}`,
`${steps.id.result}` and a dotted field path into that result. The field path is addressing, not
computation; it lets a `job` step's computed value reach the next step without an `agent` step
re-typing it (`D-2026-08-21-a-geometry-is-an-address-not-a-payload`). Branching belongs in an agent
or a connector workflow.
"""

import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.manifest_io import MAX_MANIFEST_TEXT_CHARS

# A reference to an input, an earlier step's result, or a field inside that result. Anchored and
# closed; the field path is a dotted attribute walk only.
_REFERENCE = re.compile(
    r"\$\{(inputs\.[a-z][a-z0-9_]*|steps\.[a-z][a-z0-9_-]*\.result(?:\.[a-z][a-z0-9_]*)*)\}"
)
# Anything shaped like a reference, well-formed or not. `_REFERENCE` only finds valid references, so
# a typo would pass as a literal string; `_references_are_well_formed` refuses the difference
# between the two patterns.
_ANY_REFERENCE = re.compile(r"\$\{[^}]*\}")
# The step a reference names, without any field path: the step is checkable at load time, the field
# only at run time.
_STEP_RESULT = re.compile(r"^(steps\.[a-z][a-z0-9_-]*\.result)")

# The declared type of a template input, the same closed set connector job params use.
InputType = Literal["string", "integer", "number", "boolean", "string[]", "number[]", "object"]


class TemplateInput(BaseModel):
    """One argument the template takes, as the model will see it on the generated tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    type: InputType
    description: str = Field(min_length=1, max_length=MAX_MANIFEST_TEXT_CHARS)
    required: bool = True


class _Step(BaseModel):
    """What every step has: an id later steps refer to, and a human-facing purpose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Hyphens allowed: a step id is only a key in this file and in `${steps.<id>.result}`, never a
    # Python or tool identifier.
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    # Why the step is here; not model-facing, for human readers and the run's trace.
    purpose: str = ""


class ToolStep(_Step):
    """Call one tool on the agent's surface — in-process or a connector's — with resolved arguments.

    The result is whatever the tool returned. A whole-string reference in `arguments` preserves the
    referenced value's type.
    """

    kind: Literal["tool"] = "tool"
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class JobStep(_Step):
    """Run a connector's durable job and *await* its result.

    Unlike a `tool` step naming a job launcher, which returns a job id, this runs the job as a child
    workflow so a template can sequence long work as one durable unit.
    """

    kind: Literal["job"] = "job"
    job: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentStep(_Step):
    """Run one agent turn with a rendered prompt; the result is its answer text.

    The sequence is fixed, the reasoning inside a step is not. `profile` picks which configured
    agent runs it.

    The step is read-only unless `write_tools` names writes. A human-authored template is the
    pre-approved plan and bypasses the plan gate, so `durable.template_activities.step_profile`
    removes every undeclared side-effecting tool before the graph is built. Naming a read tool is
    rejected by `make template-validate`, so the list cannot become an allow-list for the whole
    surface.
    """

    kind: Literal["agent"] = "agent"
    prompt: str = Field(min_length=1)
    profile: str | None = None
    write_tools: list[str] = Field(default_factory=list)


Step = Annotated[ToolStep | JobStep | AgentStep, Field(discriminator="kind")]


def _strings(value: Any) -> Iterator[str]:
    """Every string inside an argument tree, recursing through lists and dicts.

    One walker so references and malformed references are looked for in the same places.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def references(value: Any) -> set[str]:
    """Every well-formed `${…}` reference inside a value."""
    return {ref for text in _strings(value) for ref in _REFERENCE.findall(text)}


def malformed_references(value: Any) -> set[str]:
    """Every `${…}` span inside a value that is *not* a legal reference.

    The complement of `references`: what validation would otherwise never see.
    """
    return {
        span
        for text in _strings(value)
        for span in _ANY_REFERENCE.findall(text)
        if not _REFERENCE.fullmatch(span)
    }


class Template(BaseModel):
    """One `data/templates/<name>.yaml`: the inputs, the ordered steps, and what the model is told.

    The name comes from the filename, as a profile's does, so a file and its identity cannot
    disagree.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    # The first line of the generated `run_<template>` tool's docstring, with `description` the
    # rest. Bounded by the same constant as `JobSpec.summary`, since it is prompt text.
    summary: str = Field(min_length=1, max_length=MAX_MANIFEST_TEXT_CHARS)
    description: str = Field(default="", max_length=MAX_MANIFEST_TEXT_CHARS)
    inputs: list[TemplateInput] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_names(self) -> Self:
        """Reject a duplicate input or step id — a reference to either would be ambiguous."""
        for kind, names in (
            ("input", [item.name for item in self.inputs]),
            ("step", [step.id for step in self.steps]),
        ):
            duplicated = sorted({name for name in names if names.count(name) > 1})
            if duplicated:
                raise ValueError(f"template {self.name!r} has duplicate {kind}(s) {duplicated}")
        return self

    @model_validator(mode="after")
    def _references_are_well_formed(self) -> Self:
        """Reject a `${…}` span that is not a reference at all — a typo, not a literal.

        A separate rule because the resolution check only sees references it found, and a misspelled
        one is found by nothing; it would otherwise reach the tool as literal text.
        """
        for step in self.steps:
            malformed = sorted(malformed_references(_step_value(step)))
            if malformed:
                raise ValueError(
                    f"template {self.name!r} step {step.id!r} has malformed reference(s) "
                    f"{malformed}; the only legal forms are ${{inputs.<name>}} and "
                    "${steps.<id>.result} with an optional dotted field path"
                )
        return self

    @model_validator(mode="after")
    def _references_resolve_and_point_backwards(self) -> Self:
        """Reject a reference to an unknown input, an unknown step, or a step that has not run yet.

        `steps` is ordered, so a forward reference can never work and fails here.
        """
        known_inputs = {f"inputs.{item.name}" for item in self.inputs}
        available: set[str] = set()
        for step in self.steps:
            for reference in sorted(step_references(step)):
                if reference.startswith("inputs.") and reference not in known_inputs:
                    raise ValueError(
                        f"template {self.name!r} step {step.id!r} references unknown "
                        f"{reference!r}; declared inputs: {sorted(known_inputs)}"
                    )
                named = _STEP_RESULT.match(reference)
                if named is not None and named.group(1) not in available:
                    raise ValueError(
                        f"template {self.name!r} step {step.id!r} references {reference!r}, "
                        "which is not the result of an earlier step"
                    )
            available.add(f"steps.{step.id}.result")
        return self


def _step_value(step: Step) -> Any:
    """The part of a step that may carry references — its prompt, or its arguments."""
    return step.prompt if isinstance(step, AgentStep) else step.arguments


def step_references(step: Step) -> set[str]:
    """Every reference one step makes, whichever kind it is.

    Public because it is the dependency graph `templates/schedule.py` reads; the validators make the
    declared order a topological order of it.
    """
    return references(_step_value(step))
