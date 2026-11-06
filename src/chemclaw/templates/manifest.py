"""The template contract: a fixed sequence of steps, validated before anything can run it.

Everything here makes a bad template impossible to start, rather than discovered halfway through a
durable run. References are strict for the same reason: a `${steps.missing.result}` that became
"None" would feed a null into a calculation.

Deliberately not a template language: no conditionals or expressions, only `${inputs.x}`,
`${steps.id.result}`, a map's item variable, and a dotted field path into any of them. A `map` is a
bounded fan-out and a `select` a fixed top-k, neither a loop that stops on a result
(`D-2026-10-21-a-template-may-map-and-select`). Branching belongs in an agent or a connector
workflow.
"""

import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemclaw.core.manifest_io import MAX_MANIFEST_TEXT_CHARS

# A reference to an input, an earlier step's result, a map's item variable, or a field inside a
# result or an item. Closed; the field path is a dotted attribute walk only. Shared with
# `templates/resolve.py`, so what validates is what resolves.
REFERENCE = re.compile(
    r"\$\{(inputs\.[a-z][a-z0-9_]*"
    r"|steps\.[a-z][a-z0-9_-]*\.result(?:\.[a-z][a-z0-9_]*)*"
    r"|(?!inputs\b|steps\b)[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\}"
)
# A whole-string reference: what `over` and `from` must be.
_WHOLE_REFERENCE = re.compile(f"^{REFERENCE.pattern}$")
# Anything shaped like a reference, well-formed or not. `REFERENCE` only finds valid references, so
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

    The step's result is whatever the tool returned. `arguments` values may contain references; a
    whole-string reference preserves the referenced value's type, so passing a list to a tool that
    wants a list works without a stringly-typed detour.
    """

    kind: Literal["tool"] = "tool"
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class JobStep(_Step):
    """Run a connector's durable job and *await* its result.

    The difference from a `tool` step that names a job launcher: that returns a job id and finishes,
    which is right in a chat turn (the agent must not block) and useless inside a workflow that
    exists precisely to wait. Here the run is a child workflow, so a template can sequence long work
    — compute, then reason about the result — as one durable, resumable unit.
    """

    kind: Literal["job"] = "job"
    job: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentStep(_Step):
    """Run one agent turn with a rendered prompt; the result is its answer text.

    This is what keeps a template *agentic* rather than a shell script: the sequence is fixed, the
    reasoning inside a step is not. `profile` picks which configured agent runs it, so a step can be
    deliberately narrow — a summarizing step has no business holding the durable-job launchers.

    **The step is read-only unless it says otherwise, and `write_tools` is how it says so.** A
    template is not gated by the plan gate — a template *is* the pre-approved plan, human-authored,
    git-committed, reviewed, and uncreatable at run time — so the discretion the plan gate would
    have removed has to be removed by the file instead. The narrowing is applied by
    `chemclaw.durable.template_activities.step_profile`, which subtracts every side-effecting tool
    the step did not name from the profile's advertised surface *before the graph is built*, so an
    undeclared write is not a call that gets refused, it is a tool the step's agent never held.

    A read tool needs no entry — declaring one is rejected by `make template-validate`, because a
    list that accepts reads is an allow-list for the whole surface wearing a write-list's name.
    """

    kind: Literal["agent"] = "agent"
    prompt: str = Field(min_length=1)
    profile: str | None = None
    write_tools: list[str] = Field(default_factory=list)


#: What a `map` may run once per item: a call, never a reasoning turn or another map, so nesting
#: is refused by the type rather than by a depth counter.
MappedStep = Annotated[ToolStep | JobStep, Field(discriminator="kind")]

#: The two scopes an item name would shadow.
_SCOPES = ("inputs", "steps")
#: A dotted field path into a result or an item.
_FIELD_PATH = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"


def _whole_reference(value: str, what: str) -> str:
    """`value` if it is exactly one `${inputs.x}` or `${steps.id.result…}` reference, else raise."""
    whole = _WHOLE_REFERENCE.match(value)
    if whole is None or not whole.group(1).startswith(("inputs.", "steps.")):
        raise ValueError(
            f"{what} must be exactly one ${{inputs.<name>}} or ${{steps.<id>.result}} reference "
            f"naming a list, not {value!r}"
        )
    return value


class MapStep(_Step):
    """Run one `tool` or `job` step once per item of a list, at most `concurrency` at a time.

    `over` names the list; `as` names the item inside `step.arguments` (`${<as>}`, with an optional
    field path). The result is one `{index, item, result}` entry per item that succeeded, in item
    order; an item that fails is dropped and named in the run's degradations. `max_items` bounds the
    list at run time and sizes the run ceiling before it starts; the deployment's upper bounds are
    checked where the surface is (`agent/template_surface.step_problems`), not here, so a run in
    flight never fails to deserialize after an operator lowers them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True)

    kind: Literal["map"] = "map"
    over: str
    as_: str = Field(alias="as", pattern=r"^[a-z][a-z0-9_]*$")
    step: MappedStep
    max_items: int = Field(ge=1)
    concurrency: int = Field(default=1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _inner_step_shares_the_id(cls, data: Any) -> Any:
        """Give the inner step the map's id; it is never referenced on its own."""
        if isinstance(data, dict) and isinstance(data.get("step"), dict):
            data = {**data, "step": {"id": data.get("id"), **data["step"]}}
        return data

    @model_validator(mode="after")
    def _over_is_one_reference(self) -> Self:
        """Refuse an `over` that is not one whole reference to an input or an earlier result."""
        _whole_reference(self.over, f"map step {self.id!r}'s `over`")
        if self.as_ in _SCOPES:
            raise ValueError(f"map step {self.id!r} may not name its item {self.as_!r}")
        return self


class Condition(BaseModel):
    """One `where` comparison of a select: a field of the item against a literal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(pattern=_FIELD_PATH)
    op: Literal["lt", "le", "gt", "ge", "eq", "ne"]
    # A literal: a `${…}` here is refused by the well-formedness check, since it would be compared
    # as text.
    value: bool | float | str


class SelectStep(_Step):
    """Keep the items of a list that pass `where`, sorted by `by`, the first `top` of them.

    Deterministic and model-free: ties keep their list order, and an item whose `by` or `where`
    field is null is left out. The result is the kept items, unchanged, so a later `map` still sees
    each one's `item`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True)

    kind: Literal["select"] = "select"
    from_: str = Field(alias="from")
    by: str = Field(pattern=_FIELD_PATH)
    order: Literal["asc", "desc"] = "desc"
    # `None` keeps every item that passes `where`, sorted.
    top: int | None = Field(default=None, ge=1)
    where: list[Condition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _from_is_one_reference(self) -> Self:
        """Refuse a `from` that is not one whole reference to an input or an earlier result."""
        _whole_reference(self.from_, f"select step {self.id!r}'s `from`")
        return self


Step = Annotated[ToolStep | JobStep | AgentStep | MapStep | SelectStep, Field(discriminator="kind")]


def dispatched(step: Step) -> ToolStep | JobStep | AgentStep | SelectStep:
    """The step that names what runs: a map's inner step, otherwise the step itself.

    One answer for every reader asking which tool or job a template calls.
    """
    return step.step if isinstance(step, MapStep) else step


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
    return {ref for text in _strings(value) for ref in REFERENCE.findall(text)}


def _foreign_item(reference: str, item: str) -> bool:
    """Whether `reference` is an item variable other than `item`, the one bound here (if any)."""
    if reference.startswith(("inputs.", "steps.")):
        return False
    return reference.split(".", 1)[0] != item


def malformed_references(value: Any, item: str = "") -> set[str]:
    """Every `${…}` span inside a value that is *not* a legal reference where it stands.

    The complement of `references`: what validation would otherwise never see. An item variable is
    legal only inside the map that binds it, so `item` names the one in scope.
    """
    return {
        span
        for text in _strings(value)
        for span in _ANY_REFERENCE.findall(text)
        if not REFERENCE.fullmatch(span) or _foreign_item(span[2:-1], item)
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
            malformed = sorted(_malformed_in(step))
            if malformed:
                raise ValueError(
                    f"template {self.name!r} step {step.id!r} has malformed reference(s) "
                    f"{malformed}; the only legal forms are ${{inputs.<name>}} and "
                    "${steps.<id>.result} with an optional dotted field path, and inside a map "
                    "step the item it binds with `as`"
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
    """The part of a step that may carry run-wide references: prompt, arguments or list."""
    if isinstance(step, AgentStep):
        return step.prompt
    if isinstance(step, MapStep):
        return step.over
    if isinstance(step, SelectStep):
        return step.from_
    return step.arguments


def _malformed_in(step: Step) -> set[str]:
    """Every malformed reference in one step; a map's inner arguments may name its own item."""
    malformed = malformed_references(_step_value(step))
    if isinstance(step, MapStep):
        malformed |= malformed_references(step.step.arguments, item=step.as_)
    if isinstance(step, SelectStep):
        # A `where` value is a literal: a reference there would be compared as text.
        malformed |= {
            span
            for condition in step.where
            for span in _ANY_REFERENCE.findall(str(condition.value))
        }
    return malformed


def step_references(step: Step) -> set[str]:
    """Every run-wide reference one step makes (inputs and step results), whichever kind it is.

    Public because it is the dependency graph `templates/schedule.py` reads; the validators make the
    declared order a topological order of it. A map's item variable is local, so it is left out.
    """
    found = references(_step_value(step))
    if isinstance(step, MapStep):
        found |= references(step.step.arguments)
    return {ref for ref in found if ref.startswith(("inputs.", "steps."))}
