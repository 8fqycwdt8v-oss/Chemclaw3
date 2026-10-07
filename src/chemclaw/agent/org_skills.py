"""The organisation's own skills: judgment an administrator approved, acting on everyone's turns.

An administrator writes this tier through `POST /skills/org`, every turn reads it, and no agent path
touches it. Promotion is by document (an admin pastes a body), not by reaching into a chemist's
proposal queue, so no route ever writes into a person's namespace
(D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius).

Two namespaces replace `git revert`:

- `("org-skills",)` holds the **active** body per name; only this is mounted.
- `("org-skills-versions", name)` holds every body ever activated, keyed by content hash, so a
  revert names bytes the system already holds. The version namespace is capped, so old bodies are
  eventually evicted.

The tier is not per-actor, so `agent/leaver.py` does not sweep it. It is narrowed by the stored half
of `skill_access.SkillNarrowing` (tool scope, not `EnabledSkills`, which names shipped skills).
Every org skill's name and description is in every model call's prefix, including each helper's,
which is why `agent_org_skills_max` is small.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from chemclaw.agent.audit import bounded_repr
from chemclaw.agent.local_skills import SkillRefused
from chemclaw.agent.refusal_route import routed
from chemclaw.agent.skill_store import (
    PermittedStoreBackend,
    advisory_writer_lock,
    list_skill_names,
    paged_items,
    read_skill_body,
    skill_key,
    storable_name,
    store_writer,
)
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.turn_signals import record_skill_loaded

logger = logging.getLogger(__name__)

#: The root the organisation's skills are mounted at, and the label the model sees in their paths;
#: it names no tenant because it appears in every prompt.
ORG_SKILLS_ROOT = "/org/"

#: The same label without its slashes, for the skills middleware's source list.
ORG_SKILLS_LABEL = "org"

#: What a refused write to this tier says; the route it names is this tier's own.
_ORG_READ_ONLY = routed(
    "the organisation's skills are read-only to a turn — a skill acts on everyone's answers, so it "
    "changes only when an administrator decides it does. Nothing was changed.",
    code="org_skills_read_only",
    boundary="the organisation's skills tier, which no turn may write",
    who_can_act="an administrator, through the organisation's skills route",
    sanctioned_path="draft the skill in your answer and say an administrator can publish it",
)


def org_skills_namespace() -> tuple[str, ...]:
    """The store namespace the active bodies live under.

    No actor component: the tier is the organisation's, and every session's prefix stays
    byte-identical. A distinct first component from `memories` and `local-skills` keeps the tiers
    separately countable and erasable.
    """
    return ("org-skills",)


def org_versions_namespace(name: str) -> tuple[str, ...]:
    """The store namespace one org skill's activated bodies live under.

    Per name, so a listing walks one skill's versions and the cap is per skill. Never mounted; a
    route reads it, so its documents may be JSON.
    """
    return ("org-skills-versions", name)


@dataclass(frozen=True)
class OrgSkillVersion:
    """One body that was once the organisation's judgment, as the version store answers it."""

    content_hash: str
    body: str
    activated_by: str
    activated_at: str


def _version_key(digest: str) -> str:
    """The key one activated body is held under.

    Its content hash, so re-activating the same bytes writes the same row.
    """
    return f"/{digest}"


def content_hash(body: str) -> str:
    """The identity of one document.

    `stable_hash`, the same function proposals and the note index use, so a revert target has one
    spelling.
    """
    return stable_hash(body)


def _count_an_org_load(name: str) -> None:
    """Book one delivered org-skill body.

    Labelled by name, unlike the personal tier's bare counter: an org skill's name is deployment
    configuration, not private vocabulary, and cardinality is bounded by `agent_org_skills_max`. It
    gives retirement decisions a usage signal.
    """
    record_metric(lambda m: m.increment("chemclaw_skill_loads_total", labels={"skill": name}))
    record_skill_loaded(name)


def org_skills_backend(store: Any, permits: Callable[[str], bool]) -> PermittedStoreBackend:
    """The mounted read half of the organisation's tier.

    A factory here because the refusal wording and the counter are this tier's knowledge.

    Args:
        store: The process's store.
        permits: `agent/skill_access.skill_permits`' composed narrowing, applied per reach.
    """
    namespace = org_skills_namespace()
    return PermittedStoreBackend(
        namespace=lambda _runtime: namespace,
        store=store,
        permits=permits,
        refusal=_ORG_READ_ONLY,
        on_load=_count_an_org_load,
    )


def _one_writer_per_org(name: str) -> AbstractAsyncContextManager[None]:
    """Serialize writes to one org skill, so the row cap is a bound and an activation is not split.

    `skill_store.advisory_writer_lock`, keyed per name so two administrators publishing different
    skills never contend; the cap is re-read under each lock.
    """
    return advisory_writer_lock(f"org-skills\x1f{name}")


async def list_org_skills(store: Any) -> list[str]:
    """The names of every skill the organisation keeps, sorted — **all** of them.

    Paged through `skill_store.paged_items`; the store's default page is 10.
    """
    return await list_skill_names(store, org_skills_namespace())


async def read_org_skill(store: Any, name: str) -> str | None:
    """One org skill's active body, verbatim, or `None` if there is no skill by that name.

    A name the writer would refuse is answered as absent rather than passed to the store.
    """
    return await read_skill_body(store, org_skills_namespace(), name)


async def list_org_versions(store: Any, name: str) -> list[OrgSkillVersion]:
    """Every body ever activated under `name`, newest activation first.

    The blame half of rollback: what changed, when, and by whom. Open to every authenticated caller,
    since the tier is in everyone's prompt.
    """
    if not storable_name(name):
        return []
    held = await paged_items(store, org_versions_namespace(name))
    versions = [_version_of(item) for item in held.values()]
    return sorted(
        (version for version in versions if version is not None),
        key=lambda version: version.activated_at,
        reverse=True,
    )


def _version_of(item: Any) -> OrgSkillVersion | None:
    """One stored version record, or `None` for a document this module did not write.

    Tolerant so one malformed row cannot take the listing route down.
    """
    content = item.value.get("content")
    if not isinstance(content, str):
        return None
    try:
        held = json.loads(content)
    except ValueError:
        return None
    if not isinstance(held, dict) or not isinstance(held.get("body"), str):
        return None
    return OrgSkillVersion(
        content_hash=str(item.key).lstrip("/"),
        body=held["body"],
        activated_by=str(held.get("activated_by", "")),
        activated_at=str(held.get("activated_at", "")),
    )


async def _record_a_version(store: Any, name: str, body: str, activated_by: str) -> None:
    """Hold these bytes as a revert target, and evict the least recently activated past the cap.

    Written before the active pointer moves, so a failure in between leaves an orphan version rather
    than losing bytes. Evicted rather than refused: a skill edited often must still accept a fix.
    """
    versions = store_writer(store, org_versions_namespace(name))
    digest = content_hash(body)
    await versions.awrite(
        _version_key(digest),
        json.dumps(
            {
                "body": body,
                "activated_by": activated_by,
                "activated_at": datetime.now(UTC).isoformat(),
            }
        ),
    )
    held = await paged_items(store, org_versions_namespace(name))
    cap = settings.agent_org_skill_versions_max
    if len(held) <= cap:
        return
    # `updated_at` is the store's own, written by the backend above, so "least recently activated"
    # is a fact the store holds rather than one this module would have to maintain beside it.
    stale = sorted(held.items(), key=lambda pair: str(pair[1].updated_at))[: len(held) - cap]
    for key, _item in stale:
        await versions.adelete(key)
    log_event(
        logger,
        "org_skill.versions_evicted",
        "dropped %d old version(s) of %s past the cap of %d",
        len(stale),
        bounded_repr(name),
        cap,
        level=logging.WARNING,
        skill=bounded_repr(name),
        dropped=len(stale),
        cap=cap,
    )


async def save_org_skill(store: Any, name: str, body: str, *, activated_by: str) -> None:
    """Publish one skill to the whole deployment, holding the bytes it replaces.

    The route validates `body` through `local_skills.validated_skill` first.

    Args:
        store: The process's store.
        name: The skill's name, which is also its directory and its key.
        body: The whole `SKILL.md`, frontmatter included.
        activated_by: The administrator's oid, for the version record's blame half.

    Raises:
        SkillRefused: The tier already holds `agent_org_skills_max` other skills.
    """
    async with _one_writer_per_org(name):
        held = await list_org_skills(store)
        # Refused rather than evicted, counted inside the lock. Replacing a held skill is allowed at
        # the cap so one can always be corrected.
        if name not in held and len(held) >= settings.agent_org_skills_max:
            raise SkillRefused(
                f"this deployment already keeps {len(held)} organisation skills, which is its "
                f"limit of {settings.agent_org_skills_max}: every one of them is in the prompt of "
                "every turn every chemist takes, and again in every helper a turn spawns, so "
                "retire one before publishing another",
                conflict=True,
            )
        await _record_a_version(store, name, body, activated_by)
        await store_writer(store, org_skills_namespace()).awrite(skill_key(name), body)
    log_event(
        logger,
        "org_skill.published",
        "%s published the organisation skill %s",
        bounded_repr(activated_by),
        bounded_repr(name),
        actor=bounded_repr(activated_by),
        skill=bounded_repr(name),
        chars=len(body),
    )


async def activate_org_version(store: Any, name: str, digest: str, *, activated_by: str) -> bool:
    """Make a body this tier already holds the active one again. Returns whether it was found.

    A revert names a held hash, so what becomes active is byte-for-byte what stood before. It goes
    through `save_org_skill`, so it spends the same cap, takes the same lock and records a version.
    """
    if not storable_name(name):
        return False
    item = await store.aget(org_versions_namespace(name), _version_key(digest))
    version = _version_of(item) if item is not None else None
    if version is None:
        return False
    await save_org_skill(store, name, version.body, activated_by=activated_by)
    log_event(
        logger,
        "org_skill.reverted",
        "%s reverted the organisation skill %s to %s",
        bounded_repr(activated_by),
        bounded_repr(name),
        bounded_repr(digest),
        actor=bounded_repr(activated_by),
        skill=bounded_repr(name),
        content_hash=bounded_repr(digest),
    )
    return True


async def retire_org_skill(store: Any, name: str, *, retired_by: str) -> bool:
    """Stop one org skill acting, keeping its history. Returns whether there was one to retire.

    Retiring is not reverting; the version namespace is left alone so `activate_org_version` can
    bring any held body back.
    """
    if not storable_name(name) or (
        await store.aget(org_skills_namespace(), skill_key(name)) is None
    ):
        return False
    await store_writer(store, org_skills_namespace()).adelete(skill_key(name))
    log_event(
        logger,
        "org_skill.retired",
        "%s retired the organisation skill %s",
        bounded_repr(retired_by),
        bounded_repr(name),
        actor=bounded_repr(retired_by),
        skill=bounded_repr(name),
    )
    return True
