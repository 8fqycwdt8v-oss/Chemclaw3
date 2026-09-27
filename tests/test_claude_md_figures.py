"""A figure in `CLAUDE.md` is a claim about a commit, so it has to name the symbol that holds it.

`CLAUDE.md` is loaded into every session as authoritative, and it accumulated measurements the way
any document does: somebody measured something, wrote the number down, and the number moved. Audited
on 2026-09-19, most of its load-bearing figures were wrong — a per-turn spend cap it called "ships
at 0" after two raises had taken it elsewhere, a context ceiling and two compaction defaults from
three rewrites ago, and a helper's tool count whose *base* had moved so that the subtraction the
paragraph existed to make no longer worked. The file already knew: it says in its own words that
"the live number is whatever `tests/test_context_floor.py` measures", and then prints a dozen more
digits on either side of that sentence.

The one section of it that never went stale is the validator list, and it is the one that refuses to
state a count. This test generalises that: a figure large enough to be a measurement must resolve to
the symbol that holds it, or be declared here with the reason it cannot move.

**Scope is deliberately the thousands and above.** A small integer in this file is almost always
prose ("four layers", "§4", "D-005", "three predicates") and a rule that flagged those would be
turned off within a week. Every figure that actually rotted was in this class. Dates and ADR ids are
stripped first, because `D-2026-09-05` is an identifier rather than a measurement.
"""

from __future__ import annotations

import re
from pathlib import Path

_CLAUDE_MD = Path(__file__).resolve().parents[1] / "CLAUDE.md"

#: Identifiers that contain digits but assert nothing about a commit.
_NOT_A_FIGURE = re.compile(r"D-\d{4}-\d{2}-\d{2}|D-\d{3}\b|\b\d{4}-\d{2}-\d{2}\b|\b20\d{2}\b")

#: A figure: **three** digits or more, written plainly or with thousands separators, and not the
#: fractional part of a decimal (`0.007 s` is one number, not a `007`).
#:
#: **The floor was four digits, and the flagship figure of the section this guard was written for
#: sat under it.** `CLAUDE.md` opened by saying the decision record held "680 ADRs" — inside the
#: paragraph that explains why load-bearing figures were removed from that section — and it was
#: wrong the day it was written (683 on disk then, 691 now). A wave-10 audit found it by counting;
#: this guard could not, because `680` is three digits. Measured before lowering the floor: three
#: digits flags exactly the two argued figures below plus the decimal this pattern now excludes,
#: while two digits flags eighteen and would make the allowlist the document.
#:
#: What no digit pattern can reach is a figure **spelled as a word** — the same audit found "Six
#: places read one" against a file holding sixty-one such assertions, and "two of them asserting an
#: absence" against at least eight. That is a review matter, and saying so here is better than
#: implying this check covers it.
_FIGURE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|(?<![\d.])\d{3,}\b")

#: Figures `CLAUDE.md` may state, each with the reason it cannot go stale. A figure that names a
#: measurement does not belong here — name its symbol in the prose instead. This map may not
#: outlive its figures: `test_no_allowance_is_stale` fails on an entry the file no longer contains.
_ALLOWED: dict[str, str] = {
    "157": (
        "a figure quoted *as wrong*: the skip count prose claimed while the suite skipped 216. "
        "Its staleness is the point being made, so it cannot go stale — and the sentence names "
        "`tests/conftest.py`'s epilogue as what holds the live number."
    ),
    "216": (
        "the true skip count when the figure above was measured wrong, quoted for the same "
        "reason and in the same sentence. Neither is a claim about today."
    ),
}


def _figures(text: str) -> list[str]:
    """Every figure in `text`, with identifiers stripped first so an ADR id is not read as one."""
    return _FIGURE.findall(_NOT_A_FIGURE.sub(" ", text))


def test_no_figure_in_claude_md_is_undeclared() -> None:
    """A measurement here is a claim about the commit that wrote it, and nothing re-checks it.

    The fix is never to update the number — that is what has been done, repeatedly, and it is
    stale again by the next merge on somebody else's branch. It is to name the symbol: `CEILINGS`,
    `PREFIX_BOUND`, `agent_max_turn_billed_tokens`, `len(tool_call_middleware(...))`. A reader who
    wants the value reads it from the thing that enforces it.
    """
    found = sorted(set(_figures(_CLAUDE_MD.read_text())) - set(_ALLOWED))
    assert not found, (
        f"CLAUDE.md states {len(found)} figure(s) that resolve to no symbol: {found}. Replace each "
        "with the name of the constant, setting or test that holds it — or, if it genuinely cannot "
        "move, add it to _ALLOWED with the reason."
    )


def test_no_allowance_is_stale() -> None:
    """An exemption may not outlive the figure it exempts.

    The same failure this file is about, one level up: a list of permitted numbers is itself a
    measurement, and the GxP marker in `docs/decisions/README.md` went stale in exactly this way —
    it named 59 files where 64 matched.
    """
    present = set(_figures(_CLAUDE_MD.read_text()))
    orphaned = sorted(figure for figure in _ALLOWED if figure not in present)
    assert not orphaned, (
        f"_ALLOWED exempts figures CLAUDE.md no longer states: {orphaned}. Delete the entries; an "
        "exemption nobody can trip is a claim that a control exists."
    )


def test_the_rule_can_fail() -> None:
    """A check that cannot fail is this repository's most-cited defect class, so drive this one.

    Asserted against a synthetic document rather than by editing `CLAUDE.md`, because a test that
    mutates the file it guards leaves the tree dirty when it fails.
    """
    assert _figures("the ceiling went to 44,500 and the trigger to 74,500") == ["44,500", "74,500"]
    # Both, at the three-digit floor. This line asserted `== ["33310"]` while the floor was four
    # digits, so it pinned the gap: a tool count is exactly the figure this guard exists for, and
    # `680 ADRs` sat in the same blind spot until a wave-10 audit counted the directory by hand.
    assert _figures("it binds 113 tools costing 33310 tokens") == ["113", "33310"]
    # A decimal is one number. Without the `(?<![\d.])` guard, lowering the floor read the
    # fractional part of `0.007 s` — a real figure in CLAUDE.md — as a `007`.
    assert _figures("a 0.007 s repeat and a 0.955 recall") == []
    assert _figures("see D-2026-09-05, written 2026-09-05, which narrows D-005") == []
    # Still not figures: a small count a reader can check at a glance, and a section marker.
    assert _figures("four layers, three predicates, §4, 25 iterations") == []
