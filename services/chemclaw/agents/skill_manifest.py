"""The validated `SKILL.md` manifest — a skill's frontmatter as a typed contract.

Why this exists: a skill is discovered by its frontmatter, and until now that frontmatter was
read as a bare dict and spot-checked for two string fields by the validate script. That made two
classes of drift invisible. First, a typo'd or invented key (`descriptions:`, `tool:`) was
silently ignored — the skill loaded with a missing description rather than failing. Second, and
the reason this is worth a model rather than a longer checklist: a skill is *judgment about
capabilities* ("call `suggest_next_experiment` like this"), but it had no way to **declare** which
capabilities it depends on, so a skill could outlive the tool it teaches and nothing would notice.

`SkillManifest` makes the frontmatter a pydantic contract: `name`/`description` stay required (the
model reads them to decide when to load a skill), and the optional `tools` declaration is checked
against the live tool surface by `scripts.validate_skills` — the in-process registry
(`agents.tool_registry`) plus everything the enabled connectors advertise
(`connectors.registry.connector_tool_names`). That check is the point: it turns "this skill teaches
a
tool that no longer exists" from a silent stale-prose problem into a CI failure. `tags` is free-form
grouping for humans (and the eventual profile authoring in Stage 3).

Deliberately *not* here: enforcement at load time. A manifest declaring a tool does **not** grant
access to it — tools are advertised by the agent's own registry/profile and gated by
`enforce_tool_authz`. The declaration is documentation the gate validates, never an authorization
input, so this module cannot widen what a skill's reader may do (audit doc 10 §7).
"""

from pydantic import BaseModel, ConfigDict, Field


class SkillManifest(BaseModel):
    """One skill's `SKILL.md` frontmatter, validated.

    `extra="forbid"` is what makes a misspelled key fail instead of vanishing — the same fail-fast
    stance the config models take. Every current skill declares exactly `name` + `description`, so
    forbidding extras costs nothing today and catches the next typo.
    """

    # `str_strip_whitespace` makes a whitespace-only value collapse to empty and fail `min_length`,
    # preserving what the hand-rolled check did before this model (`value.strip()`).
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    # The capabilities this skill's judgment is written about, by *tool* name — the in-process
    # tools,
    # the generated connector job launchers, and the tools an enabled connector's endpoint serves,
    # all in one list because all three are things the model calls by name. Optional: a skill that
    # is
    # pure process guidance depends on nothing. Validated against the live surface
    # (`agents.tool_registry` + `connectors.registry.connector_tool_names`), so a renamed or deleted
    # capability surfaces as a CI failure rather than as stale prose in the skill body.
    #
    # Deliberately no separate "which connector" declaration: the thing that breaks a skill is the
    # tool disappearing, not the bundle being renamed, and a coarser second field would be a second
    # way to say almost the same thing (it would also pass while the tool it teaches was gone).
    tools: list[str] = Field(default_factory=list)
    # Free-form grouping (e.g. "retrieval", "optimization") — human-facing only; nothing dispatches
    # on a tag today, so it stays an unconstrained list rather than an invented enum.
    tags: list[str] = Field(default_factory=list)
