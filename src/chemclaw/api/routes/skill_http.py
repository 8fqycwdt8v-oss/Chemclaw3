"""How the three skill-writing surfaces answer over HTTP — one translation, not three copies.

`skills.py`, `org_skills.py` and `proposals.py` all write through the tiers' admission rules and
must map a `SkillRefused` and a missing store to the same status codes. Holds no route.
"""

from fastapi import HTTPException

from chemclaw.agent.local_skills import SkillRefused


def skill_refusal_http(error: SkillRefused) -> HTTPException:
    """One refusal as a status code — 409 for a taken name or a full tier, 422 for a bad document.

    The rules themselves live with the tier, so every surface enforces the same ones.
    """
    return HTTPException(409 if error.conflict else 422, str(error))


def store_or_503(store: object | None, message: str) -> object:
    """`store`, or a 503 carrying `message` — the tier is unavailable, which is not the tier empty.

    Without a store a listing would answer `[]` and a save would vanish. Takes the store as an
    argument so each route keeps its own patchable `turn_store` binding.
    """
    if store is None:
        raise HTTPException(503, message)
    return store
