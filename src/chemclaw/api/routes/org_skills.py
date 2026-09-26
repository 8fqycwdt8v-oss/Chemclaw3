"""The organisation's skills over HTTP: open to read, closed to change.

`agent/org_skills.py` holds the tier and says why it exists; this is the surface that makes it
usable, and the split down the middle of this module is the whole design.

**The three reads take any authenticated caller, and that is a requirement rather than laxity.**
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` §3 grants a skills tier its exemption from
per-use review *on the condition* that the people it acts on can see what is acting on them — "a
behaviour change nobody can inspect is the property that makes the shared tier need a gate". The
personal tier owes that to one person. This one is in the prompt of every turn every chemist takes,
so it owes it to all of them, and a version list nobody but an admin could read would make the
rollback story something the organisation has to take on trust.

**The three writes take the privileged role**, through `deps._is_reviewer` — the same role set that
guards every write tool rather than a new one. That function's own docstring predicted this subject:
*"The role is also what an admin will hold when a skill is proposed."* A refusal here is a 403 plus
`record_refusal`, the shape `api/routes/protocols.py` uses, because the record and the response are
different decisions and it was the record that used to go unwritten.

**What is deliberately absent is a route that decides somebody else's proposal.** An admin promotes
by posting a *document*, not by reaching into a queue: `api/routes/proposals.py` stays owner-scoped
by construction, with no parameter naming whose queue to touch, and no admin ever writes into a
person's namespace. `agent/org_skills.py` carries the argument and the cost.

**Availability rides the memory store**, exactly as `api/routes/skills.py` does and for its reason:
a second flag would be a second switch for one resource, and a 503 that says the tier is
*unavailable* is the honest answer where an empty list would say the deployment simply has none.
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


def _refused(error: SkillRefused) -> HTTPException:
    """One refusal as this surface's status code — 409 for a taken name or a full tier, 422 else.

    The admission rules live with the tier (`agent/local_skills.validated_skill`) rather than here,
    for the reason that function's docstring measures: a rule stated at one surface is a rule the
    other surface does not have. What is left here is the translation.
    """
    return HTTPException(409 if error.conflict else 422, str(error))


def _reviewer_or_refuse(principal: CurrentUser, target: str, act: str) -> None:
    """Refuse unless this caller holds the privileged role, and record it either way it goes.

    403 rather than this module's neighbours' 404: the tier's reads are open, so a skill name's
    existence is not a secret and only the right to change it is withheld (`deps.ORG_SKILL`).

    `record_refusal` rather than a bare `raise`, because the response and the server-side record are
    different decisions and `api/routes/protocols.py` exists as the precedent for the case where
    only one of them was being made.
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

    `api/routes/skills.py`'s reason exactly: with no store a listing would answer `[]` and a publish
    would appear to succeed and vanish, so the surface would read "this organisation keeps no
    skills" when the truth is "this deployment cannot keep any".
    """
    store = await turn_store()
    if store is None:
        raise HTTPException(
            503,
            "this deployment keeps no organisation skills: it needs the durable memory store "
            "(CHEMCLAW_AGENT_MEMORY_ENABLED with a Postgres session store)",
        )
    return store


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

    Validated before the role is checked would leak whether a name is taken to a caller with no
    role, so the gate comes first — `_reviewer_or_refuse` is the first statement, and the name it
    records is the one the *frontmatter* will declare only after validation, so the target recorded
    is `<publish>` until there is a document to name.
    """
    _reviewer_or_refuse(principal, "<publish>", "publishing an organisation skill")
    store = await _store_or_refuse()
    try:
        name = validated_skill(body.body)
    except SkillRefused as refusal:
        raise _refused(refusal) from refusal
    # The row cap rides on the writer, not on this route: it has to be counted and spent under one
    # lock, and a check here would be the second copy the personal tier's acceptance door already
    # proved goes stale.
    try:
        await save_org_skill(store, name, body.body, activated_by=principal.oid)
    except SkillRefused as refusal:
        raise _refused(refusal) from refusal
    return OrgSkillOut(name=name, body=body.body)


async def revert_skill(name: str, body: OrgSkillRevertIn, principal: CurrentUser) -> OrgSkillOut:
    """Make a body this tier already holds the active one again.

    404 on a hash the version namespace does not hold, which is the property that makes this a
    rollback rather than a write: the pointer can only point at history.
    """
    _reviewer_or_refuse(principal, name, "reverting an organisation skill")
    store = await _store_or_refuse()
    try:
        reverted = await activate_org_version(
            store, name, body.content_hash, activated_by=principal.oid
        )
    except SkillRefused as refusal:
        raise _refused(refusal) from refusal
    if not reverted:
        raise HTTPException(
            404,
            f"this organisation holds no version of {name!r} with that content hash — read "
            f"GET /skills/org/{name}/versions for the bodies it can be reverted to",
        )
    # Read back rather than echoed from the version record, so what the caller is told is what the
    # tier now serves — the two can only differ if something raced this call, and that is precisely
    # the case where echoing would be a confident lie.
    active = await read_org_skill(store, name)
    return OrgSkillOut(name=name, body=active or "")


async def forget_skill(name: str, principal: CurrentUser) -> OrgSkillsOut:
    """Stop one organisation skill acting, and answer with what is left.

    The history survives, so this is reversible through the revert route: retiring a skill and
    rolling one back are different acts and only one of them is this.
    """
    _reviewer_or_refuse(principal, name, "retiring an organisation skill")
    store = await _store_or_refuse()
    if not await retire_org_skill(store, name, retired_by=principal.oid):
        raise HTTPException(404, f"this organisation publishes no skill named {name!r}")
    return OrgSkillsOut(skills=await list_org_skills(store))


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`, for the
    reason every `register` in this package gives.
    """
    app.get("/skills/org", response_model=OrgSkillsOut)(list_skills)
    app.post("/skills/org", response_model=OrgSkillOut)(publish_skill)
    app.get("/skills/org/{name}", response_model=OrgSkillOut)(read_skill)
    app.delete("/skills/org/{name}", response_model=OrgSkillsOut)(forget_skill)
    app.get("/skills/org/{name}/versions", response_model=OrgSkillVersionsOut)(read_versions)
    app.post("/skills/org/{name}/revert", response_model=OrgSkillOut)(revert_skill)
