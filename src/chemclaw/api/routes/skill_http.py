"""How the three skill-writing surfaces answer over HTTP — one translation, not four copies.

`skills.py` (a chemist's own tier), `org_skills.py` (the organisation's) and `proposals.py`
(accepting a proposed skill) each write through the tiers' own admission rules
(`agent/local_skills.validated_skill`), and each has to turn the same two outcomes into a status:
a `SkillRefused` and a deployment with no store. Those translations were written four and three
times with identical bodies, differing only in the 503's words — and a surface whose status code
drifted from its neighbours' would give one refusal two meanings. Holds no route, like `caching.py`.
"""

from fastapi import HTTPException

from chemclaw.agent.local_skills import SkillRefused


def skill_refusal_http(error: SkillRefused) -> HTTPException:
    """One refusal as a status code — 409 for a taken name or a full tier, 422 for a bad document.

    The admission rules live with the tier rather than here, because a rule stated at one surface
    is a rule the *other* surface does not have: measured, a body `POST /skills/mine` refused with
    a 422 was written whole by `POST /proposals/skill/{name}`. What is left here is the
    translation, which is genuinely this layer's.
    """
    return HTTPException(409 if error.conflict else 422, str(error))


def store_or_503(store: object | None, message: str) -> object:
    """`store`, or a 503 carrying `message` — the tier is unavailable, which is not the tier empty.

    The distinction is the whole point of raising: with no store a listing would answer `[]` and a
    save would appear to succeed and vanish, so the surface would read "you have no skills" when
    the truth is "this deployment cannot keep any". A confident empty answer about a mechanism that
    is not running is the failure `Chemclaw3_ui`'s review queue has had to delete twice.

    It takes the store rather than fetching it so each route keeps its own `turn_store` binding —
    the seam the route tests patch — and passes its own words, since what "unavailable" costs the
    caller differs per surface.
    """
    if store is None:
        raise HTTPException(503, message)
    return store
