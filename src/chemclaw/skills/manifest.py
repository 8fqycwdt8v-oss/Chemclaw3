"""What a skill is, as a document: its validated frontmatter, its declarations and its admission.

`SkillManifest` makes the `SKILL.md` frontmatter a pydantic contract, so an invented key fails
validation instead of being ignored. `name`/`description` are required (the model reads them to
decide when to load a skill); the optional `tools` declaration is checked against the live tool
surface by `chemclaw.cli.validate_skills`. A declaration never grants access: at run time
`access.ToolScopedSkills` reads it only to *hide* a skill whose capability is absent, so it can cost
a skill visibility but never buy it a tool. `validated_skill` is the one admission rule every door
into a stored tier goes through, and `declared_tools` reads the files directly because a loaded
`Skill` object drops `tools:`.
"""

import logging
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import frontmatter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# Where a skill's frontmatter lives inside its directory — the Agent Skills spec's filename.
SKILL_FILENAME = "SKILL.md"

#: The Agent Skills spec's bounds on the two required fields, as the loader that applies them
#: states them; `tests/test_upstream_surface.py` pins the equality, because importing them here
#: would import `deepagents`. Upstream truncates over-long values with only a warning; declaring
#: the bounds on `SkillManifest` makes them a validation error instead (a CI failure for the
#: reviewed tree, a 422 for a chemist's own tier).
MAX_SKILL_NAME_CHARS = 64
MAX_SKILL_DESCRIPTION_CHARS = 1024


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
    # The tools this skill's judgment is about, by name (in-process tools, connector job launchers
    # and connector endpoint tools alike). Optional: pure process guidance depends on nothing.
    # Validated against the live surface, so a renamed or deleted tool is a CI failure. No separate
    # connector field: what breaks a skill is the tool disappearing.
    tools: list[str] = Field(default_factory=list)
    # The subset of `tools` without which this skill is misleading rather than merely narrower.
    # `ToolScopedSkills` hides a skill only when every declared tool is absent, which misses a skill
    # whose central tools left with an opt-in bundle while peripheral ones remain; `requires` names
    # that central subset. Opt-in and usually empty; `make skill-validate` refuses an entry not also
    # in `tools`.
    requires: list[str] = Field(default_factory=list)
    # Free-form grouping (e.g. "retrieval", "optimization") — human-facing only; nothing dispatches
    # on a tag today, so it stays an unconstrained list rather than an invented enum.
    tags: list[str] = Field(default_factory=list)


def required_tools(skills_dirs: Iterable[str]) -> dict[str, frozenset[str]]:
    """Each discovered skill's *required* tools, by skill name — the `requires:` half.

    Off the same cached walk as `declared_tools`, so both maps agree on which skills exist. Almost
    always empty (see `SkillManifest.requires`).
    """
    return _declared_tools(tuple(skills_dirs))[1]


def declared_tools(skills_dirs: Iterable[str]) -> dict[str, frozenset[str]]:
    """Each discovered skill's declared tool dependencies, by skill name.

    The run-time reader of `tools:` for `access.ToolScopedSkills`. Cached per process (via
    `_declared_tools`), because the graph is compiled per turn and re-parsing every `SKILL.md`
    would block the event loop each time.

    Tolerant where `chemclaw.cli.validate_skills` is strict: a filter that raised would take down
    live turns over a frontmatter typo, while the validator catches it before deploy. Tolerant does
    not mean absent: an unreadable manifest maps to `UNREADABLE_DECLARATION` (scoped to nothing),
    because an empty set means "declares nothing" and would leave the skill visible.

    Args:
        skills_dirs: The directories to walk — the configured tree plus each enabled connector
        bundle's own `skills/` (the same list `build_langgraph_agent` routes through
        `langgraph_agent.skills_backend`).

    Returns:
        `{skill name: declared tool names}`, keyed by the frontmatter `name` (what a `Skill`
        carries). A duplicate name across directories keeps the first, matching
        `langgraph_agent._labelled`'s precedence.
    """
    return _declared_tools(tuple(skills_dirs))[0]


@cache
def _declared_tools(
    skills_dirs: tuple[str, ...],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """`declared_tools` over a hashable key — the cached half.

    Split out because callers pass lists, `dict_keys` and generators, which `@cache` cannot key on.
    The returned mapping is shared and must be treated as read-only. Tests call
    `_declared_tools.cache_clear()` after writing a skills tree.
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


#: The `tools:` declaration given to a skill whose frontmatter could not be read: a name no tool can
#: have, so the skill is scoped to nothing. Not an empty set, which means "declares nothing" and
#: would fail open.
UNREADABLE_DECLARATION: frozenset[str] = frozenset({"\x00unreadable-skill-manifest"})


def declared_triple(metadata: Mapping[str, Any]) -> tuple[str, frozenset[str], frozenset[str]]:
    """`(name, declared tools, required tools)` off already-parsed frontmatter, or raise.

    Shared by filed skills and stored skills (`store.stored_skill_declarations`, which has no path),
    so a `tools:` declaration has one parser; only the fallback name differs. It reads the three
    keys directly rather than validating the whole `SkillManifest`, so an unrelated frontmatter
    defect cannot erase the declaration; it must stay in step with `SkillManifest`'s
    `str_strip_whitespace` and `min_length=1` on `name`. It raises rather than returning a sentinel
    because the caller knows the fallback name and what to log.

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
    # Stripped and required non-empty, as `SkillManifest` would. The raise sends the caller to its
    # fallback key, so the entry is scoped to nothing rather than missing (which would read as
    # "declares nothing").
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

    All three come off one read, so the `tools:` and `requires:` maps agree on which skills exist.
    Total: every failure returns a triple keyed by the directory name with `UNREADABLE_DECLARATION`,
    failing closed. The catch is broad because the YAML parser raises many types, and `make
    skill-validate` reports these failures properly before deploy.
    """
    try:
        return declared_triple(frontmatter.load(path).metadata)
    except Exception as exc:
        # WARNING rather than ERROR: `make skill-validate` catches this before it ships, so a live
        # occurrence is a corpus authoring problem. The counter is the only signal of a skill
        # silently scoped away.
        degraded(
            logger,
            "skill_manifest",
            "skill %s has unreadable frontmatter, scoping it to nothing: %s",
            path,
            exc,
            level=logging.WARNING,
            exc_info=False,
        )
        # Fail closed: `ToolScopedSkills` reads a missing entry as "declares nothing" and would
        # leave the skill visible. Keyed by the directory name because the frontmatter is what could
        # not be read; `make skill-validate` requires the two to match.
        return path.parent.name, UNREADABLE_DECLARATION, UNREADABLE_DECLARATION


def skill_fingerprint(name: str) -> str:
    """The stable identity of one skill name, for a row that must not carry the name itself.

    `turn_costs.skills_loaded` answers an equality question (was this skill already acting?), so it
    holds a digest: a personal skill's name is a chemist's own words and `turn_costs` is retained
    through erasure. Writer (`api/runner.py`) and reader (`agent/distiller.py`) share this one
    function so the two hashes cannot diverge; shared skills are digested too, so the column has
    one meaning.

    Args:
        name: The skill's name, as the frontmatter declares it.

    Returns:
        A digest stable across processes and restarts, so a guard comparing two runs' rows agrees.
    """
    return stable_hash(name)


class SkillRefused(ChemclawError):
    """A document a stored tier will not keep, and whether the reason is a conflict or a fault.

    One exception carrying `conflict`, since every caller translates it (409/422, or prose) anyway.
    """

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        """Refuse, saying whether the name is taken (`conflict`) or the document is malformed."""
        super().__init__(message)
        self.conflict = conflict


def storable_name(name: str) -> bool:
    r"""Whether this name could ever have been written, which is what a read of it may assume.

    The writer's rule, asked by the readers: the `GET`/`DELETE /skills/mine/{name}` routes accept
    any URL bytes, and a name like `a\x00b` would make Postgres raise (a 500) where the honest
    answer is 404.
    """
    return all(not character.isspace() and character.isprintable() for character in name)


def validated_skill(body: str, *, expected_name: str | None = None) -> str:
    """The name this body declares, or a refusal naming what is wrong with it.

    Every door into a stored skills tier goes through here (save, proposal acceptance,
    `propose_skill`, the distiller, org publish and revert), so the admission rules belong to the
    tiers rather than to a surface. The size bound is not per tier: it bounds what a skill is.

    Args:
        body: The whole `SKILL.md`, frontmatter included.
        expected_name: The caller's independent opinion of the name, if any (`propose_skill`'s
            `name` argument); compared rather than preferred.

    Returns:
        The validated name, which is also the skill's directory and its store key.

    Raises:
        SkillRefused: With `conflict` set when the name is a shipped skill's — the one refusal that
            is about the deployment rather than about the document.
    """
    if len(body) > settings.agent_local_skill_max_chars:
        raise SkillRefused(
            f"a skill may be at most {settings.agent_local_skill_max_chars} characters and this "
            f"one is {len(body)}. A skill is judgment, not a transcript."
        )
    try:
        parsed = frontmatter.loads(body)
    except Exception as error:
        raise SkillRefused(
            f"the skill's frontmatter could not be parsed: {error}. It must open with `---`, a "
            "`name:` and a `description:`, then `---`."
        ) from error
    try:
        manifest = SkillManifest.model_validate(parsed.metadata)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
        raise SkillRefused(f"the skill's frontmatter is not valid: {problems}") from error
    if expected_name is not None and manifest.name != expected_name.strip():
        raise SkillRefused(
            f"the frontmatter declares the name {manifest.name!r} and the name given beside it is "
            f"{expected_name.strip()!r}. Send the same name in both, since the frontmatter is what "
            "a later turn reads."
        )
    if "/" in manifest.name or manifest.name.startswith("."):
        raise SkillRefused("a skill name may not contain '/' or start with '.'")
    if not storable_name(manifest.name):
        raise SkillRefused(
            "a skill name may not contain whitespace or control characters — use hyphens."
        )
    # Imported lazily: `langgraph_agent` imports this package.
    from chemclaw.agent.langgraph_agent import shipped_skill_names

    if manifest.name in shipped_skill_names():
        raise SkillRefused(
            f"{manifest.name!r} is the name of a skill this deployment already ships; give yours a "
            "different name so it is clear which judgment is acting",
            conflict=True,
        )
    return manifest.name
