"""`DEFERRED.md` must stay a register of pending work, not a log of past reviews (D-154).

The file drifted for a structural reason, not a careless one. Closing a deferral was recorded by
*appending* — a new dated section explaining that an older section was out of date — so the rows
themselves were never deleted and never corrected. Nine such sections accumulated, three of them
false by the time D-154 read them, and five rows described work that had already shipped: IDEA-1,
IDEA-2 and IDEA-6 all landed in D-085, the commit immediately after the section listing them as
deferred.

A register that is wrong about the tree is worse than no register: it invites re-deferring work
that is done, and it hides the item whose trigger has quietly been met.

These checks are what a machine can catch of that. Each pins a failure mode the file actually had:

- **A struck-through row.** `~~item~~ **Done (D-0NN)**` was how closure was written. It reads as
  half-deleted, it survives forever, and it is the exact shape that let a *stale* row hide beside
  a *closed* one. A closed item leaves; its ADR is the record.
- **A row with no trigger.** "Why not now" without "what would change that" is not a deferral, it
  is either a rejection (which belongs in the declined table, whose column says so) or a wish. The
  pg-boss row carried a literal em dash in its trigger cell for eleven months.
- **A citation to an ADR that does not exist.** The rows lean on `D-NNN` to carry the reasoning
  they no longer restate, so a dangling number silently empties the justification.

Deliberately *not* checked: whether a row's claim is still true. No test can know that — which is
why the convention (delete on close, in the closing commit) is the real fix and this is the guard.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REGISTER = _ROOT / "docs" / "planning" / "DEFERRED.md"
_DECISIONS = _ROOT / "docs" / "decisions"

# A table row: leading pipe, then the cells. Header and separator rows are filtered by shape.
_ROW = re.compile(r"^\|(?![\s:-]+\|$).*\|\s*$", re.MULTILINE)
_ADR = re.compile(r"\bD-(\d{3})\b")
# A ledger row claiming a number whose ADR file is not written yet (see `test_decision_log.py`).
_ADR_RESERVED = re.compile(r"^\| (D-\d{3}) \| RESERVED", re.MULTILINE)


def _rows() -> list[list[str]]:
    """Every data row of every table, as its list of stripped cells."""
    rows = []
    for line in _ROW.findall(_REGISTER.read_text("utf-8")):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0].startswith("**"):  # a data row names an item in bold; headers do not
            rows.append(cells)
    return rows


def test_the_register_has_rows_to_check() -> None:
    """Guard the guard: a parse that silently matched nothing would pass every test below."""
    rows = _rows()
    assert len(rows) > 15
    assert all(len(cells) == 3 for cells in rows), "every row is item | why | trigger"


def test_no_row_is_struck_through() -> None:
    """A closed deferral is deleted, not crossed out — the ADR is the record, git is the history.

    Strikethrough was the file's way of saying "done", and it is why six finished items were still
    being read as pending state a year later.
    """
    struck = [line for line in _REGISTER.read_text("utf-8").splitlines() if "~~" in line]
    assert struck == [], f"delete the closed row instead of striking it: {struck}"


def test_every_row_states_a_trigger() -> None:
    """A "why not now" with no "what would change it" is a wish, not a deferral.

    The declined table answers the same column with an explicit "Nothing", which is a decision;
    an empty cell (or a bare dash) is an absence.
    """
    empty = [cells[0] for cells in _rows() if len(cells[-1].strip(" -—–")) < 3]
    assert empty == [], f"rows with no revisit trigger: {empty}"


def test_every_cited_adr_exists() -> None:
    """The rows delegate their reasoning to `D-NNN`, so a dangling number empties the row.

    A number *reserved* in the ledger counts as known, for the same reason
    `test_decision_log.py` exempts those rows: `CLAUDE.md` has an author claim the number in
    their first commit, so a branch legitimately cites its own ADR before the file exists.
    """
    known = {path.stem[:5] for path in _DECISIONS.glob("D-*.md")}
    known |= set(_ADR_RESERVED.findall((_DECISIONS / "README.md").read_text("utf-8")))
    cited = {f"D-{number}" for number in _ADR.findall(_REGISTER.read_text("utf-8"))}
    assert cited, "the register cites no ADR at all — suspicious"
    assert cited <= known, f"cited but not in docs/decisions/: {sorted(cited - known)}"
