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
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import frontmatter
from deepagents.middleware.skills import (
    MAX_SKILL_DESCRIPTION_LENGTH,
    MAX_SKILL_NAME_LENGTH,
)
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# Where a skill's frontmatter lives inside its directory — the Agent Skills spec's filename.
SKILL_FILENAME = "SKILL.md"

#: The Agent Skills spec's bounds on the two required fields, taken from the loader that applies
#: them rather than transcribed.
#:
#: **Imported because upstream *truncates* rather than refuses.** A description over the limit is
#: cut to it with a warning, and a name over it likewise — so a skill can be stored under one name
#: and listed under another, and a description can be stored whole and read half. Both are silent
#: from anywhere but a log. Declaring them on `SkillManifest` turns the same limits into a
#: validation error: a CI failure for the reviewed tree (`make skill-validate`) and a 422 for the
#: chemist's own tier, in both cases naming the field rather than quietly shortening it.
#:
#: `tests/test_upstream_surface.py` is what holds the assumption that upstream truncates, because
#: if a bump made it *refuse* instead, these bounds would be the only thing standing between a
#: person and a skill that vanishes from the listing with no error anywhere.
MAX_SKILL_NAME_CHARS = MAX_SKILL_NAME_LENGTH
MAX_SKILL_DESCRIPTION_CHARS = MAX_SKILL_DESCRIPTION_LENGTH


class SkillManifest(BaseModel):
    """One skill's `SKILL.md` frontmatter, validated.

    `extra="forbid"` is what makes a misspelled key fail instead of vanishing — the same fail-fast
    stance the config models take. Every current skill declares exactly `name` + `description`, so
    forbidding extras costs nothing today and catches the next typo.
    """

    # `str_strip_whitespace` makes a whitespace-only value collapse to empty and fail `min_length`,
    # preserving what the hand-rolled check did before this model (`value.strip()`).
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=MAX_SKILL_NAME_CHARS)
    description: str = Field(min_length=1, max_length=MAX_SKILL_DESCRIPTION_CHARS)
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
    # The subset of `tools` without which this skill's judgment is **misleading rather than merely
    # narrower** — the tools it is centrally about. Empty for almost every skill, and that is the
    # point: `ToolScopedSkills` hides a skill only when *every* declared tool is absent, and that
    # rule was measured (hiding on *any* absent tool takes 20 of 28 skills off the shipped
    # `property-lookup` profile, because a skill routinely names one tool outside a narrow agent's
    # surface while staying useful for the rest).
    #
    # **This is the case that rule does not catch.** Three process-development skills lose their
    # *central* tools when their bundle ships off while keeping peripheral ones, so they survive the
    # "all absent" test and are listed in every deployment's prefix as judgment about a path the
    # turn cannot take — measured at 242 tokens on every model call, for a capability most
    # deployments do not enable. `solvent-swap-and-distillation` is the clearest: six of its twelve
    # tools are the `props` chain plus `shortcut_distillation`, so a default deployment can execute
    # one step of its five-step answer.
    #
    # Opt-in, so nothing changes for a skill that declares none, and `make skill-validate` refuses a
    # `requires` entry that is not also in `tools` — a required tool the skill does not declare
    # would be a dependency no validator checks.
    requires: list[str] = Field(default_factory=list)
    # Free-form grouping (e.g. "retrieval", "optimization") — human-facing only; nothing dispatches
    # on a tag today, so it stays an unconstrained list rather than an invented enum.
    tags: list[str] = Field(default_factory=list)


def required_tools(skills_dirs: Iterable[str]) -> dict[str, frozenset[str]]:
    """Each discovered skill's *required* tools, by skill name — the `requires:` half.

    Off the same cached walk as `declared_tools` below, so "which skills exist" has one answer:
    two separate globs could disagree about a skill that appeared or vanished between them, and
    `ToolScopedSkills` reads both maps for the same skill in the same call.

    Almost always empty. `SkillManifest.requires` says what the key is for and why it is separate
    from `tools:` — the short version is that "every declared tool is absent" is the right rule for
    a narrow *profile* and misses a skill whose *central* tools went with an opt-in bundle.
    """
    return _declared_tools(tuple(skills_dirs))[1]


def declared_tools(skills_dirs: Iterable[str]) -> dict[str, frozenset[str]]:
    """Each discovered skill's declared tool dependencies, by skill name.

    The run-time reader of the `tools:` declaration, for `chemclaw.agent.skill_access.
    ToolScopedSkills`. Read once per process, not per turn: the skills tree does not change
    while the process runs, and re-reading every `SKILL.md` on every turn would trade the whole
    point of progressive disclosure for a filter.

    **That sentence used to be a claim rather than a property, and the claim had gone false.** It
    was true when an agent was built once and lived in the process. A graph is now compiled *per
    turn* (`agent/langgraph_agent.py`, M7 — LangGraph binds tools at construction), and this
    function sat on that path uncached: measured at **2.6 ms of synchronous `open()` + YAML parse
    across 28 skills, on the event loop, per turn** — and doubled again by `agent/subagents.py`,
    which compiles a second graph through the same builder for the helper behind `task`. That is
    the hazard `tests/test_event_loop_offload.py` exists for, in the layer above the one it watches.
    `@cache` on `_declared_tools` restores the property the paragraph describes, so the docstring is
    now enforced rather than asserted.

    **Tolerant where `chemclaw.cli.validate_skills` is strict, and deliberately so.** An unreadable
    or invalid `SKILL.md` is reported there, loudly, before deploy; here it does not raise. The
    failure directions are not symmetric: a validator that shrugs ships a broken skill, while a
    *filter* that raises takes down every live conversation over a frontmatter typo. Both halves see
    the same files, so the strict one is what actually holds the line.

    **What "tolerant" does *not* mean is "absent from the map", and this paragraph said it did.** It
    read "here it is simply absent from the map, which the source reads as 'declares nothing' and
    therefore leaves visible" — which is the exact behaviour `_declared_pair` a hundred lines below
    was changed to stop, because leaving a skill visible is a *widening* in a filter whose whole
    contract is that a declaration can only cost visibility. An unreadable manifest is now mapped to
    `UNREADABLE_DECLARATION`, keyed by its directory, and scoped to nothing.

    **So a skill missing from the returned map and a skill mapped to an empty set mean opposite
    things**, and this paragraph claimed they were the same and therefore not distinguished. An
    empty set is "declares nothing", which `ToolScopedSkills._permits` leaves visible to every
    profile; absence means the same. `UNREADABLE_DECLARATION`'s own comment says an empty set
    "would be the fail-*open* answer", which is why nothing here ever returns one for a file it
    could not read.

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
    return _declared_tools(tuple(skills_dirs))[0]


@cache
def _declared_tools(
    skills_dirs: tuple[str, ...],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """`declared_tools` over a hashable key — the cached half; see it for the why.

    Split rather than decorating the public function because callers pass a list (and a `dict_keys`,
    and a generator), none of which `@cache` can key on. The returned mapping is shared by every
    caller and must be treated as read-only; every caller today only reads it.

    `_declared_tools.cache_clear()` is the seam a test uses after writing a skills tree, the same
    one `connectors.registry.discovered` offers for the same reason.
    """
    declared: dict[str, frozenset[str]] = {}
    required: dict[str, frozenset[str]] = {}
    for directory in skills_dirs:
        for path in sorted(Path(directory).glob(f"*/{SKILL_FILENAME}")):
            name, tools, requires = _declared_pair(path)
            if name in declared:
                continue
            declared[name] = tools
            required[name] = requires
    return declared, required


#: The `tools:` declaration given to a skill whose frontmatter could not be read.
#:
#: A name no tool can have, so `ToolScopedSkills._permits`' `required & available` is always empty
#: and the skill is scoped to nothing. A sentinel rather than an empty set, because an empty set is
#: exactly what "declares nothing" means to that predicate and would be the fail-*open* answer.
UNREADABLE_DECLARATION: frozenset[str] = frozenset({"\x00unreadable-skill-manifest"})


def declared_triple(metadata: Mapping[str, Any]) -> tuple[str, frozenset[str], frozenset[str]]:
    """`(name, declared tools, required tools)` off already-parsed frontmatter, or raise.

    **The reading half, split out because a stored skill has no path.** The two stored tiers hold a
    body under a store key rather than a file under a directory, and `agent/stored_skill_tools.py`
    needs exactly this answer about exactly these three keys. Two readers would be two opinions
    about what a `tools:` declaration is, in a filter whose contract is that a declaration can only
    ever cost a skill its visibility — so the parse is one function and only the *fallback* differs
    (a directory name for a file, a store key's name for a body).

    It raises rather than returning a sentinel, because the caller is what knows the fallback name
    and what to log about the thing that could not be read. See `_declared_pair` for why every
    failure has to fail *closed*.

    Args:
        metadata: The frontmatter mapping, from `frontmatter.load` or `frontmatter.loads`.

    Returns:
        The stripped name and the two declarations, each as a frozenset of tool names.

    Raises:
        TypeError: When `name` is not a string, or `tools`/`requires` is not a list.
        ValueError: When `name` is empty or whitespace.
        KeyError: When there is no `name` at all.
    """
    name = metadata["name"]
    tools = metadata.get("tools") or []
    if not isinstance(name, str) or not isinstance(tools, list):
        raise TypeError(f"name must be a string and tools a list, got {type(name)}/{type(tools)}")
    # Stripped and required non-empty, which is what `SkillManifest`'s `str_strip_whitespace` plus
    # `min_length=1` did before this read the keys directly.
    #
    # **An empty name does not "stay out of the map".** The raise reaches the caller, which keys the
    # entry by its own fallback — driven, a filed skill with `name: '   '` is keyed `'empty-name'`.
    # That is the right answer for the reason `_declared_pair`'s `except` arm gives: a manifest
    # whose own name cannot be read is one whose `tools:` declaration cannot be trusted either. What
    # must not happen is the entry going missing, which reads as "declares nothing" and leaves the
    # skill visible.
    if not name.strip():
        raise ValueError("a skill's `name` is empty")
    requires = metadata.get("requires") or []
    if not isinstance(requires, list):
        raise TypeError(f"requires must be a list, got {type(requires)}")
    return (
        name.strip(),
        frozenset(str(tool) for tool in tools),
        frozenset(str(tool) for tool in requires),
    )


def _declared_pair(path: Path) -> tuple[str, frozenset[str], frozenset[str]]:
    """One skill's `(name, declared tools, required tools)`, scoped to nothing when unreadable.

    **The third element comes off this same read rather than a second walk**, so "which skills
    exist" has one answer: a `requires:` map built by re-globbing could disagree with the `tools:`
    map about a skill that appeared or vanished between them, and the two are read together on
    every path that reads either.

    **Total, and it used to be able to return `None`.** The summary line here said "or None (logged)
    if the file cannot be read at all" and the caller guarded on it, long after the `except` arm was
    changed to return `(directory name, UNREADABLE_DECLARATION)` — so the `| None`, the guard and
    that clause were all dead, and `mypy --strict` cannot see it. Driven over a missing file, an
    empty `name`, a whitespace `name`, undecodable bytes and a scalar `tools:`: all five come back
    as a pair. The annotation is narrowed rather than the behaviour widened, because a `None` here
    is exactly the fail-open answer the arm below exists to refuse.

    Separate from `declared_tools` so the "why swallow it" reasoning sits next to the `except`:
    both failure modes are reported properly by `make skill-validate`, and neither is worth raising
    on the path that serves a live turn.

    **It reads the two keys the scoping needs rather than validating the whole manifest, and that
    is a fix rather than a shortcut.** This used to run `SkillManifest.model_validate`, so *any*
    frontmatter defect erased the `tools:` declaration — and `ToolScopedSkills` reads a missing
    entry as "declares nothing", which it leaves **visible to every caller**. A read error was
    therefore a *widening*, in a filter whose whole contract is that a declaration can only ever
    cost a skill its visibility. Measured after `MAX_SKILL_DESCRIPTION_CHARS` arrived: a site skill
    one character over the limit went from "scoped, description truncated by the loader" to
    "unscoped", with nothing but a WARNING to say so. The length of a description is not evidence
    about which tools a skill teaches, and neither is a misspelled `tags:` key.

    The catch is broad on purpose. `frontmatter.load` surfaces whatever the YAML parser raises,
    which is not one type; enumerating them would leave the next parser error to break every
    conversation in the deployment.
    """
    try:
        return declared_triple(frontmatter.load(path).metadata)
    except Exception as exc:
        # WARNING rather than the helper's ERROR default: `make skill-validate` is a CI gate over
        # exactly this, so an unreadable manifest is caught before it ships and a live occurrence
        # is an authoring problem in the corpus, not an outage. The counter is still what makes it
        # visible at all — a skill silently absent from every turn produces no other signal.
        degraded(
            logger,
            "skill_manifest",
            "skill %s has unreadable frontmatter, scoping it to nothing: %s",
            path,
            exc,
            level=logging.WARNING,
            exc_info=False,
        )
        # **Fail closed, and returning `None` here failed open.** `ToolScopedSkills._permits` reads
        # a
        # *missing* entry as "declares nothing", which it leaves visible to every caller — so an
        # unreadable `tools:` key was a **widening**, in a filter whose whole contract is that a
        # declaration can only ever cost a skill its visibility. That is the defect this function's
        # docstring above describes and fixes for one case (an over-long `description`); measured, a
        # scalar `tools:`, a mapping `tools:` and a YAML fault all still reached a profile with zero
        # callable tools.
        #
        # The key is the *directory* name rather than the frontmatter's, because the frontmatter is
        # the thing that could not be read. `make skill-validate` requires the two to match
        # (`cli/validate_skills.py`), so in any tree CI has walked this is the same string the
        # readable path would have produced — and in a tree it has not walked, a skill scoped to
        # nothing is the safe answer rather than a guess.
        return path.parent.name, UNREADABLE_DECLARATION, UNREADABLE_DECLARATION
