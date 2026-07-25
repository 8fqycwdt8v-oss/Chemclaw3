"""`DECISIONS.md` is an append-only ADR log, so its ids must actually identify a decision (D-088).

Two branches building in parallel each appended ADRs and each allocated the *next* free number as
seen from its own base — so both wrote D-074, D-075, D-076, D-081 and D-082, and the collision
survived two merges because nothing looked. A duplicate id in an append-only log is not a cosmetic
problem: `BACKLOG.md`, `DEFERRED.md`, the design docs and several modules cite ADRs by number, and a
citation that resolves to two different decisions is worse than a dangling one — it reads as
authoritative while pointing at the wrong rationale.

This is the check that was missing. It is deliberately about *identity*, not formatting: the log's
prose style is a review matter, but a number that names two things is a defect a machine can catch.
"""

import re
from collections import Counter
from pathlib import Path

_DECISIONS = Path(__file__).resolve().parents[1] / "DECISIONS.md"
_HEADING = re.compile(r"^## (D-\d+)", re.MULTILINE)


def _adr_ids() -> list[str]:
    """Every ADR id in the log, in file order."""
    return _HEADING.findall(_DECISIONS.read_text(encoding="utf-8"))


def test_every_adr_id_is_unique() -> None:
    """No ADR number names two decisions — the invariant a parallel merge silently breaks."""
    duplicates = sorted(adr for adr, count in Counter(_adr_ids()).items() if count > 1)
    assert not duplicates, f"DECISIONS.md reuses ADR ids: {duplicates}"


def test_the_newest_decision_is_the_last_one() -> None:
    """The highest ADR number is the final heading — "decided last" is the tail, not a scan.

    This is the property that makes the duplicate above avoidable at authoring time: an author who
    appends after reading the tail sees the highest number in use and takes the next one. A branch
    whose ADR lands anywhere but the end has numbered against a stale view of the log, which is
    exactly how the D-074…D-082 collision happened.

    Asserted as "the max is last" rather than "every id ascends": D-009 was written before D-008 in
    2026-02 and that inversion is history. Reordering an append-only log to satisfy a test would be
    the very edit the log forbids, and an exemption list for it would only restate the exception.
    """
    ids = [int(adr.removeprefix("D-")) for adr in _adr_ids()]
    assert ids[-1] == max(ids), (
        f"the last ADR is D-{ids[-1]:03d} but the highest is D-{max(ids):03d}; "
        "a new decision belongs at the end with the next free number"
    )
