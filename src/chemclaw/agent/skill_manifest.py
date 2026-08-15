"""The validated `SKILL.md` manifest — a skill's frontmatter as a typed contract.

Why this exists: a skill is discovered by its frontmatter, and until now that frontmatter was read
as a bare dict and spot-checked for two string fields by the validate script. That made two
classes of drift invisible. First, a typo'd or invented key (`descriptions:`, `tool:`) was
silently ignored — the skill loaded with a missing description rather than failing. Second, and the
reason this is worth a model rather than a longer checklist: a skill is *judgment about
capabilities* ("call `suggest_next_experiment` like this"), but it had no way to **declare** which
capabilities it depends on, so a skill could outlive the tool it teaches and nothing would notice.

`SkillManifest` makes the frontmatter a pydantic contract: `name`/`description` stay required (the
model reads them to decide when to load a skill), and the optional `tools` declaration is checked
against the live tool surface by `chemclaw.cli.validate_skills` — the in-process registry
(`chemclaw.core.tool_registry`) plus everything the enabled connectors advertise
(`chemclaw.connectors.registry.connector_tool_names`). That check is the point: it turns "this
skill teaches
a tool that no longer exists" from a silent stale-prose problem into a CI failure. `tags` is
free-form
grouping for humans (and the eventual profile authoring in Stage 3).

Deliberately *not* here: enforcement at load time. A manifest declaring a tool does **not** grant
access to it — tools are advertised by the agent's own registry/profile and gated by
`enforce_tool_authz`. The declaration is documentation the gate validates, never an authorization
input, so this module cannot widen what a skill's reader may do (audit doc 10 §7).

It is, since D-2026-08-05, read at run time too — by `chemclaw.agent.skill_access.
ToolScopedSkills`, which *hides* a skill whose whole declared capability is absent from the
agent's surface. That is the same one-way direction: the declaration can only cost a skill its
visibility, never buy it a tool. `declared_tools` below is the reader, and it exists here rather
than in the source because a skill loader keeps only the Agent Skills spec's own fields
and drops `tools:` on the floor — the declaration is invisible to a `Skill` object, so anything
that wants it must read the file.
"""

import logging
from collections.abc import Iterable
from pathlib import Path

import frontmatter
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# Where a skill's frontmatter lives inside its directory — the Agent Skills spec's filename.
SKILL_FILENAME = "SKILL.md"


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
    # tools, the generated connector job launchers, and the tools an enabled connector's endpoint
    # serves, all in one list because all three are things the model calls by name. Optional: a
    # skill that is pure process guidance depends on nothing. Validated against the live surface
    # (`core.tool_registry` + `connectors.registry.connector_tool_names`), so a renamed or deleted
    # capability surfaces as a CI failure rather than as stale prose in the skill body.
    #
    # Deliberately no separate "which connector" declaration: the thing that breaks a skill is the
    # tool disappearing, not the bundle being renamed, and a coarser second field would be a second
    # way to say almost the same thing (it would also pass while the tool it teaches was gone).
    tools: list[str] = Field(default_factory=list)
    # Free-form grouping (e.g. "retrieval", "optimization") — human-facing only; nothing dispatches
    # on a tag today, so it stays an unconstrained list rather than an invented enum.
    tags: list[str] = Field(default_factory=list)


def declared_tools(skills_dirs: Iterable[str]) -> dict[str, frozenset[str]]:
    """Each discovered skill's declared tool dependencies, by skill name.

    The run-time reader of the `tools:` declaration, for `chemclaw.agent.skill_access.
    ToolScopedSkills`. Built once when the agent is built, not per turn: the skills tree does
    not change while the process runs, and re-reading every `SKILL.md` on every turn would trade the
    whole point of progressive disclosure for a filter.

    **Tolerant where `chemclaw.cli.validate_skills` is strict, and deliberately so.** An unreadable
    or invalid `SKILL.md` is reported there, loudly, before deploy; here it is simply absent from
    the map, which the source reads as "declares nothing" and therefore leaves visible. The failure
    directions are not symmetric: a validator that shrugs ships a broken skill, while a *filter*
    that raises takes down every live conversation over a frontmatter typo. Both halves see the same
    files, so the strict one is what actually holds the line.

    A skill missing from the returned map and a skill mapped to an empty set mean the same thing to
    every caller, so the two are not distinguished.

    Args:
        skills_dirs: The directories to walk — the configured tree plus each enabled connector
            bundle's own `skills/` (the same list `build_langgraph_agent` routes through
            `langgraph_agent.skills_backend`).

    Returns:
        `{skill name: declared tool names}`, keyed by the frontmatter `name` because that is what a
        `Skill` object carries and therefore what the filter can match on. A duplicate name across
        two directories keeps the first, matching the precedence `langgraph_agent._labelled` gives
        the routed trees.
    """
    declared: dict[str, frozenset[str]] = {}
    for directory in skills_dirs:
        for path in sorted(Path(directory).glob(f"*/{SKILL_FILENAME}")):
            manifest = _read_manifest(path)
            if manifest is not None:
                declared.setdefault(manifest.name, frozenset(manifest.tools))
    return declared


def _read_manifest(path: Path) -> SkillManifest | None:
    """One skill's validated frontmatter, or None (logged) if it cannot be read.

    Separate from `declared_tools` so the "why swallow it" reasoning sits next to the `except`:
    both failure modes are reported properly by `make skill-validate`, and neither is worth raising
    on the path that serves a live turn.

    The catch is broad on purpose. `frontmatter.load` surfaces whatever the YAML parser raises,
    which is not one type, and `model_validate` adds `ValidationError`; enumerating them would
    leave the next parser error to break every conversation in the deployment.
    """
    try:
        return SkillManifest.model_validate(frontmatter.load(path).metadata)
    except Exception as exc:
        # WARNING rather than the helper's ERROR default: `make skill-validate` is a CI gate over
        # exactly this, so an unreadable manifest is caught before it ships and a live occurrence
        # is an authoring problem in the corpus, not an outage. The counter is still what makes it
        # visible at all — a skill silently absent from every turn produces no other signal.
        degraded(
            logger,
            "skill_manifest",
            "skill %s has unreadable frontmatter, treating it as undeclared: %s",
            path,
            exc,
            level=logging.WARNING,
            exc_info=False,
        )
        return None
