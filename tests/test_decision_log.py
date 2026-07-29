"""ADR ids must actually identify a decision, and the ledger must match the files (D-088, D-146).

Two branches building in parallel each appended ADRs to the end of one `DECISIONS.md` and each
allocated the *next* free number as seen from its own base — so both wrote D-074, D-075, D-076,
D-081 and D-082, and the collision survived two merges because nothing looked. A duplicate id in an
append-only record is not cosmetic: `docs/planning/BACKLOG.md`, `docs/planning/DEFERRED.md`, the
design docs and several modules cite ADRs by number, and a citation that resolves to two different
decisions is worse than a dangling one — it reads as authoritative while pointing at the wrong
rationale.

D-146 split the log into one file per ADR, which turns that particular collision into an add/add
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
_FILENAME = re.compile(r"^(D-\d+)-[a-z0-9-]+$")
_HEADING = re.compile(r"^# (D-\d+) — ", re.MULTILINE)
# The id cell tolerates a bare `D-NNN` (a reservation) and a `[D-NNN](file.md)` link (written up).
_INDEX_ROW = re.compile(r"^\| \[?(D-\d+)\]?(?:\([^)]*\))? \| ([^|]*)\|", re.MULTILINE)

# A ledger row for a number claimed but not yet written up. `CLAUDE.md` tells an author to reserve
# in the *first* commit on a branch — "a number you have not yet pushed is a number another session
# will take" — which necessarily means the row exists before the ADR does. Without a marker for
# that state this test rejected the very convention the repo documents: `1f1f233` reserved
# D-124…D-129 as instructed and `8f6a319` had to delete five of them to get CI green.
_RESERVED = "RESERVED"


def _adr_files() -> list[Path]:
    """Every ADR file, ascending by number — the record itself."""
    return sorted(_DECISIONS.glob("D-*.md"), key=lambda path: int(path.stem[2:5]))


def _index_rows() -> list[tuple[str, str]]:
    """Every `(id, title)` in the allocation ledger, in file order."""
    return [(adr, title.strip()) for adr, title in _INDEX_ROW.findall(_INDEX.read_text("utf-8"))]


def _adr_ids() -> list[str]:
    """Every ADR id that has a file, ascending."""
    return [path.stem[:5] for path in _adr_files()]


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

    Since D-146 the filesystem enforces the common case (two files cannot share a name), but two
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
        assert headings[0] == path.stem[:5], (
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
    names = {path.stem[:5]: path.name for path in _adr_files()}
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
