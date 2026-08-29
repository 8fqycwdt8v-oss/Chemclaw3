"""The evidence pack's session-ownership gate, which shipped with no test at all.

`assemble_evidence_pack` takes a `session_id` argument and returns one conversation's whole record —
every tool call with its actor, every job with its free-text rationale, every approval and every
external effect. Before the gate it read *any* session, while its own docstring claimed the check
lived on a FastAPI dependency that is not on this path. The gate closed that.

**And then shipped unexercised**, which is how it would quietly re-open: a later "an unknown session
should just return an empty pack" refactor makes `_may_read` return True on a missing row, the hole
is back, and `make lint type test` stays green. So this file drives the predicate directly rather
than asserting that a string appears in a dict.
"""

import asyncio

import pytest

from chemclaw.agent.evidence_tools import _may_read, assemble_evidence_pack
from chemclaw.agent.session_store import SessionOwnerStore, owner_permits
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from tests.pg import migrated_db_or_skip

OWNER = "u-owner-evidence"
INTRUDER = "u-intruder-evidence"
SESSION = "sess-evidence-scoping"


def test_the_ownership_rule_is_the_one_the_routes_resolve() -> None:
    """`owner_permits` is shared, so the tool and `/sessions/{id}` cannot disagree.

    Asserted here as well as through the routes because a second copy of an authorization predicate
    is how one surface ends up stricter than the other, and the loose one is the one that matters.
    """
    assert owner_permits(OWNER, OWNER) is True
    assert owner_permits(OWNER, INTRUDER) is False
    assert owner_permits(OWNER, "") is False
    assert owner_permits(OWNER, None) is False


def test_an_owner_less_row_follows_the_enforcement_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open in dev, closed under Entra — the split every other gate here makes.

    Enforcement never mints an owner-less row, so one surviving into it is a leftover from a
    dev-mode write and belongs to nobody rather than to everybody.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    assert owner_permits("", INTRUDER) is True
    assert owner_permits(None, INTRUDER) is True
    monkeypatch.setattr(settings, "entra_required", True)
    assert owner_permits("", INTRUDER) is False
    assert owner_permits(None, INTRUDER) is False


def test_a_session_somebody_else_owns_is_refused_and_does_not_confirm_it_exists() -> None:
    """The gate, driven end to end against a real ownership row.

    The refusal deliberately uses the wording an *unknown* session gets: telling a caller that a
    session exists but belongs to somebody else confirms the id, which is the leak the front door's
    shared 404 rule exists to prevent — and the ids are discoverable, which is what made this
    reachable in the first place.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await SessionOwnerStore().record(SESSION, OWNER)

        identity = set_current_identity(INTRUDER, frozenset())
        session = set_current_session_id("sess-intruders-own")
        try:
            assert await _may_read(SESSION) is False
            answer = await assemble_evidence_pack(SESSION)
            assert answer["empty"] is True
            assert SESSION in str(answer["reason"])
            # The refusal must not carry any of the record it refused.
            assert "tool_calls" not in answer and "jobs" not in answer
        finally:
            reset_current_session_id(session)
            reset_current_identity(identity)

        # And the owner still reaches their own session.
        identity = set_current_identity(OWNER, frozenset())
        session = set_current_session_id(SESSION)
        try:
            assert await _may_read(SESSION) is True
        finally:
            reset_current_session_id(session)
            reset_current_identity(identity)

    asyncio.run(_run())
