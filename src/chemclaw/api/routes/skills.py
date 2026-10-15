"""A chemist's own skills over HTTP: the only way one is written, listed or removed.

A route, never a tool, so a model cannot change its own behaviour
(`skills.backends.SkillsReadOnlyRefusal`). Read and delete let a chemist inspect and withdraw
what acts on their turns. Owner-scoped via a namespace derived from `principal.oid`; available
exactly when the memory store is.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from chemclaw.api.deps import CurrentUser
from chemclaw.api.routes.skill_http import (
    handle_write_refusals,
    skill_refusal_http,
    store_or_503,
)
from chemclaw.api.runner import turn_store
from chemclaw.skills.access import Tier, Writer
from chemclaw.skills.manifest import SkillRefused, validated_skill
from chemclaw.skills.store import SkillStore


class LocalSkillIn(BaseModel):
    """One skill a chemist is asking to keep, as the whole `SKILL.md` they are asserting.

    The body arrives entire — frontmatter included — rather than as fields this route assembles,
    so what a person approves is byte-for-byte what a later turn reads. A route that built the
    frontmatter would be a second author of the document, and the thing being gated here is
    precisely the document.

    **The length bound is checked in the handler rather than declared here**, because it is
    `agent_local_skill_max_chars` and a `Field(max_length=…)` would bind whatever the setting read
    at *import*. A deployment that raises the cap would then be refused by a model built before it
    was read, which is the class of defect where a setting exists and does nothing.
    """

    body: str = Field(min_length=1)


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


async def _skills_of(principal: CurrentUser) -> SkillStore:
    """The caller's skill store, or a 503 saying the tier is unavailable rather than empty."""
    store = store_or_503(
        await turn_store(),
        "this deployment keeps no personal skills: it needs the durable memory store "
        "(CHEMCLAW_AGENT_MEMORY_ENABLED with a Postgres session store)",
    )
    return SkillStore(store, actor=principal.oid)


async def list_skills(principal: CurrentUser) -> LocalSkillsOut:
    """Every skill acting on this chemist's own turns."""
    skills = await _skills_of(principal)
    return LocalSkillsOut(skills=await skills.names(Tier.MINE))


async def read_skill(name: str, principal: CurrentUser) -> LocalSkillOut:
    """One of this chemist's own skills, verbatim — the body a turn is actually given."""
    skills = await _skills_of(principal)
    body = await skills.read(Tier.MINE, name)
    if body is None:
        raise HTTPException(404, f"you have no personal skill named {name!r}")
    return LocalSkillOut(name=name, body=body)


async def save_skill(body: LocalSkillIn, principal: CurrentUser) -> LocalSkillOut:
    """Keep one skill for this chemist, replacing any earlier version of that name.

    Replaced, not versioned, so "what acts on my turns" has one answer; the response echoes what was
    stored.
    """
    skills = await _skills_of(principal)
    try:
        name = validated_skill(body.body)
    except SkillRefused as refusal:
        raise skill_refusal_http(refusal) from refusal
    # The cap is the writer's: it is counted and spent under one lock.
    try:
        await skills.save(Tier.MINE, name, body.body, writer=Writer(actor=principal.oid))
    except SkillRefused as refusal:
        raise skill_refusal_http(refusal) from refusal
    return LocalSkillOut(name=name, body=body.body)


async def forget_skill(name: str, principal: CurrentUser) -> LocalSkillsOut:
    """Remove one of this chemist's own skills, and answer with what is left."""
    skills = await _skills_of(principal)
    if not await skills.remove(Tier.MINE, name, writer=Writer(actor=principal.oid)):
        raise HTTPException(404, f"you have no personal skill named {name!r}")
    return LocalSkillsOut(skills=await skills.names(Tier.MINE))


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered on the app rather than via the lazy `include_router`.
    """
    handle_write_refusals(app)
    app.get("/skills/mine", response_model=LocalSkillsOut)(list_skills)
    app.post("/skills/mine", response_model=LocalSkillOut)(save_skill)
    app.get("/skills/mine/{name}", response_model=LocalSkillOut)(read_skill)
    app.delete("/skills/mine/{name}", response_model=LocalSkillsOut)(forget_skill)
