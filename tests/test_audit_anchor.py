"""The one alteration the audit chain cannot see, and why a backup story forced the issue.

`infra/sql/011` chains every audited row to its predecessor, so modification, reordering, interior
deletion and prefix truncation all break a link. Deleting a *trailing* run does not: the survivors
chain cleanly and nothing recorded how many rows there should have been. `verify_audit_chain.py`
carried that as a "Known limit" paragraph and `DEFERRED.md` held the fix pending a regulated
deployment asking for provable tail completeness.

The readiness review changed which question it is. **A point-in-time restore is a trailing
deletion.** The system has four unowned stores and no documented recovery, and the moment a recovery
procedure is written down, using it silently shortens the compliance trail in the exact way the
chain was built not to notice. The anchor is what makes the backup story safe to have, so it stopped
being a thing to build when an auditor asks.

These tests drive the pure halves — signing, parsing, comparison — offline. The database halves
(`take_anchor`, `latest_anchor`) are covered by the Postgres-backed suite; what can be wrong here is
the *reasoning*, and none of it needs a server.
"""

import pytest
from pydantic import SecretStr

from chemclaw.agent.audit_anchor import (
    ANCHOR_LOG_MARKER,
    Anchor,
    compare,
    parse_anchor,
    sign,
    signature_ok,
)

_SECRET = "anchor-secret"


@pytest.fixture(autouse=True)
def _with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anchoring is off without a secret, so every test here configures one."""
    monkeypatch.setattr("chemclaw.core.config.settings.audit_anchor_secret", SecretStr(_SECRET))


def _anchor(**overrides: object) -> Anchor:
    """A signed anchor over a trail of 100 rows, with fields overridable per test."""
    base = Anchor(
        taken_at="2026-08-01 00:00:00+00:00",
        row_count=100,
        max_event_id=100,
        tip_hash="tip-100",
        chain_version=2,
    )
    anchor = base.model_copy(update=overrides)
    return anchor.model_copy(update={"signature": sign(anchor)})


def test_a_shortened_trail_is_caught() -> None:
    """The finding: 100 anchored rows, 90 present, and every existing check passes.

    The chain is intact — the 90 survivors link perfectly, which is exactly why this needed a
    number recorded out of band rather than a cleverer walk of the rows that remain.
    """
    problems = compare(_anchor(), row_count=90, max_event_id=90, tip_hash="tip-90")
    assert problems
    assert "10 record(s) are missing from the tail" in problems[0]


def test_a_growing_trail_is_not_a_problem() -> None:
    """The anchor is a high-water mark. Appending is what the trail is for."""
    assert compare(_anchor(), row_count=140, max_event_id=140, tip_hash="tip-140") == []


def test_deleting_old_rows_while_appending_new_ones_is_caught_by_the_id() -> None:
    """Why the anchor records a max id as well as a count.

    Delete ten rows and append ten, and the count is back where it started — a count-only anchor
    reports an intact trail. The highest id cannot go backwards while rows are being appended, so
    it is the number that still disagrees.
    """
    problems = compare(_anchor(max_event_id=120), row_count=100, max_event_id=100, tip_hash="t")
    assert problems and "lost its most recent rows" in problems[0]


def test_a_rebuilt_trail_of_the_right_length_is_caught_by_the_tip() -> None:
    """Why the anchor records the tip hash and not only two integers.

    A trail replaced wholesale with a fabricated one of the same height satisfies both counters.
    The tip is the content check, and it is the only one of the three that can see this.
    """
    problems = compare(_anchor(), row_count=100, max_event_id=100, tip_hash="fabricated")
    assert problems and "not the anchored content" in problems[0]


def test_an_intact_trail_reports_nothing() -> None:
    """The passing case, asserted as explicitly as the failing ones."""
    assert compare(_anchor(), row_count=100, max_event_id=100, tip_hash="tip-100") == []


def test_a_forged_anchor_does_not_verify() -> None:
    """Signed, because an actor who can delete rows can also insert a lower anchor.

    An unsigned high-water mark defends against accidents and nothing else — the attacker with
    write access to `audit_events` has write access to `audit_anchors` too. What they do not have
    is `CHEMCLAW_AUDIT_ANCHOR_SECRET`, which is why it is a setting and not a generated value.
    """
    honest = _anchor()
    forged = honest.model_copy(update={"row_count": 10})
    assert signature_ok(honest)
    assert not signature_ok(forged), "an anchor's numbers can be changed without breaking its seal"


def test_an_unsigned_anchor_is_not_evidence() -> None:
    """An empty signature must fail closed, not pass as "nothing to check"."""
    assert not signature_ok(_anchor().model_copy(update={"signature": ""}))


def test_no_secret_means_no_verification_rather_than_silent_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With anchoring off, an anchor is not evidence — and must not be treated as if it were."""
    anchor = _anchor()
    monkeypatch.setattr("chemclaw.core.config.settings.audit_anchor_secret", SecretStr(""))
    assert not signature_ok(anchor)


def test_an_anchor_signed_under_the_empty_key_is_not_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty secret is a key everybody knows, so it cannot be allowed to act as one.

    Found by a mutation: deleting the `if not settings.audit_anchor_secret` guard left every other
    test here passing, because an anchor signed under a *real* key still fails to verify under an
    empty one. The case it opens is the other direction — with anchoring off, an attacker computes
    the HMAC under `b""` themselves and presents a perfectly self-consistent anchor. The comparison
    would accept it, and a deployment that had deliberately not enabled this control would start
    trusting anchors supplied by whoever wrote them.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.audit_anchor_secret", SecretStr(""))
    unsecured = Anchor(taken_at="2026-08-01 00:00:00+00:00", row_count=0, max_event_id=0)
    forged = unsecured.model_copy(update={"signature": sign(unsecured)})
    assert not signature_ok(forged), (
        "an anchor sealed with the empty key verified — anchoring being switched off must mean "
        "'no anchor is evidence', not 'any anchor is'"
    )


def test_the_signature_covers_the_time_it_was_taken() -> None:
    """An anchor that can be back-dated is one an attacker can present as the current baseline."""
    honest = _anchor()
    moved = honest.model_copy(update={"taken_at": "2020-01-01 00:00:00+00:00"})
    assert not signature_ok(moved)


def test_an_anchor_is_recoverable_from_a_whole_log_line() -> None:
    """The out-of-band copy is a log line, so the recovery path has to accept one.

    This is the form that matters after a restore: the copy in `audit_anchors` was rolled back with
    everything else, so the operator's evidence is whatever their log store kept. Asking them to
    extract exactly the JSON substring — under pressure, during a recovery — is a way to be handed
    a truncated anchor that verifies nothing.
    """
    anchor = _anchor()
    line = (
        "2026-08-01 INFO chemclaw.agent.audit_anchor: "
        f"{ANCHOR_LOG_MARKER}={anchor.model_dump_json()}"
    )
    recovered = parse_anchor(line)
    assert recovered == anchor
    assert signature_ok(recovered)


def test_a_line_with_no_anchor_in_it_raises_rather_than_verifying_everything() -> None:
    """An empty anchor would compare a trail against zero rows and pass — silently, forever."""
    with pytest.raises(ValueError, match="no JSON object"):
        parse_anchor("audit_chain_anchor= <the log rotated>")


def test_the_verifier_holds_the_trail_to_an_anchor_when_given_one() -> None:
    """The wiring, which is the claim: a clean chain plus a short trail is still a failure.

    Asserted through `ChainCheck` and `compare` together rather than a live walk, because the walk
    needs Postgres and what can actually be wrong offline is whether the counters the fold keeps are
    the ones the anchor is compared against.
    """
    from chemclaw.agent.audit import AuditEvent
    from chemclaw.agent.audit_store import chain_hash
    from chemclaw.durable.audit_chain import ChainCheck, ChainRow

    rows: list[ChainRow] = []
    previous = ""
    for index in range(1, 4):
        event = AuditEvent(
            correlation_id=f"c{index}",
            session_id="s",
            purpose="p",
            actor="a",
            tool="t",
            arguments="{}",
            outcome="ok",
            detail="",
            latency_ms=1.0,
            revision="r",
        )
        row_hash = chain_hash(previous, event)
        rows.append(ChainRow(id=index, prev_hash=previous, row_hash=row_hash, event=event))
        previous = row_hash

    check = ChainCheck()
    check.feed(rows)
    assert check.problems == [], "the synthetic chain is intact; the anchor is the only question"
    assert (check.rows_seen, check.last_id, check.tip_hash) == (3, 3, previous)

    intact = _anchor(row_count=3, max_event_id=3, tip_hash=previous)
    assert compare(intact, row_count=3, max_event_id=3, tip_hash=previous) == []
    assert compare(
        _anchor(row_count=5, max_event_id=5, tip_hash="later"),
        row_count=check.rows_seen,
        max_event_id=check.last_id,
        tip_hash=check.tip_hash,
    )
