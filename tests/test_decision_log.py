"""ADR ids must actually identify a decision, and the ledger must match the files (D-088, D-147).

Two branches building in parallel each appended ADRs to the end of one `DECISIONS.md` and each
allocated the *next* free number as seen from its own base — so both wrote D-074, D-075, D-076,
D-081 and D-082, and the collision survived two merges because nothing looked. A duplicate id in an
append-only record is not cosmetic: `docs/planning/BACKLOG.md`, `docs/planning/DEFERRED.md`, the
design docs and several modules cite ADRs by number, and a citation that resolves to two different
decisions is worse than a dangling one — it reads as authoritative while pointing at the wrong
rationale.

D-147 split the log into one file per ADR, which turns that particular collision into an add/add
conflict on a filename rather than something to be noticed in prose. These checks are what is left
that a machine can still catch: an id that names two files, a file whose name and heading disagree,
and a ledger that has drifted from the files beside it.

Deliberately about *identity*, not formatting: the prose style of an ADR is a review matter.
"""

import re
from collections import Counter
from pathlib import Path

_DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
_INDEX = _DECISIONS / "README.md"
# Two id shapes, and the second is the one new ADRs use.
#
# `D-NNN` is the original sequence. It is **frozen**: 167 ADRs and ~971 citations across ~475 files
# carry those numbers, and a merged ADR can never collide, so nothing is gained by renaming them.
#
# `D-YYYY-MM-DD-<slug>` is what an author writes now. The whole stem is the id — not the date —
# because two ADRs on one day is routine here, and an id that identifies two decisions is the exact
# thing this file exists to prevent. Collision then requires the same date *and* the same slug, and
# even that surfaces as an add/add conflict on a filename rather than as a silent duplicate.
#
# The change is not a preference. `D-147` split one `DECISIONS.md` into one file per ADR to make a
# collision loud, and it worked — but "highest on origin/main, plus one" is a read that is stale the
# instant another session pushes, and this repository runs many sessions at once. In a single day
# one branch renumbered three ADRs twice and another renumbered three times; five collisions, all
# on unallocated numbers. A date and a slug are knowable without consulting anything.
_NUMBERED = r"D-\d{3}"
_DATED = r"D-\d{4}-\d{2}-\d{2}-[a-z0-9-]+"
_FILENAME = re.compile(rf"^(?:{_NUMBERED}-[a-z0-9-]+|{_DATED})$")
_HEADING = re.compile(rf"^# ({_NUMBERED}|{_DATED}) — ", re.MULTILINE)
# The id cell tolerates a bare id (a legacy reservation) and a `[id](file.md)` link (written up).
_INDEX_ROW = re.compile(
    rf"^\| \[?({_NUMBERED}|{_DATED})\]?(?:\([^)]*\))? \| ([^|]*)\|", re.MULTILINE
)

# A ledger row for a number claimed but not yet written up. **Legacy**, kept because sessions had
# reservations in flight when the dated form landed and a convention change must not strand them.
# A dated id needs no reservation: it cannot be taken by anyone else, so there is nothing to claim.
#
# It mattered while it lasted: `CLAUDE.md` told an author to reserve in their *first* commit, which
# necessarily means the row exists before the ADR does, and without a marker for that state this
# test rejected the very convention the repo documented — `1f1f233` reserved D-124…D-129 as
# instructed and `8f6a319` had to delete five of them to get CI green.
_RESERVED = "RESERVED"


def _adr_id(path: Path) -> str:
    """The id a file carries: `D-NNN` for the numbered form, the whole stem for the dated one."""
    return path.stem[:5] if re.fullmatch(rf"{_NUMBERED}-.*", path.stem) else path.stem


def _sort_key(path: Path) -> tuple[int, int, str]:
    """Numbered ADRs first in numeric order, then dated ones chronologically.

    Two orderings in one sequence, because `int(stem[2:5])` cannot read a date and lexicographic
    order cannot read `D-9` against `D-10`. The ledger is asserted to match this exactly, so the
    key is the definition of "the record's order" rather than a display preference.
    """
    stem = path.stem
    if re.fullmatch(rf"{_NUMBERED}-.*", stem):
        return (0, int(stem[2:5]), "")
    return (1, 0, stem)


def _adr_files() -> list[Path]:
    """Every ADR file in record order — the record itself."""
    return sorted(_DECISIONS.glob("D-*.md"), key=_sort_key)


def _index_rows() -> list[tuple[str, str]]:
    """Every `(id, title)` in the allocation ledger, in file order."""
    return [(adr, title.strip()) for adr, title in _INDEX_ROW.findall(_INDEX.read_text("utf-8"))]


def _adr_ids() -> list[str]:
    """Every ADR id that has a file, in record order."""
    return [_adr_id(path) for path in _adr_files()]


def _index_ids() -> list[str]:
    """Every id in the ledger, reserved or written, in file order."""
    return [adr for adr, _ in _index_rows()]


def _written_ids() -> list[str]:
    """Ledger ids whose ADR is claimed to exist — every row that is not a reservation."""
    return [adr for adr, title in _index_rows() if not title.startswith(_RESERVED)]


def _reserved_ids() -> list[str]:
    """Ledger ids claimed by a branch whose ADR is not written yet."""
    return [adr for adr, title in _index_rows() if title.startswith(_RESERVED)]


def test_every_adr_id_is_unique() -> None:
    """No ADR number names two decisions — the invariant a parallel merge silently breaks.

    Since D-147 the filesystem enforces the common case (two files cannot share a name), but two
    *differently slugged* files can still carry the same number, which is exactly what two branches
    both writing D-074 would produce.
    """
    duplicates = sorted(adr for adr, count in Counter(_adr_ids()).items() if count > 1)
    assert not duplicates, f"docs/decisions/ reuses ADR ids: {duplicates}"


def test_every_filename_matches_its_heading() -> None:
    """The id in the filename is the id in the document — or a citation resolves to the wrong ADR.

    The filename is what the ledger links to and what a `git grep` for an ADR finds; the heading is
    what a reader sees. A file renamed without its heading is a mismatch nothing else here catches.
    """
    for path in _adr_files():
        assert _FILENAME.match(path.stem), (
            f"{path.name}: expected `D-NNN-lowercase-slug.md`; the ledger's links and every "
            "`git grep` for an ADR rely on that shape"
        )
        headings = _HEADING.findall(path.read_text("utf-8"))
        assert headings, f"{path.name} has no `# D-NNN — Title` heading"
        assert len(headings) == 1, f"{path.name} carries more than one ADR heading: {headings}"
        assert headings[0] == _adr_id(path), (
            f"{path.name} is titled {headings[0]}; filename and heading must name one decision"
        )


def test_the_index_lists_exactly_the_decisions_on_disk() -> None:
    """`docs/decisions/README.md` and the files beside it name the same ADRs, in the same order.

    The index exists so that "which numbers are taken?" is one listing against `origin/main` instead
    of a scan of the whole record — which is what makes it usable for allocating a number *before*
    writing the ADR. That only holds while the two agree. A ledger that has silently drifted is
    worse than no ledger: it is consulted, believed, and hands out a number somebody already used —
    the exact failure it was added to prevent (D-109).

    **Rows marked `RESERVED` are excluded**, because they are the one legitimate way for the two to
    differ: a number claimed on a branch whose ADR is not written yet. Requiring exact equality made
    the documented convention impossible to follow — see `_RESERVED`.
    """
    on_disk, written = _adr_ids(), _written_ids()
    missing = [adr for adr in on_disk if adr not in set(written)]
    extra = [adr for adr in written if adr not in set(on_disk)]
    assert not missing, f"in docs/decisions/ but not listed in its README.md: {missing}"
    assert not extra, (
        f"listed in docs/decisions/README.md but no such file: {extra}. If the ADR is still to be "
        f"written, mark the row '{_RESERVED} — …' so the number stays claimed."
    )
    assert on_disk == written, (
        "docs/decisions/README.md lists the same ids as the files beside it but in a different "
        "order; the ledger is ascending, mirroring the record"
    )


def test_every_written_row_links_to_its_file() -> None:
    """A ledger row is only useful if it reaches the ADR — the index is how a reader navigates."""
    index = _INDEX.read_text("utf-8")
    names = {_adr_id(path): path.name for path in _adr_files()}
    unlinked = [
        adr for adr in _written_ids() if adr in names and f"[{adr}]({names[adr]})" not in index
    ]
    assert not unlinked, (
        f"listed in docs/decisions/README.md without a link to their file: {unlinked}"
    )


def test_a_reserved_number_has_no_adr_yet() -> None:
    """Once the ADR is written the marker comes off, or the ledger stops meaning anything.

    A row left marked `RESERVED` after its ADR has merged would read as an unclaimed number to the
    next author enumerating against `origin/main` — handing out a number already in use, which is
    the collision the ledger exists to prevent.
    """
    written_up = sorted(set(_reserved_ids()) & set(_adr_ids()))
    assert not written_up, (
        f"still marked {_RESERVED} in docs/decisions/README.md but written: {written_up}; "
        "replace the marker with the ADR's title and a link to its file"
    )


def test_the_index_has_no_duplicate_reservations() -> None:
    """Two branches reserving the same number is exactly the collision this ledger is for.

    Caught here as a one-line conflict rather than after a merge has buried it in an ADR's prose.
    """
    duplicates = sorted(adr for adr, count in Counter(_index_ids()).items() if count > 1)
    assert not duplicates, f"docs/decisions/README.md reserves the same number twice: {duplicates}"


def test_nothing_in_the_record_carries_an_unresolved_conflict_marker() -> None:
    """A `<<<<<<<` left in the record is invisible to every other check here, and once was.

    The id checks parse filenames, headings and `| D-NNN |` rows, so three marker lines sat in the
    ledger on `main` while every assertion above passed: the rows on both sides of the conflict were
    kept, the ids were fine, and nothing looked at the lines between them. The ledger's whole
    purpose is that "which numbers are taken?" is one listing a human trusts, so a file that still
    shows a half-finished merge undermines the mechanism rather than the data.
    """
    for path in [_INDEX, *_adr_files()]:
        offenders = [
            f"{path.name}:{number}"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "======="
        ]
        assert not offenders, f"unresolved merge conflict markers: {offenders}"


def test_two_adrs_on_one_day_are_distinct_ids() -> None:
    """The property the dated form exists for, asserted rather than described.

    Two sessions writing an ADR on the same day is routine here — it is what produced five
    collisions in a single day under the numbered scheme. If the id were the *date*, the dated form
    would reproduce exactly the failure it replaces; because the id is the whole stem, same-day ADRs
    are distinct and only an identical slug collides — as an add/add conflict on a filename, which
    git reports loudly.
    """
    first = Path("D-2026-07-31-adr-ids-that-cannot-collide.md")
    second = Path("D-2026-07-31-a-different-decision-entirely.md")
    assert _adr_id(first) != _adr_id(second)
    assert _FILENAME.match(first.stem) and _FILENAME.match(second.stem)


def test_a_dated_id_round_trips_filename_heading_and_ledger() -> None:
    """The three places an id appears must agree for the dated form exactly as for the numbered one.

    `_adr_id`, `_HEADING` and `_INDEX_ROW` are three independent parsers; a scheme change that
    taught one of them the new shape and not the others would leave the drift checks passing
    vacuously on dated ADRs while still policing numbered ones.
    """
    stem = "D-2026-07-31-adr-ids-that-cannot-collide"
    assert _adr_id(Path(f"{stem}.md")) == stem
    assert _HEADING.findall(f"# {stem} — ADR ids that cannot collide\n") == [stem]
    row = f"| [{stem}]({stem}.md) | ADR ids that cannot collide |\n"
    # Compared the way `_index_rows` consumes it — the title cell is stripped there, so asserting
    # its exact whitespace would pin a detail no caller depends on.
    assert [(adr, title.strip()) for adr, title in _INDEX_ROW.findall(row)] == [
        (stem, "ADR ids that cannot collide")
    ]


def test_numbered_ids_sort_before_dated_ones_and_stay_numeric() -> None:
    """Record order is defined here, and the ledger is asserted to match it exactly.

    **`D-900` is the case that makes this test mean anything.** Sorting the stems as plain strings
    gives the right answer for every id in the record today — `D-001` … `D-166` are zero-padded, so
    lexicographic order *is* numeric order, and they all begin `D-0`/`D-1`, which precedes
    `D-2025-…`. So a sort key that ignored the two shapes entirely would pass a test built from
    today's ids while being wrong.

    It is wrong from `D-300` onward: `"D-900-…" > "D-2025-…"` as strings, because `'9' > '2'`. A
    numbered ADR would then sort after the dated ones and the ledger-order assertion would start
    failing years from now, for a reason nobody would connect to this function. The first version of
    this test used `D-009`/`D-010` and did not catch that — it survived flattening the key to a
    single lexicographic tuple.
    """
    paths = [
        Path("D-2026-01-02-later.md"),
        Path("D-900-nine-hundred.md"),
        Path("D-2025-12-31-earlier.md"),
        Path("D-009-nine.md"),
    ]
    assert [p.stem for p in sorted(paths, key=_sort_key)] == [
        "D-009-nine",
        "D-900-nine-hundred",
        "D-2025-12-31-earlier",
        "D-2026-01-02-later",
    ]


def test_a_malformed_id_is_still_rejected() -> None:
    """Widening the shape must not widen it to anything — the filename check is a real gate.

    A scheme change is exactly when a validator quietly becomes permissive, so the shapes that were
    invalid before must still be invalid: an uppercase slug, a missing slug, a two-digit month, a
    bare date with no decision in it.
    """
    for bad in ("D-2026-7-31-slug", "D-2026-07-31", "D-999", "D-2026-07-31-Slug", "D-1234-slug"):
        assert not _FILENAME.match(bad), f"{bad} should not be a valid ADR filename"
