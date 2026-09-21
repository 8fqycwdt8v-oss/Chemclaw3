"""What the two *stored* skills tiers declare about tools, read off the bodies they already hold.

**Why this module exists.** `skill_access.ToolScopedSkills` hides a skill whose every declared tool
is absent from the turn's surface, and it reads `skill_manifest.declared_tools` — which globs a
*directory*. The chemist's own tier and the organisation's are stored rather than filed, so neither
had an entry in that map, and `ToolScopedSkills._permits` reads a missing entry as "declares
nothing" and leaves the skill visible to everyone. The narrowing therefore ran and could not narrow:
measured, a personal skill declaring `[compute_thermochemistry, sample_conformers]` was served and
listed in a turn that bound **zero** tools, while 34 of 39 filed skills were hidden by that same
predicate in that same turn.

**Read, rather than the parse-on-write the backlog row proposed.** The row's cheap shape was to
parse each body in the two publish routes and keep the declaration beside it. That is a second
stored artefact per skill and a migration for every skill already saved, and it can drift from the
body it describes. `BaseStore.asearch` already returns each item's whole `value`, `content`
included, so the declarations come off the same paged walk a listing costs — one source of truth
(the body), nothing to migrate, and nothing that can disagree with what the model reads.

**The row's actual objection is answered by *where* this runs, not by caching it.** It said applying
the narrowing would mean "parsing every body out of the store inside a possibly-synchronous `ls`" —
and it would, if a backend did it. This is called from the async caller that already builds the
store, which is the seam `store` and `checkpointer` established precisely because
`build_langgraph_agent` is synchronous and must stay so (see `api/runner.turn_store`). One walk per
turn per tier, on a cap of `agent_local_skills_max` + `agent_org_skills_max` rows, which is one page
each.

**Fail closed, because a declaration may only ever cost a skill its visibility.** A body whose
frontmatter cannot be read is scoped to `skill_manifest.UNREADABLE_DECLARATION` — a tool name
nothing can have — exactly as a filed skill's unreadable manifest is. The opposite (dropping the
entry) would make an unparseable body a *widening*, which is the defect `_declared_pair`'s own
`except` arm was written to stop. It cannot normally happen here, because both write doors run
`validated_skill` before storing anything; what makes it reachable at all is a body stored before a
stricter rule existed, which is the same class of thing `UnreservedNames` closes on the name.

**No name from here reaches a log line or a metric label.** A personal skill's name is a person's
own words — `local_skills._count_a_local_load` refuses to put one in a metric label for that reason
— so `langgraph_agent._log_narrowing` keeps counting the *filed* map alone even though the narrowing
now reads both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import frontmatter

from chemclaw.agent.local_skills import LOCAL_SKILL_FILENAME, local_skills_namespace
from chemclaw.agent.org_skills import org_skills_namespace
from chemclaw.agent.skill_manifest import UNREADABLE_DECLARATION, declared_triple
from chemclaw.agent.skill_store import paged_items
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredSkillTools:
    """The `tools:` and `requires:` declarations of every stored skill a turn can reach.

    The same pair of maps `skill_manifest.declared_tools`/`required_tools` answer for the filed
    trees, so `skill_access.skill_permits` takes one merged map of each and does not learn that two
    kinds of tier exist. Both are read-only to every caller, as the filed maps are.

    A type rather than a bare tuple because this is threaded through `build_langgraph_agent` beside
    `store` and `checkpointer`, and `(dict, dict)` at a call site says nothing about which is which.
    """

    declared: dict[str, frozenset[str]] = field(default_factory=dict)
    required: dict[str, frozenset[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Whether anything was read, so a caller can skip the merge entirely."""
        return bool(self.declared or self.required)


async def stored_skill_declarations(store: Any | None, actor: str) -> StoredSkillTools:
    """Read both stored tiers' declarations, for the turn this actor is taking.

    **Two tiers, two conditions, mirroring the mount exactly.** `scratchpad_backend` mounts the
    organisation's tier on a store alone and the chemist's on a store *and* an actor; asking about a
    tier a turn has no mount for would narrow by a declaration the model can never reach, which is a
    gate disagreeing with a listing in the direction that hides a skill for no reason.

    A name held by both tiers keeps the **personal** declaration, because `_skills_middleware`
    mounts `/mine` before `/org` and upstream resolves a collision by listing order, so the personal
    body is the one a turn reads. That the write door refuses the reverse direction (a personal
    skill may not take an org name, an admin may take a personal one) is what makes the collision
    possible at all — see `local_skills.save_local_skill`.

    Args:
        store: The process's store, or `None` for a deployment with no durable memory — in which
            case neither tier is mounted and there is nothing to read.
        actor: The turn's actor, empty off the request path. Empty means no personal mount, so only
            the organisation's tier is read.

    Returns:
        The two declaration maps, empty when no tier is reachable.
    """
    if store is None:
        return StoredSkillTools()
    declared: dict[str, frozenset[str]] = {}
    required: dict[str, frozenset[str]] = {}
    namespaces = [org_skills_namespace()]
    if actor:
        # Read last so it wins the `update` below, which is the order the mount resolves in.
        namespaces.append(local_skills_namespace(actor))
    for namespace in namespaces:
        for key, item in (await paged_items(store, namespace)).items():
            # **Keyed by the store key's name, never by the frontmatter's**, which is the same
            # decision `_declared_pair` makes when it keys an unreadable filed manifest by its
            # directory: the frontmatter is the thing that might not be readable, so its `name:` is
            # no more trustworthy than its `tools:`. Here the key is additionally the stronger
            # source — `validated_skill` compared the two before this body was written, so the key
            # is what the reader of a working body would have produced anyway, and a *missing* entry
            # is the one outcome that must not happen because it reads as "declares nothing".
            name = _name_of(key)
            if name is None:
                continue
            declared[name], required[name] = _declaration(item)
    return StoredSkillTools(declared=declared, required=required)


def _name_of(key: str) -> str | None:
    """The skill a store key names, or `None` for a key that is not a skill body.

    The shape `StoreBackend` writes is `/<name>/SKILL.md` (`local_skills._key`), and a key of any
    other shape is not this tier's — the same filter both listings apply, stated here because a
    non-skill key must not become a declaration keyed by a nonsense name.
    """
    suffix = f"/{LOCAL_SKILL_FILENAME}"
    if not key.startswith("/") or not key.endswith(suffix) or len(key) <= len(suffix) + 1:
        return None
    return key[1 : -len(suffix)]


def _declaration(item: Any) -> tuple[frozenset[str], frozenset[str]]:
    """One stored skill's two declarations, scoped to nothing when its body cannot be read.

    `item.value["content"]` is the body upstream's `StoreBackend` stores, and reading it off the
    search result is what makes this one round trip rather than one per skill.

    The declared name that comes back is discarded: the caller keys by the store key, and says why.
    """
    body = (item.value or {}).get("content") if isinstance(item.value, dict) else None
    if not isinstance(body, str):
        return _unreadable("its stored value carries no body")
    try:
        _declared_name, tools, requires = declared_triple(frontmatter.loads(body).metadata)
    except Exception as exc:
        return _unreadable(str(exc))
    return tools, requires


def _unreadable(why: str) -> tuple[frozenset[str], frozenset[str]]:
    """Scope one stored skill to nothing, and say so once.

    WARNING rather than ERROR, matching `skill_manifest._declared_pair`: a body this unreadable was
    refused at both write doors, so an occurrence is a document stored before a rule tightened
    rather than an outage — and the skill being scoped to nothing is the safe answer either way.
    `degraded` is what makes it visible at all, since a skill silently absent from every turn
    produces no other signal.

    **The name is not in the message**, which is the one difference from the filed path: a personal
    skill's name is a person's words, and this line would carry them into a shared log. The counter
    `degraded` books is what says it happened; a body that needs identifying is identified through
    the route its owner calls.
    """
    degraded(
        logger,
        "stored_skill_manifest",
        "a stored skill's frontmatter could not be read, scoping it to nothing: %s",
        why,
        level=logging.WARNING,
        exc_info=False,
    )
    return UNREADABLE_DECLARATION, UNREADABLE_DECLARATION
