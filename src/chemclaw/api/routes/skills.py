"""A chemist's own skills over HTTP: the only way one is written, listed or removed.

`agent/local_skills.py` holds the tier and says why it exists; this is the surface that makes it
usable, and three of its four properties are requirements rather than conveniences.

**The write is a route and never a tool**, which is the same shape `api/routes/plan.py` uses and
the same reason: a model must never be able to authorize its own behaviour change. A skill is
injected into the prompt and reshapes every later answer with no citation trail — the exact
inverse of the property that makes ungated knowledge safe
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`) — so the agent may draft one into its
answer and a person decides whether it becomes judgment.
`agent/skill_backend.SkillsReadOnlyRefusal` is what makes that structural rather than a convention,
and it is untouched by this module.

**The read and the delete are the tier's licence to exist.** That ADR's §3 grants the local tier its
exemption from review on one condition, stated as a requirement: *"a chemist must be able to list
and read the local skills acting on their turns, and remove one. A behaviour change nobody can
inspect is the property that makes the shared tier need a gate."* An inspectable change nobody can
withdraw is the worse bargain of the two, because the person has learned something is acting on
them and still cannot stop it.

**Owner-scoped by construction, not by a check.** Every handler reads `principal.oid` and passes it
to a namespace derived from it — there is no path parameter naming whose tier to touch, so there is
no authorization decision here to get wrong. That is deliberate: `GET /notes/{id}` can afford to be
un-scoped because the graph is the organisation's, and this is the opposite case.

**Availability rides the memory store, and the coupling is stated rather than switched.** The tier
is stored in the same `AsyncPostgresStore` that serves `/memories/`, so it is available exactly when
that is — `agent_memory_enabled` and a Postgres session store. A second flag would be a second
switch for one resource, and `D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has` is the
reason it is not defaulted off besides.
"""

import frontmatter
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError

from chemclaw.agent.local_skills import (
    MAX_LOCAL_SKILL_CHARS,
    delete_local_skill,
    list_local_skills,
    read_local_skill,
    save_local_skill,
)
from chemclaw.agent.skill_manifest import SkillManifest
from chemclaw.api.deps import CurrentUser
from chemclaw.api.runner import turn_store


class LocalSkillIn(BaseModel):
    """One skill a chemist is asking to keep, as the whole `SKILL.md` they are asserting.

    The body arrives entire — frontmatter included — rather than as fields this route assembles,
    so what a person approves is byte-for-byte what a later turn reads. A route that built the
    frontmatter would be a second author of the document, and the thing being gated here is
    precisely the document.
    """

    body: str = Field(min_length=1, max_length=MAX_LOCAL_SKILL_CHARS)


class LocalSkillOut(BaseModel):
    """One of a chemist's own skills, verbatim."""

    name: str
    body: str


class LocalSkillsOut(BaseModel):
    """The names of every skill acting on this chemist's turns, sorted.

    Names rather than bodies: this answers "what is acting on me", which is a question about the
    set, and a listing that returned every body would be a page nobody reads for an answer that
    fits on a line.
    """

    skills: list[str]


def _validated_name(body: str) -> str:
    """The skill name this body declares, or a 422 naming what is wrong with it.

    Validated against the same `SkillManifest` the shared tree is, so a local skill cannot be a
    document the listing then silently skips — which is the failure mode a tier with no validator
    has: the person is told it was saved and no turn ever sees it.

    The name comes *from the frontmatter* rather than from a separate field, for the reason
    `agent/profile_discovery.py` refuses a `name:` key beside a filename: two sources of one name
    can disagree, and the one the reader believes is whichever the code happens to consult.
    """
    try:
        parsed = frontmatter.loads(body)
    except Exception as error:
        raise HTTPException(422, f"the skill's frontmatter could not be parsed: {error}") from error
    try:
        manifest = SkillManifest.model_validate(parsed.metadata)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
        raise HTTPException(422, f"the skill's frontmatter is not valid: {problems}") from error
    if "/" in manifest.name or manifest.name.startswith("."):
        raise HTTPException(422, "a skill name may not contain '/' or start with '.'")
    return manifest.name


async def _store_or_refuse() -> object:
    """This deployment's store, or a 503 saying the tier is unavailable rather than empty.

    The distinction is the whole point of raising here: with no store, a list would answer `[]` and
    a save would appear to succeed and vanish — a surface that reads as "you have no skills" when
    the truth is "this deployment cannot keep any". A confident empty answer about a mechanism that
    is not running is the failure `Chemclaw3_ui`'s review queue has had to delete twice.
    """
    store = await turn_store()
    if store is None:
        raise HTTPException(
            503,
            "this deployment keeps no personal skills: it needs the durable memory store "
            "(CHEMCLAW_AGENT_MEMORY_ENABLED with a Postgres session store)",
        )
    return store


async def list_skills(principal: CurrentUser) -> LocalSkillsOut:
    """Every skill acting on this chemist's own turns."""
    store = await _store_or_refuse()
    return LocalSkillsOut(skills=await list_local_skills(store, principal.oid))


async def read_skill(name: str, principal: CurrentUser) -> LocalSkillOut:
    """One of this chemist's own skills, verbatim — the body a turn is actually given."""
    store = await _store_or_refuse()
    body = await read_local_skill(store, principal.oid, name)
    if body is None:
        raise HTTPException(404, f"you have no personal skill named {name!r}")
    return LocalSkillOut(name=name, body=body)


async def save_skill(payload: LocalSkillIn, principal: CurrentUser) -> LocalSkillOut:
    """Keep one skill for this chemist, replacing any earlier version of that name.

    Replacing rather than versioning: a skill is judgment its owner is asserting *now*, and a tier
    that accumulated drafts would make "what is acting on my turns" a question with a list for an
    answer. The earlier body is not recoverable from here, which is why the response echoes what
    was stored.
    """
    store = await _store_or_refuse()
    name = _validated_name(payload.body)
    await save_local_skill(store, principal.oid, name, payload.body)
    return LocalSkillOut(name=name, body=payload.body)


async def forget_skill(name: str, principal: CurrentUser) -> LocalSkillsOut:
    """Remove one of this chemist's own skills, and answer with what is left.

    The remainder rather than a bare 204, because the question behind a delete is "what is acting
    on me now" and answering it costs one query the caller would otherwise make.
    """
    store = await _store_or_refuse()
    if not await delete_local_skill(store, principal.oid, name):
        raise HTTPException(404, f"you have no personal skill named {name!r}")
    return LocalSkillsOut(skills=await list_local_skills(store, principal.oid))


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`, for the
    reason every `register` in this package gives.
    """
    app.get("/skills/mine", response_model=LocalSkillsOut)(list_skills)
    app.post("/skills/mine", response_model=LocalSkillOut)(save_skill)
    app.get("/skills/mine/{name}", response_model=LocalSkillOut)(read_skill)
    app.delete("/skills/mine/{name}", response_model=LocalSkillsOut)(forget_skill)
