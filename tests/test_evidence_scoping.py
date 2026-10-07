"""The evidence pack's session-ownership gate.

`assemble_evidence_pack` returns one conversation's whole record, so it must refuse a session the
caller cannot read. The predicate is driven directly, so a refactor that made an unknown session
readable would turn this red.
"""

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


async def test_a_session_somebody_else_owns_is_refused_and_does_not_confirm_it_exists() -> None:
    """A session somebody else owns is refused, without confirming it exists.

    The refusal uses the wording an unknown session gets, matching the front door's shared-404 rule.
    """
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


async def test_a_member_of_a_shared_session_reads_its_pack_and_a_stranger_still_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member of a shared session reads its pack, and a stranger still does not.

    The tool admits whom the session routes admit.
    """
    from chemclaw.agent import session_members

    await migrated_db_or_skip()
    monkeypatch.setattr(settings, "session_store", "postgres")
    session_members.session_member_store.cache_clear()
    shared = "sess-evidence-shared"
    member = "u-member-evidence"
    try:
        await SessionOwnerStore().record(shared, OWNER)
        await session_members.session_member_store().add(shared, member)
        for actor, expected in ((member, True), (INTRUDER, False)):
            identity = set_current_identity(actor, frozenset())
            try:
                assert await _may_read(shared) is expected, actor
            finally:
                reset_current_identity(identity)
    finally:
        session_members.session_member_store.cache_clear()
