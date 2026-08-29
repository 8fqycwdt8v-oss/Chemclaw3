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

  **That second guard was built twice by position and once, correctly, by phrasing.** A
  header-scoped pattern and a body-scoped pattern each knew shapes the other did not, so a live
  count could pass by standing in the section whose pattern did not recognise its wording — and one
  shape, "221 findings are open", was outside both. What made the header's "237 open rows"
  legitimately exempt was never that it sits in the header; it is that it records what this file
  *once was*. So there is now one pattern over the whole file and an allowlist of quoted
  retrospective sentences, each required to carry a past-tense marker so the exemption cannot
  launder a live number.

Deliberately not checked: whether a row is still true. No test can know that — the same line
`tests/test_deferred_register.py` draws for the sibling register, and for the same reason.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKLOG = _ROOT / "docs" / "planning" / "BACKLOG.md"
# An open row: a checkbox item whose title is bolded, which is the shape the whole file uses.
_ROW_TITLE = re.compile(r"^- \[ \] \*\*(.+?)\*\*", re.MULTILINE)

# The nouns this register and its archive are counted in. Narrow to these three deliberately, so a
# genuine measurement of something else ("124 of 261 probes", "a 39-note knowledge graph", "measured
# at 2,170 tokens") is untouched: those name a corpus, not the queue's own length.
_COUNTED = r"(?:rows|findings|items)"

#: A count of this register's own length, in every phrasing that has actually been written here.
#:
#: **One pattern, applied to the whole file.** It replaces two that were scoped by *position* — one
#: over the header, one over the body — and the pair had a gap in each direction, both reproduced
#: before this was written:
#:
#: - A live, wrong count *in the header* passed both. "38 open rows are still open in this file."
#:   escaped the header pattern (which required the digits to sit next to the word `rows`, and they
#:   sit next to `open`) and the body pattern never looked at the header at all.
#: - Header-shaped phrasings *in the body* passed both. "The archive holds 221." was caught only by
#:   the header pattern, which does not run there — and "221 findings are open." was caught by
#:   **neither**, because one wanted `open` before the noun and the other wanted the digits welded
#:   to `are open`.
#:
#: Position was never what made a sentence retrospective; `_HISTORICAL` below carries that, so the
#: shape is checked everywhere and the exemption is stated rather than inferred from a heading.
_STATED_COUNT = re.compile(
    rf"\b\d[\d,]*\s+(?:of its\s+)?(?:open\s+)?{_COUNTED}\b"
    rf"|\bholds\s+\d[\d,]*\b"
    rf"|\b\d[\d,]*\s+are\s+open\b"
)

#: The sentences that may state a count, quoted exactly, because each describes a *past* state.
#:
#: The register's own argument is that a number nobody re-derives is a claim about its author's
#: afternoon. A measurement of what this file once was is not that claim: it is the evidence the
#: rule rests on, and it cannot go stale, because the past does not move.
#:
#: **An entry here is not a way past the test**, which is why `_RETROSPECTIVE` exists beside it:
#: every entry must carry an explicit past-tense marker, so the allowlist cannot be used to launder
#: a live count by quoting it. And every entry must still appear in the file, so an exemption whose
#: sentence has been rewritten goes red instead of sitting here exempting nothing.
_HISTORICAL: tuple[str, ...] = ("this file reached 4,717 lines and 237 open rows",)

#: What makes a sentence a record of the past rather than a claim about now.
_RETROSPECTIVE = re.compile(r"\b(?:reached|grew to|used to|once held|was|were|had)\b")


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


def test_every_historical_exemption_is_still_quoted_and_still_past_tense() -> None:
    """Guard the allowlist: an exemption is a claim about the file, and claims go stale.

    Two ways `_HISTORICAL` can rot, and both leave the guard below quietly weaker rather than red.
    An entry whose sentence has since been rewritten exempts nothing and hides that it does not; an
    entry worded in the present tense exempts a *live* count, which is the failure the whole file
    exists to catch, committed through the list that was built to make the exception honest.
    """
    text = _BACKLOG.read_text(encoding="utf-8")
    for sentence in _HISTORICAL:
        assert sentence in text, (
            f"the exempted sentence {sentence!r} is no longer in BACKLOG.md. Delete it from "
            "`_HISTORICAL` or quote the sentence that replaced it — an exemption for prose that is "
            "not there reads as a rule and is not one."
        )
        assert _RETROSPECTIVE.search(sentence), (
            f"the exempted sentence {sentence!r} states a count with no past-tense marker, so it "
            "reads as a live claim. `_HISTORICAL` is for measurements of what this file once was; "
            "it is not a place to put a number that has to stay correct."
        )


def test_the_header_still_shows_how_to_derive_the_count() -> None:
    """Removing the number must not remove the way to get it — that would be the other failure."""
    assert "grep -c '^- \\[ \\]'" in _header(), (
        "the header no longer shows the command that counts the rows"
    )


def test_nowhere_in_the_file_states_a_live_row_count() -> None:
    """The register carries the `grep`; it must not also carry the answer, in any section.

    Both numbers the header used to state were wrong — 223 against 221, 41 against 45 — and the
    command that disproves each was printed on the same line. Keeping the command and dropping the
    number is the whole fix, and it is the only one that stays true as rows are added and closed.

    **Checked file-wide, because two position-scoped patterns had a gap in each direction.** The
    first guard was written for "223 of its rows are open" and scoped to the header; the second for
    "223 open findings live in `findings-2026-08.md`" and scoped to the body. Between them a live
    count could stand in the header under a phrasing the header pattern did not know, or in the body
    under a phrasing only the header pattern knew — and "221 findings are open." was outside both.
    A guard aimed at one sentence catches that sentence; the failure is the *shape*, and the shape
    can appear anywhere, which is why the pattern now goes everywhere and the exemption is a quoted
    sentence rather than a heading.
    """
    text = _BACKLOG.read_text(encoding="utf-8")
    # Remove the exempted sentences rather than testing each match's neighbourhood: whatever is left
    # is prose no retrospective narrative accounts for, and the pattern may run over all of it. The
    # test above is what keeps this subtraction honest.
    for sentence in _HISTORICAL:
        text = text.replace(sentence, "")
    stated = _STATED_COUNT.findall(text)
    assert not stated, (
        f"BACKLOG.md states a live count of its own rows: {stated}. Cite the `grep -c` and let it "
        "answer; a number here is stale the next time a row lands or closes "
        "(D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose). If the sentence is genuinely "
        "about a past state, quote it in `_HISTORICAL` — it must carry a past-tense marker."
    )
