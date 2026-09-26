"""The organisation's own skills: judgment an administrator approved, acting on everyone's turns.

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` drew one axis — *does this change what the
agent does?* — and answered it with one gate and two destinations: a skill that reaches everyone
goes through an admin, and a skill that reaches one person goes through that person. The second
shipped (`agent/local_skills.py`). The first was `skills/` in git, reachable only by a reviewed
commit, so a deployment that learned something useful could put it in one chemist's tier or nowhere.

This is the missing destination. **An administrator writes it, every turn reads it, and no agent
path touches it** — `propose_skill` still writes a proposal and nothing else, the chemist still
accepts it into their own tier, and promoting a document from there to here is an act a privileged
role takes through `POST /skills/org`.

**Why the promotion unit is a document rather than a queue entry.** The obvious design gives an
admin a listing of everybody's open proposals and an `actor` field on the decision. It was refused:
`api/routes/proposals.py`'s load-bearing property is that it is *"owner-scoped by construction, not
by a check — there is no parameter naming whose queue to touch, so there is no authorization
decision here to get wrong"*, and an `actor` field converts that into a checked one on the
highest-consequence route in the module. It also breaks the rule this tier is built to respect: an
admin never writes into a person's namespace. Promoting a body costs the admin one paste and keeps
both properties (`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius`). The cost is real
and is recorded rather than hidden: there is no in-product way for a chemist to *request* a
promotion, and `docs/planning/BACKLOG.md` carries the row.

**Two namespaces, and the second is what replaces `git revert`.** `D-2026-09-05` grants the shared
tree its safety from being git-resident: *"a bad shared behaviour change is a revert — the rollback
property any future skill-evolution loop rests on."* A stored tier has no commit to revert, so it
carries its own history:

- `("org-skills",)` holds the **active** body per name. Only this is mounted, so a turn sees exactly
the active set and never a retired body.
- `("org-skills-versions", name)` holds **every body ever activated**, keyed by its content hash.

Reverting is therefore one call naming a hash the system already holds the bytes for, rather than an
admin retyping last week's text — which is a re-authoring wearing a rollback's name, and would pass
any test that checked only that the name still resolves.
`D-2026-09-20-a-revert-is-a-pointer-when-there-is-no-commit-to-revert` carries the argument and
names what is weaker here than in git: the version namespace is capped, so a body activated long
enough ago is evicted and is no longer a revert target.

**What this tier does *not* get, stated because three of them are deliberate.** It is not per-actor,
so `agent/leaver.py` does not sweep it — an erasure request that finds a departing person's words in
an organisation's skill is a content question for an admin (a revert, or a retire), not a prefix
sweep, and `tests/test_leaver.py` holds that as an assertion rather than as an absence.

**It is not narrowed by `EnabledSkills` — and this paragraph asserted that while it was.** The
reason given here was right: that setting names *shipped* skills, so applying it would delete this
tier outright rather than narrow it. Driven with `CHEMCLAW_SKILLS_ENABLED=development-report`,
`ls('/org/')` came back empty, on a tier that acts on everybody's turns. The exclusion is structural
now rather than asserted — `skill_access.SkillNarrowing` builds one predicate per kind of tier, and
the mount takes `.stored`.

**It is narrowed by `ToolScopedSkills`**, which this paragraph recorded as a gap needing each body's
frontmatter parsed out of the store "inside a possibly-synchronous `ls`". That framing is what made
it look expensive: `agent/stored_skill_tools.py` reads the bodies in the async caller instead, off
the same paged search a listing already costs, and the builder is handed the declarations the way it
is already handed the store.

**The prefix is the cost, and it is the reason the row cap is small.** Every org skill's name and
description sit in the system message of every model call every chemist makes — and again in every
helper a turn spawns, since a helper is compiled through the same builder over the same backend. A
four-helper fan-out therefore pays this tier five times. `agent_org_skills_max` is set from that
multiplier rather than from the personal tier's number, and `tests/test_context_floor.py` bounds the
whole tier against its own allowance.
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

#: The root the organisation's skills are mounted at, and the label the model sees in their paths.
#:
#: `org` rather than anything naming the deployment: the path appears in the system prompt of every
#: turn, and a tenant name there would be one more thing a prompt carries that a prompt need not.
ORG_SKILLS_ROOT = "/org/"

#: The same label without its slashes, for the skills middleware's source list.
ORG_SKILLS_LABEL = "org"

#: What a refused *write* to this tier says.
#:
#: The personal tier's wording names its owner and their route; the reviewed tree's names a reviewed
#: commit. Neither is true here, and a refusal that names the wrong way in is worse than one that
#: names none: it sends the model, and then the chemist reading its answer, somewhere that cannot
#: help them.
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

    **No actor component, and that absence is load-bearing twice.** It is what makes the tier the
    organisation's rather than a person's — every turn resolves the same namespace — and it is what
    keeps the system prefix byte-identical between two sessions, which
    `tests/test_context_floor.py` asserts of the whole system message.

    A distinct first component from `memories` and `local-skills`, for the reason
    `local_skills_namespace` gives: the three tiers are separately countable, separately erasable
    (or, here, deliberately not erasable), and a bug in one cannot serve another's rows.
    """
    return ("org-skills",)


def org_versions_namespace(name: str) -> tuple[str, ...]:
    """The store namespace one org skill's activated bodies live under.

    Keyed by name rather than holding every skill's history in one namespace, so a listing is one
    store walk over one skill's versions and the cap is per skill rather than per deployment.

    Not mounted anywhere: this is a record a route reads, never a tier a turn reaches. That is why
    its documents may be JSON rather than `SKILL.md` bodies — no model ever reads one.
    """
    return ("org-skills-versions", name)


@dataclass(frozen=True)
class OrgSkillVersion:
    """One body that was once the organisation's judgment, as the version store answers it.

    Frozen because a caller holding one is holding a record of something that happened.
    """

    content_hash: str
    body: str
    activated_by: str
    activated_at: str


def _version_key(digest: str) -> str:
    """The key one activated body is held under.

    Its content hash, so re-activating the same bytes writes the same row rather than a second
    one.
    """
    return f"/{digest}"


def content_hash(body: str) -> str:
    """The identity of one document.

    `stable_hash` rather than a fresh digest, so this repository keeps one answer to "are these the
    same bytes" — the same function `behaviour_proposals.content_hash` and the note index use. A
    revert names one of these, so two spellings of the digest would be two documents.
    """
    return stable_hash(body)


def _count_an_org_load(name: str) -> None:
    """Book one delivered org-skill body.

    **The labelled counter the reviewed tree uses, unlike the personal tier's bare one**, and the
    difference is whose words the label would carry. `agent/local_skills.py` books
    `chemclaw_local_skill_loads_total` with no label because a personal skill's name is one person's
    private project vocabulary appearing in a shared Prometheus exposition that no erasure reaches.
    An org skill's name is the deployment's own configuration — written by an administrator, read by
    everyone, listed on an open route — so the label carries nothing private, its cardinality is
    bounded by `agent_org_skills_max`, and it gives
    `D-2026-09-16-a-skill-nothing-counts-is-a-skill-nobody-can-retire` the retirement signal it asks
    for without a second mechanism.
    """
    record_metric(lambda m: m.increment("chemclaw_skill_loads_total", labels={"skill": name}))
    record_skill_loaded(name)


def org_skills_backend(store: Any, permits: Callable[[str], bool]) -> PermittedStoreBackend:
    """The mounted read half of the organisation's tier.

    A factory beside the tier rather than a constructor call at the mount point, for
    `local_skills_backend`'s reason: the refusal wording and the counter are this tier's knowledge,
    and `agent/scratchpad.py` composes routes.

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
    """Serialize writes to one org skill, so the row cap is a bound rather than a suggestion.

    `skill_store.advisory_writer_lock`, keyed on the skill rather than on a person. Beyond the
    cap race that lock was measured against, here the same race would also split an activation in
    half: the version row written and the active pointer not, or the reverse.

    Per name rather than per tier so two administrators publishing two different skills never
    contend; the row cap is read inside the lock anyway, so two *new* names racing at the cap is the
    one case this does not serialize, and it is bounded by the cap being re-read under each lock.
    """
    return advisory_writer_lock(f"org-skills\x1f{name}")


async def list_org_skills(store: Any) -> list[str]:
    """The names of every skill the organisation keeps, sorted — **all** of them.

    Paged through `skill_store.paged_items`, which both tiers and the capability narrowing now
    share: un-paged this answers ten and reads as the whole tier, which here would mean an
    administrator unable to see or retire the eleventh skill acting on everybody's turns.
    """
    return await list_skill_names(store, org_skills_namespace())


async def read_org_skill(store: Any, name: str) -> str | None:
    """One org skill's active body, verbatim, or `None` if there is no skill by that name.

    A name the writer would have refused is answered as absent rather than passed to the store —
    `local_skills.storable_name` measured a 500 out of the shipped backend for a name that cannot
    exist, and this tier's read route takes a path parameter exactly as that one does.
    """
    return await read_skill_body(store, org_skills_namespace(), name)


async def list_org_versions(store: Any, name: str) -> list[OrgSkillVersion]:
    """Every body ever activated under `name`, newest activation first.

    This is the blame half of the rollback story: it answers *what changed, when and who* for a tier
    that has no commit log. Open to every authenticated caller, because the tier is in their prompt
    — `D-2026-09-05` §3 makes inspectability the condition a tier holds its exemption under, and a
    tier every person pays for owes that more than one person's own does.
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

    Tolerant rather than raising, because the alternative is a listing route that a single malformed
    row takes down — and the row this reads is one an earlier version of this module wrote, which is
    precisely the shape that changes under a migration nobody remembers to write.
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

    Written before the active pointer moves, so a failure between the two leaves the tier serving
    what it served and the history holding one row nothing points at — which is recoverable and
    inspectable. The other order loses the bytes that were about to become reachable.

    **Evicted rather than refused, unlike the name cap, and the asymmetry is the point.** Refusing a
    version would mean an administrator could not publish a fix because the skill had been edited
    too often, which puts a bound on the wrong thing entirely. Evicting the least recently activated
    is `scratchpad.BoundedStoreBackend`'s tiebreak, taken for its reason: it is the only ordering
    the store carries, and the version anybody would actually revert to is a recent one.
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

    The route validates `body` through `local_skills.validated_skill` before calling this, so the
    four admission rules are the tier's rather than a surface's — the hole that function exists to
    close was two doors into the personal tier disagreeing about what a skill is.

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
        # Refused rather than evicted, and counted inside the lock so the cap binds the tier rather
        # than trailing it by however many requests arrived together. Replacing a skill already held
        # is not a new row, so it is allowed at the cap — otherwise nobody could correct one.
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

    **This is what a revert is, and why it is not a re-authoring.** The administrator names a hash
    the system holds the bytes for, so what becomes active is byte-for-byte what stood before rather
    than what somebody retyped. A hash the version namespace does not hold is answered as absent —
    the pointer can only point at history, which is the property that makes this a rollback.

    It goes through `save_org_skill` rather than writing the active key directly, so a revert spends
    the same row cap, takes the same lock and leaves the same version record as any other
    publication. A revert is an ordinary activation whose bytes happen to be old.
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

    **Retiring and reverting are different acts and this is only the first.** Removing the active
    body takes the deployment from bad judgment to *no* judgment, which is a third state rather than
    last week's — so this deliberately leaves the version namespace alone and stays itself
    reversible: `activate_org_version` brings any held body back afterwards.
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
