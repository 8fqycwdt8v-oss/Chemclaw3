"""The template contract: a fixed sequence of steps, validated before anything can run it.

A template is the deterministic counterpart to a profile. A profile configures an agent and lets the
model choose the order of work; a template fixes the order and lets the model fill the gaps. That is
the whole distinction, and it is why the two are separate things rather than one thing with a flag —
they answer different questions and fail in different ways (`src/chemclaw/templates/README.md`).

Everything here is about making a bad template impossible to *start*, because the alternative is
discovering it halfway through a durable run that has already spent money. The reference resolver is
strict for the same reason: a `${steps.missing.result}` that quietly became the string "None" would
put a null into a calculation and produce a confident wrong answer, which is the worst failure this
system can have.

Deliberately *not* a template language. There are no conditionals, no loops and no expressions —
only `${inputs.x}` and `${steps.id.result}`. Adding them is how a config format becomes a
programming language with no debugger, and the moment a procedure needs branching it wants an agent
(a profile) or real code (a connector workflow), neither of which is more YAML.
"""

import re
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A reference to an input or to an earlier step's result. Anchored and closed: exactly two forms,
# so a typo like `${step.x.result}` fails validation rather than being passed through as a literal.
_REFERENCE = re.compile(r"\$\{(inputs\.[a-z][a-z0-9_]*|steps\.[a-z][a-z0-9_-]*\.result)\}")

# The declared type of a template input, reusing the closed set a connector job's params use — the
# same reasoning applies (a schema the model can always fill correctly beats an open type language),
# and one vocabulary across both is one less thing for an author to look up.
InputType = Literal["string", "integer", "number", "boolean", "string[]", "number[]", "object"]


class TemplateInput(BaseModel):
    """One argument the template takes, as the model will see it on the generated tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    type: InputType
    description: str = Field(min_length=1)
    required: bool = True


class _Step(BaseModel):
    """What every step has: an id later steps refer to, and a human-facing purpose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Hyphens allowed (unlike a tool name) because a step id is never a Python or tool identifier —
    # it is only a key inside this file and in `${steps.<id>.result}`.
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    # Why the step is here. Not model-facing: it is for the human reading the template and for the
    # run's own trace, which is what an auditor reads when asking what a procedure did.
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
    """

    kind: Literal["agent"] = "agent"
    prompt: str = Field(min_length=1)
    profile: str | None = None


Step = Annotated[ToolStep | JobStep | AgentStep, Field(discriminator="kind")]


def references(value: Any) -> set[str]:
    """Every `${…}` reference inside a value, recursing through lists and dicts.

    Recursive because arguments are arbitrary JSON: a reference is as likely to be the third element
    of a list as a top-level value, and a resolver that only looked at the top level would silently
    pass `${inputs.smiles}` through as a literal string.
    """
    if isinstance(value, str):
        return set(_REFERENCE.findall(value))
    if isinstance(value, dict):
        return {ref for item in value.values() for ref in references(item)}
    if isinstance(value, list):
        return {ref for item in value for ref in references(item)}
    return set()


class Template(BaseModel):
    """One `data/templates/<name>.yaml`: the inputs, the ordered steps, and what the model is told.

    The name comes from the filename, as a profile's does, so a file and its identity cannot
    disagree.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    # The first line of the generated tool's docstring — what the model reads when deciding to run
    # this — and `description` is the rest, exactly as a connector job declares them.
    summary: str = Field(min_length=1)
    description: str = ""
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
    def _references_resolve_and_point_backwards(self) -> Self:
        """Reject a reference to an unknown input, an unknown step, or a step that has not run yet.

        The forward-reference check is the one worth spelling out: `steps` is an *ordered* list
        and a step can only use what already happened, so naming a later step is not a subtle
        timing bug to debug at run time — it is a template that can never work, and it fails here.
        """
        known_inputs = {f"inputs.{item.name}" for item in self.inputs}
        available: set[str] = set()
        for step in self.steps:
            for reference in sorted(_step_references(step)):
                if reference.startswith("inputs.") and reference not in known_inputs:
                    raise ValueError(
                        f"template {self.name!r} step {step.id!r} references unknown "
                        f"{reference!r}; declared inputs: {sorted(known_inputs)}"
                    )
                if reference.startswith("steps.") and reference not in available:
                    raise ValueError(
                        f"template {self.name!r} step {step.id!r} references {reference!r}, "
                        "which is not the result of an earlier step"
                    )
            available.add(f"steps.{step.id}.result")
        return self


def _step_references(step: Step) -> set[str]:
    """Every reference one step makes, whichever kind it is."""
    if isinstance(step, AgentStep):
        return references(step.prompt)
    return references(step.arguments)
