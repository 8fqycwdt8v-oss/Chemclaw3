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
_REGISTRY = Path(__file__).resolve().parents[1] / "ADR-REGISTRY.md"
_HEADING = re.compile(r"^## (D-\d+)", re.MULTILINE)
_REGISTRY_ROW = re.compile(r"^\| (D-\d+) \|", re.MULTILINE)


def _adr_ids() -> list[str]:
    """Every ADR id in the log, in file order."""
    return _HEADING.findall(_DECISIONS.read_text(encoding="utf-8"))


def _registry_ids() -> list[str]:
    """Every ADR id in the allocation ledger, in file order."""
    return _REGISTRY_ROW.findall(_REGISTRY.read_text(encoding="utf-8"))


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


def test_the_registry_lists_exactly_the_decisions_in_the_log() -> None:
    """`ADR-REGISTRY.md` and `DECISIONS.md` name the same ADRs, in the same order.

    The registry exists so that "which numbers are taken?" is one grep against `origin/main`
    instead of a scan of a several-thousand-line log — which is what makes it usable for
    allocating a number *before* writing the ADR. That only holds while the two agree. A ledger
    that has silently drifted is worse than no ledger: it is consulted, believed, and hands out a
    number somebody already used — the exact failure it was added to prevent (D-109).

    So the sync is machine-checked rather than left to whoever remembers. Adding an ADR means
    adding both lines; this test is the reminder.
    """
    log, registry = _adr_ids(), _registry_ids()
    missing = [adr for adr in log if adr not in set(registry)]
    extra = [adr for adr in registry if adr not in set(log)]
    assert not missing, f"in DECISIONS.md but not reserved in ADR-REGISTRY.md: {missing}"
    assert not extra, f"reserved in ADR-REGISTRY.md but no such ADR in DECISIONS.md: {extra}"
    assert log == registry, (
        "ADR-REGISTRY.md lists the same ids as DECISIONS.md but in a different order; "
        "the ledger is ascending and append-only, mirroring the log"
    )


def test_the_registry_has_no_duplicate_reservations() -> None:
    """Two branches reserving the same number is exactly the collision this ledger is for.

    Caught here as a one-line conflict rather than after a merge has buried it in an ADR's prose.
    """
    duplicates = sorted(adr for adr, count in Counter(_registry_ids()).items() if count > 1)
    assert not duplicates, f"ADR-REGISTRY.md reserves the same number twice: {duplicates}"


def test_neither_ledger_carries_an_unresolved_conflict_marker() -> None:
    """A `<<<<<<<` left in the ledger is invisible to every other check here, and was.

    The id checks parse `| D-NNN |` rows and `## D-NNN` headings, so three marker lines sat in
    `ADR-REGISTRY.md` on `main` while every assertion above passed: the rows on both sides of the
    conflict were kept, the ids were fine, and nothing looked at the lines between them. The
    registry's whole purpose is that "which numbers are taken?" is one grep a human trusts, so a
    file that still shows a half-finished merge undermines the mechanism rather than the data.
    """
    for path in (_DECISIONS, _REGISTRY):
        offenders = [
            f"{path.name}:{number}"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "======="
        ]
        assert not offenders, f"unresolved merge conflict markers: {offenders}"
