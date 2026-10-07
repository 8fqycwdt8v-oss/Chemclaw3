"""A chemist's own skills: judgment that shapes their turns and nobody else's.

The chemist writes this tier through `POST /skills/mine` (or by accepting a proposal); no agent path
writes a skill, and `agent/skill_store.PermittedStoreBackend` raises `SkillsReadOnlyRefusal` on
every write verb. Invariants:

- **Per-actor, resolved per turn.** The namespace closes over the turn's actor when
  `build_langgraph_agent` builds the backend, so another chemist's turn cannot reach it.
- **Never a source of shared truth.** Nothing promotes or cites one.
- **Inspectable.** `api/routes/skills.py` lists, reads and deletes.

Storage is the Postgres store, not a directory, because pod filesystems are ephemeral and
replicated; its namespace is the erasure key `agent/leaver.py` sweeps by prefix. The tier is
narrowed by the stored half of `skill_access.SkillNarrowing` (profile, role and tool scope, not
`EnabledSkills`, which names shipped skills and would empty it).
"""

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import frontmatter
from pydantic import ValidationError

from chemclaw.agent.audit import bounded_repr
from chemclaw.agent.refusal_route import routed
from chemclaw.agent.skill_manifest import SkillManifest
from chemclaw.agent.skill_store import (
    PermittedStoreBackend,
    advisory_writer_lock,
    list_skill_names,
    read_skill_body,
    skill_key,
    storable_name,
    store_writer,
)
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.turn_signals import record_skill_loaded

logger = logging.getLogger(__name__)

#: The root a chemist's own skills are mounted at, and the label the model sees in their paths.
#: `mine` rather than an actor digest, so no person identifier appears in prompts or logs.
LOCAL_SKILLS_ROOT = "/mine/"

#: The same label without its slashes, for the skills middleware's source list.
LOCAL_SKILLS_LABEL = "mine"

#: What a refused write to this tier says; the sanctioned path is the owner's route.
_LOCAL_READ_ONLY = routed(
    "your own skills are read-only to a turn — a skill is judgment that reshapes later answers, "
    "so it changes only when you decide it does, not when a turn decides. Nothing was changed.",
    code="local_skills_read_only",
    boundary="the chemist's own skills tier, which no turn may write",
    who_can_act="the chemist who owns it, through the skills route",
    sanctioned_path="draft the skill in your answer and say it can be saved from there",
)


class SkillRefused(ChemclawError):
    """A document this tier will not keep, and whether the reason is a conflict or a fault.

    One exception carrying `conflict`, since every caller translates it (409/422, or prose) anyway.
    """

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        """Refuse, saying whether the name is taken (`conflict`) or the document is malformed."""
        super().__init__(message)
        self.conflict = conflict


def validated_skill(body: str, *, expected_name: str | None = None) -> str:
    """The name this body declares, or a refusal naming what is wrong with it.

    Every door into a stored skills tier goes through here (save, proposal acceptance,
    `propose_skill`, the distiller, org publish and revert), so the admission rules belong to the
    tier rather than to a surface. The size bound is not per tier: it bounds what a skill is.

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
    # Imported lazily: `langgraph_agent` imports this module.
    from chemclaw.agent.langgraph_agent import shipped_skill_names

    if manifest.name in shipped_skill_names():
        raise SkillRefused(
            f"{manifest.name!r} is the name of a skill this deployment already ships; give yours a "
            "different name so it is clear which judgment is acting",
            conflict=True,
        )
    return manifest.name


#: In-process tools whose only outcome is a personal skill, so they are not bound where
#: `personal_skills_available()` is false.
PERSONAL_TIER_TOOLS = frozenset({"propose_skill"})


def personal_skills_available() -> bool:
    """Whether this deployment can keep a chemist's own skill at all.

    Requires `agent_memory_enabled` (agent-authored files may outlive a session) and a Postgres
    `session_store` (the store shares its pool). One predicate for the mount, the route and the
    binding of `PERSONAL_TIER_TOOLS`, so a tool whose only outcome is unreachable is not offered.

    Returns:
        True where a proposal has somewhere durable to land and a route that can accept it.
    """
    return settings.agent_memory_enabled and settings.session_store == "postgres"


def local_skills_namespace(actor: str) -> tuple[str, ...]:
    """The store namespace one person's own skills live under.

    Digested like `scratchpad.memory_namespace`: always a legal namespace component, and an actor's
    two spellings stay distinct. A different first component from `memories`, so the tiers are
    separately erasable and countable.

    Args:
        actor: The turn's actor id, in whichever spelling the caller holds.

    Returns:
        The namespace tuple, stable for one actor across processes and restarts.
    """
    return ("local-skills", stable_hash(actor))


def local_skills_prefix(actor: str) -> str:
    """The `store.prefix` value naming one person's own skills, for the erasure sweep.

    Exposed so `agent/leaver.py` uses this module's key rather than re-deriving it.
    """
    return ".".join(local_skills_namespace(actor))


def _count_a_local_load(name: str) -> None:
    """Book one delivered personal-skill body, on the two channels this tier may use.

    A bare counter, because a personal skill's name is private vocabulary and a label would put it
    in a shared exposition. `record_skill_loaded` carries the name in process only (digested before
    it reaches `turn_costs`) for the distiller's self-confirmation guard.
    """
    record_metric(lambda m: m.increment("chemclaw_local_skill_loads_total"))
    record_skill_loaded(name)


def local_skills_backend(
    store: Any, actor: str, permits: Callable[[str], bool]
) -> PermittedStoreBackend:
    """The mounted read half of one chemist's own tier.

    A factory because the namespace, refusal wording and counter are this tier's knowledge.

    Args:
        store: The process's store.
        actor: Whose tier this is, in the turn's own actor spelling.
        permits: `agent/skill_access.SkillNarrowing.stored`, the stored half of this turn's
            narrowing, applied per reach.
    """
    namespace = local_skills_namespace(actor)
    return PermittedStoreBackend(
        namespace=lambda _runtime: namespace,
        store=store,
        permits=permits,
        refusal=_LOCAL_READ_ONLY,
        on_load=_count_a_local_load,
    )


def _one_writer_per_chemist(actor: str) -> AbstractAsyncContextManager[None]:
    """Serialize this chemist's saves, so the row cap is a bound rather than a suggestion.

    Per chemist via `skill_store.advisory_writer_lock`, so two people never contend.
    """
    return advisory_writer_lock(f"local-skills\x1f{actor}")


async def save_local_skill(store: Any, actor: str, name: str, body: str) -> None:
    """Write one of a chemist's own skills, replacing any earlier version of that name.

    No version history: a skill is what its owner asserts now. This write comes from a route a
    person calls, not a tool call, so it crosses no audit middleware; authorization is the route's
    `CurrentUser` plus a namespace derived from the caller, and an INFO line records who changed
    which skill.

    Args:
        store: The process's store.
        actor: Whose tier to write, in the turn's own actor spelling.
        name: The skill's name, which is also its directory.
        body: The whole `SKILL.md`, frontmatter included.
    """
    async with _one_writer_per_chemist(actor):
        held = await list_local_skills(store, actor)
        # Refused rather than evicted, counted inside the lock. Replacing a held skill is allowed at
        # the cap so one can always be corrected.
        if name not in held and len(held) >= settings.agent_local_skills_max:
            raise SkillRefused(
                f"you already keep {len(held)} personal skills, which is this deployment's limit "
                f"of {settings.agent_local_skills_max}: every one of them is in the prompt of "
                "every turn you take, so remove one before adding another",
                conflict=True,
            )
        # A name the organisation already publishes is refused here (it would never act, since
        # `/org` is mounted after `/mine`). The reverse is allowed, so one person's private name
        # cannot block a deployment-wide publication.
        from chemclaw.agent.org_skills import list_org_skills

        if name in await list_org_skills(store):
            raise SkillRefused(
                f"{name!r} is the name of a skill your organisation publishes to everyone, so a "
                "personal one by that name would never act — give yours a different name",
                conflict=True,
            )
        await store_writer(store, local_skills_namespace(actor)).awrite(skill_key(name), body)
    log_event(
        logger,
        "local_skill.saved",
        "%s saved their own skill %s",
        bounded_repr(actor),
        bounded_repr(name),
        actor=bounded_repr(actor),
        skill=bounded_repr(name),
        chars=len(body),
    )


async def list_local_skills(store: Any, actor: str) -> list[str]:
    """The names of one chemist's own skills, sorted — **all** of them.

    Read off the store because the route has no turn and so no mount. Paged
    (`skill_store.list_skill_names`), since the store's default page is 10 and a chemist must be
    able to see and delete every skill acting on their turns.
    """
    return await list_skill_names(store, local_skills_namespace(actor))


async def read_local_skill(store: Any, actor: str, name: str) -> str | None:
    """One of a chemist's own skills, verbatim, or `None` if they have no skill by that name.

    A name the writer would refuse is answered as absent (see `storable_name`).
    """
    return await read_skill_body(store, local_skills_namespace(actor), name)


async def delete_local_skill(store: Any, actor: str, name: str) -> bool:
    """Remove one of a chemist's own skills. Returns whether there was one to remove."""
    if not storable_name(name) or (
        await store.aget(local_skills_namespace(actor), skill_key(name)) is None
    ):
        return False
    # Through the same backend the write uses, so no first-party module reaches the store's write
    # verbs directly.
    await store_writer(store, local_skills_namespace(actor)).adelete(skill_key(name))
    log_event(
        logger,
        "local_skill.removed",
        "%s removed their own skill %s",
        bounded_repr(actor),
        bounded_repr(name),
        actor=bounded_repr(actor),
        skill=bounded_repr(name),
    )
    return True


# The tier's bounds, `agent_local_skill_max_chars` and `agent_local_skills_max`, live in
# `core/config/agent.py`. The row cap bounds prefix spend and is refused rather than evicted.
