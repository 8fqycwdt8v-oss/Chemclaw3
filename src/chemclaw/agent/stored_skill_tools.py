"""What the two *stored* skills tiers declare about tools, read off the bodies they already hold.

`skill_access.ToolScopedSkills` reads declarations from `skill_manifest.declared_tools`, which walks
directories; the chemist's and the organisation's tiers are stored, so without this they had no
entry and a missing entry reads as "declares nothing", leaving every stored skill visible regardless
of the bound tools.

The declarations are parsed from the bodies on the same paged walk a listing costs, so the body is
the one source of truth and nothing needs migrating. It runs in the async caller that builds the
store (`api/runner.turn_store`), keeping `build_langgraph_agent` synchronous: one page per tier per
turn.

Fails closed: an unreadable body is scoped to `skill_manifest.UNREADABLE_DECLARATION`, as a filed
skill's unreadable manifest is. No stored skill name reaches a log line or metric label, since a
personal skill's name is a person's own words.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import frontmatter

from chemclaw.agent.local_skills import local_skills_namespace
from chemclaw.agent.org_skills import org_skills_namespace
from chemclaw.agent.skill_manifest import UNREADABLE_DECLARATION, declared_triple
from chemclaw.agent.skill_store import name_of_key, paged_items
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredSkillTools:
    """The `tools:` and `requires:` declarations of every stored skill a turn can reach.

    The same pair of maps the filed trees produce, so `skill_access.skill_permits` merges them
    without knowing two kinds of tier exist. Treat both as read-only: `frozen=True` does not freeze
    the dicts. A type rather than a tuple, so call sites say which map is which.
    """

    declared: dict[str, frozenset[str]] = field(default_factory=dict)
    required: dict[str, frozenset[str]] = field(default_factory=dict)


async def stored_skill_declarations(store: Any | None) -> StoredSkillTools:
    """Read both stored tiers' declarations, for the turn in flight.

    Mirrors the mounts in `scratchpad_backend`: the organisation's tier needs a store, the chemist's
    needs a store and an actor. The actor comes from the same ambient reader the mount uses
    (`get_current_actor()`, which normalizes), so both resolve the same namespace.

    A name held by both tiers keeps the organisation's declaration: `_skills_middleware` orders
    sources `/mine`, `/org`, then the reviewed trees, and upstream resolves collisions
    last-source-wins, so the org body is what the turn reads. Reading `/mine` first and `/org` last
    matches that, so one person's private document cannot decide an org skill's visibility.

    Args:
        store: The process's store, or `None` for a deployment with no durable memory — in which
        case neither tier is mounted and there is nothing to read.

    Returns:
        The two declaration maps, empty when no tier is reachable. Off the request path there is no
        actor, so only the organisation's tier is read, which is also what is mounted there.
    """
    if store is None:
        return StoredSkillTools()
    actor = get_current_actor()
    declared: dict[str, frozenset[str]] = {}
    required: dict[str, frozenset[str]] = {}
    # `/mine` first and `/org` last, so the later write wins for a name both hold, matching the body
    # a turn reads.
    namespaces = [local_skills_namespace(actor)] if actor else []
    namespaces.append(org_skills_namespace())
    for namespace in namespaces:
        for key, item in (await paged_items(store, namespace)).items():
            # Keyed by the store key's name, never the frontmatter's: the frontmatter may be
            # unreadable, and `validated_skill` checked the two agree before the body was written. A
            # missing entry must never happen, since it reads as "declares nothing".
            name = name_of_key(key)
            if name is None:
                continue
            declared[name], required[name] = _declaration(item)
    return StoredSkillTools(declared=declared, required=required)


def _declaration(item: Any) -> tuple[frozenset[str], frozenset[str]]:
    """One stored skill's two declarations, scoped to nothing when its body cannot be read.

    Reads `item.value["content"]` off the search result, so there is no extra round trip. The parsed
    name is discarded; the caller keys by the store key.
    """
    body = (item.value or {}).get("content") if isinstance(item.value, dict) else None
    if not isinstance(body, str):
        return _unreadable("its stored value carries no body")
    try:
        _declared_name, tools, requires = declared_triple(frontmatter.loads(body).metadata)
    except Exception as exc:
        # The exception's type, never its message: a parser quotes what it choked on, which could
        # put a person's own words into a shared log.
        return _unreadable(type(exc).__name__)
    return tools, requires


def _unreadable(why: str) -> tuple[frozenset[str], frozenset[str]]:
    """Scope one stored skill to nothing, and say so once.

    WARNING via `degraded`, matching `skill_manifest._declared_pair`: both write doors validate
    bodies, so this is a document stored before a rule tightened, and scoping to nothing is safe.
    Unlike the filed path the name is not logged, since a personal skill's name is a person's words.
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
