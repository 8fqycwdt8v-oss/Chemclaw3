"""`BACKLOG.md` must obey the two rules it opens with, which it did not.

The file states them itself: it is *a queue of what is still open, not a log of what was found*, and
a row leaves in the commit that closes it. Both failures this checks for were found by measuring
the file against its own header (X7, 2026-08-27):

- **A row present twice.** "Memory records; it does not change what the next turn does" appeared in
  two sections, identical for sixteen lines, with only one of the copies carrying the 2026-08-25
  measurement that says the row is blocked on a deployment that does not exist. A duplicate does not
  read as a duplicate — it reads as two open items — and it inflates every count taken of the file.
- **A self-count that nobody re-derived.** The header said "223 of its rows are open" beside the
  `grep` that answers 221, and "it holds 41 now" where the same `grep` answers 45. Both were printed
  *next to the command that disproves them*, which is the shape
  `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` exists to reject: a number in prose is
  a claim about its author's afternoon, and one that reads as freshly measured is worse than none.

Deliberately not checked: whether a row is still true. No test can know that — the same line
`tests/test_deferred_register.py` draws for the sibling register, and for the same reason.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKLOG = _ROOT / "docs" / "planning" / "BACKLOG.md"
# An open row: a checkbox item whose title is bolded, which is the shape the whole file uses.
_ROW_TITLE = re.compile(r"^- \[ \] \*\*(.+?)\*\*", re.MULTILINE)
# A count of this file's own rows, or of the archive's, written as prose. "7 of the 30" is not this
# shape: it is a historical measurement of an overlap, naming no unit a `grep` re-derives.
_ROW_COUNT = re.compile(r"\b\d+\s+(?:of its\s+)?rows\b|\bholds\s+\d+\b|\b\d+\s+are open\b")
# The same claim as `_ROW_COUNT`, worded as a count of what a register *holds* rather than of its
# rows — the phrasing that escaped it. Narrow to the three nouns these two files are counted in, so
# a genuine measurement of something else ("124 of 261 probes", "a 39-note knowledge graph") is
# untouched: those name a corpus, not the queue's own length.
_LIVE_COUNT = re.compile(r"\b\d+\s+open\s+(?:findings|rows|items)\b")


def _titles() -> list[str]:
    """Every open row's bolded title, in file order."""
    return _ROW_TITLE.findall(_BACKLOG.read_text(encoding="utf-8"))


def _header() -> str:
    """The file's self-description: everything before the first section heading."""
    return _BACKLOG.read_text(encoding="utf-8").split("\n## ", 1)[0]


def test_the_register_has_rows_to_check() -> None:
    """Guard the guard: a parse matching nothing would pass everything below."""
    assert len(_titles()) > 10, "no open rows parsed from BACKLOG.md; the row shape moved"


def test_no_row_appears_twice() -> None:
    """One item, one row. A second copy reads as a second item and diverges from the first."""
    seen: dict[str, int] = {}
    for title in _titles():
        seen[title] = seen.get(title, 0) + 1
    repeated = sorted(title for title, count in seen.items() if count > 1)
    assert not repeated, (
        f"these rows appear more than once in BACKLOG.md: {repeated}. Keep the copy carrying the "
        "most measurement and delete the other; two copies of one row drift."
    )


def test_the_header_states_no_row_count_it_does_not_derive() -> None:
    """The header carries the `grep`; it must not also carry the answer.

    Both numbers it used to state were wrong, and the command that disproves each was printed on the
    same line. Keeping the command and dropping the number is the whole fix — and it is the only one
    that stays true as rows are added and closed.
    """
    counts = _ROW_COUNT.findall(_header())
    assert not counts, (
        f"BACKLOG.md's header counts its own rows in prose: {counts}. The `grep` beside it is the "
        "count; a number written here is stale the next time a row lands "
        "(D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose)."
    )


def test_the_header_still_shows_how_to_derive_the_count() -> None:
    """Removing the number must not remove the way to get it — that would be the other failure."""
    assert "grep -c '^- \\[ \\]'" in _header(), (
        "the header no longer shows the command that counts the rows"
    )


def test_no_section_below_the_header_states_a_live_row_count() -> None:
    """The same failure, one section lower, where the guard above could not see it.

    The header test was written for "223 of its rows are open" and scoped to the header, so it
    missed **"223 open findings live in `findings-2026-08.md`"** in the *Everything else* section —
    stale by two, printed beside the `grep -c` that answers 221, and phrased differently enough to
    escape the pattern as well as the scope. A guard aimed at one sentence catches that sentence;
    the failure is the *shape*, and the shape can appear anywhere.

    Scoped to the body deliberately. The header's own narrative is retrospective — it says this file
    once "reached 4,717 lines and 237 open rows", which is a measurement of a past state and exactly
    the evidence the rule rests on. A body section has no such reason: a count written beside a
    register that grows and shrinks is a live claim, and it is wrong on the next commit.
    """
    # `split(maxsplit=1)` returns one element when the file has no section heading at all, and
    # taking [-1] there would hand the *header* to a check written to exempt it — a false positive
    # on the very prose the docstring above says must stay. Two elements or no body.
    sections = _BACKLOG.read_text(encoding="utf-8").split("\n## ", 1)
    body = sections[1] if len(sections) == 2 else ""
    stated = _LIVE_COUNT.findall(body)
    assert not stated, (
        f"BACKLOG.md states a live row count below its header: {stated}. Cite the `grep -c` and "
        "let it answer; a number here is stale the next time a row lands or closes "
        "(D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose)."
    )
