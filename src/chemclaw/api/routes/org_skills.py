"""The organisation's skills over HTTP: open to read, closed to change.

The tier itself is `agent/org_skills.py`. Reads take any authenticated caller: the tier acts on
every chemist's turn, so everyone may inspect it and its versions. Writes take the privileged role
(`deps._is_reviewer`); a refusal is a 403 plus `record_refusal`. No route decides somebody else's
proposal: an admin promotes by posting a document, never by reaching into a person's queue.
Availability follows the memory store, as in `api/routes/skills.py`: without one, 503 rather than
an empty list.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from chemclaw.agent.local_skills import SkillRefused, validated_skill
from chemclaw.agent.org_skills import (
    activate_org_version,
    list_org_skills,
    list_org_versions,
    read_org_skill,
    retire_org_skill,
    save_org_skill,
)
from chemclaw.api.deps import ORG_SKILL, CurrentUser, _is_reviewer, record_refusal
from chemclaw.api.routes.skill_http import skill_refusal_http, store_or_503
from chemclaw.api.runner import turn_store


class OrgSkillIn(BaseModel):
    """One skill an administrator is publishing to the whole deployment.

    The body arrives entire — frontmatter included — rather than as fields this route assembles, so
    what is approved is byte-for-byte what every later turn reads. A route that built the
    frontmatter would be a second author of the document, and the document is the thing being
    gated. The length bound is the handler's rather than a `Field(max_length=…)` here, because it is
    a setting and a validator would bind whatever it read at *import*.
    """

    body: str = Field(min_length=1)


class OrgSkillRevertIn(BaseModel):
    """Which held body to make active again.

    `content_hash` and nothing else. A revert that named only the skill would mean "whatever was
    there before", which is not a decision anybody can check — the same argument
    `api/routes/proposals.DecisionIn` makes for binding a decision to the document it was shown.
    """

    content_hash: str = Field(min_length=1)


class OrgSkillOut(BaseModel):
    """One organisation skill, verbatim."""

    name: str
    body: str


class OrgSkillsOut(BaseModel):
    """The names of every skill acting on every turn in this deployment, sorted."""

    skills: list[str]


class OrgSkillVersionOut(BaseModel):
    """One body that was once active, and who made it so.

    The body is included rather than only its hash, because the question a reader has before
    reverting is *what would this put back* — and a version list that answered it with a digest
    would be asking for a decision about something unseen.
    """

    content_hash: str
    body: str
    activated_by: str
    activated_at: str


class OrgSkillVersionsOut(BaseModel):
    """Every held version of one organisation skill, newest activation first."""

    versions: list[OrgSkillVersionOut]


def _reviewer_or_refuse(principal: CurrentUser, target: str, act: str) -> None:
    """Refuse unless this caller holds the privileged role, and record it either way it goes.

    403 rather than 404: the tier's reads are open, so a name's existence is no secret
    (`deps.ORG_SKILL`).
    """
    if _is_reviewer(principal):
        return
    record_refusal(ORG_SKILL, "not a reviewer", principal, target, status=403)
    raise HTTPException(
        403,
        f"{act} is an administrator's action: an organisation skill is in the prompt of every turn "
        "every chemist takes, so changing one needs the privileged role this deployment names in "
        "CHEMCLAW_ENTRA_PRIVILEGED_ROLES",
    )


async def _store_or_refuse() -> object:
    """This deployment's store, or a 503 saying the tier is unavailable rather than empty.

    Without a store a listing would answer `[]` and a publish would vanish.
    """
    return store_or_503(
        await turn_store(),
        "this deployment keeps no organisation skills: it needs the durable memory store "
        "(CHEMCLAW_AGENT_MEMORY_ENABLED with a Postgres session store)",
    )


async def list_skills(principal: CurrentUser) -> OrgSkillsOut:
    """Every skill the organisation publishes — names, since this answers "what acts on me"."""
    store = await _store_or_refuse()
    return OrgSkillsOut(skills=await list_org_skills(store))


async def read_skill(name: str, principal: CurrentUser) -> OrgSkillOut:
    """One organisation skill, verbatim — the body a turn is actually given."""
    store = await _store_or_refuse()
    body = await read_org_skill(store, name)
    if body is None:
        raise HTTPException(404, f"this organisation publishes no skill named {name!r}")
    return OrgSkillOut(name=name, body=body)


async def read_versions(name: str, principal: CurrentUser) -> OrgSkillVersionsOut:
    """Every body ever activated under this name — the blame half of the rollback story."""
    store = await _store_or_refuse()
    return OrgSkillVersionsOut(
        versions=[
            OrgSkillVersionOut(
                content_hash=version.content_hash,
                body=version.body,
                activated_by=version.activated_by,
                activated_at=version.activated_at,
            )
            for version in await list_org_versions(store, name)
        ]
    )


async def publish_skill(body: OrgSkillIn, principal: CurrentUser) -> OrgSkillOut:
    """Publish one skill to everyone, holding the body it replaces as a revert target.

    The role check comes first, so validation cannot leak whether a name is taken; the recorded
    target is `<publish>` since there is no validated name yet.
    """
    _reviewer_or_refuse(principal, "<publish>", "publishing an organisation skill")
    store = await _store_or_refuse()
    try:
        name = validated_skill(body.body)
    except SkillRefused as refusal:
        raise skill_refusal_http(refusal) from refusal
    # The row cap is enforced by the writer, under the same lock that spends it.
    try:
        await save_org_skill(store, name, body.body, activated_by=principal.oid)
    except SkillRefused as refusal:
        raise skill_refusal_http(refusal) from refusal
    return OrgSkillOut(name=name, body=body.body)


async def revert_skill(name: str, body: OrgSkillRevertIn, principal: CurrentUser) -> OrgSkillOut:
    """Make a body this tier already holds the active one again.

    404 on a hash the tier does not hold: the pointer can only point at history.
    """
    _reviewer_or_refuse(principal, name, "reverting an organisation skill")
    store = await _store_or_refuse()
    try:
        reverted = await activate_org_version(
            store, name, body.content_hash, activated_by=principal.oid
        )
    except SkillRefused as refusal:
        raise skill_refusal_http(refusal) from refusal
    if not reverted:
        raise HTTPException(
            404,
            f"this organisation holds no version of {name!r} with that content hash — read "
            f"GET /skills/org/{name}/versions for the bodies it can be reverted to",
        )
    # Read back, so the caller is told what the tier now serves even if something raced this call.
    active = await read_org_skill(store, name)
    return OrgSkillOut(name=name, body=active or "")


async def forget_skill(name: str, principal: CurrentUser) -> OrgSkillsOut:
    """Stop one organisation skill acting, and answer with what is left.

    History survives, so the revert route can restore it.
    """
    _reviewer_or_refuse(principal, name, "retiring an organisation skill")
    store = await _store_or_refuse()
    if not await retire_org_skill(store, name, retired_by=principal.oid):
        raise HTTPException(404, f"this organisation publishes no skill named {name!r}")
    return OrgSkillsOut(skills=await list_org_skills(store))


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    App decorators, not an `APIRouter`; see `chemclaw/api/routes/jobs.py`'s `register`.
    """
    app.get("/skills/org", response_model=OrgSkillsOut)(list_skills)
    app.post("/skills/org", response_model=OrgSkillOut)(publish_skill)
    app.get("/skills/org/{name}", response_model=OrgSkillOut)(read_skill)
    app.delete("/skills/org/{name}", response_model=OrgSkillsOut)(forget_skill)
    app.get("/skills/org/{name}/versions", response_model=OrgSkillVersionsOut)(read_versions)
    app.post("/skills/org/{name}/revert", response_model=OrgSkillOut)(revert_skill)
