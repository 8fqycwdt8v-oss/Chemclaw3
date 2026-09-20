r"""`tasks/lessons.md` is held to the shape its own header prescribes.

**Why this file exists rather than a longer paragraph.** `tasks/lessons.md` says, in its own words,
*"Do not append a dated section — that is how the old file grew"* and *"if a rule is being broken
repeatedly, the fix is a mechanism (a script, a test, a `Makefile` target), not a longer
paragraph"*. The rule being broken repeatedly was that prohibition: the digest that replaced a
1,937-line narrative grew 88 dated sections of its own, to 3,212 lines — 1.66x the length the
restructure existed to escape — with the prohibition sitting unread at the top of every one of the
sessions that appended to it. So the paragraph gets its mechanism.

**The subject is the file's structure, not the spelling of a date.** A guard for `^## \d{4}-` would
have caught only some of those 88 headings even on the day they were written: the majority put the
date in trailing parentheses (`## A line count is not a measure of reading cost (2026-08-17)`), and
seven carried no date at all. What actually distinguishes a rule from an incident is *where the
prose sits*: a rule is a numbered list item, so every line of it is either the item's own first line
or a continuation indented under it, and a narrative section is unindented prose directly under a
heading. That property is derived from the markdown, holds whatever a heading is called, and is what
`test_every_line_under_a_theme_belongs_to_a_numbered_rule` asserts. The date scan is kept beside it
as a second, weaker reading — it names the defect in the vocabulary a reader will recognise — and it
covers a date anywhere in the heading rather than only at its start.

The other half of the contract is with the tree that cites the file. Rule numbers are stable labels
(`tasks/lessons.md` rule 9), so folding or renumbering can silently invalidate a citation in a
docstring that no other gate reads; `test_every_rule_citation_in_the_tree_resolves` resolves each
one against the file.

**The reading-cost ceiling is derived, not chosen.** The file's argument for being short is that a
long one is not read, and the length that proved it is the archive it escaped from. So the bound is
"shorter than the shortest narrative archive on disk" rather than a number typed here, which moves
with the evidence and cannot go stale against it. It is the weakest assertion in this file and is
deliberately not the primary one: a ceiling says nothing about *why* the file grew, and the file
grew by narrative rather than by rules.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Derived from this file's own location rather than from an installed package, because the suite is
#: run from linked worktrees whose `import chemclaw` resolves to the *main* checkout through an
#: editable-install `.pth`. A test that walked the tree from the package would read a different tree
#: from the one holding the `tasks/lessons.md` under review, and would pass while the worktree's copy
#: was broken. This is `tasks/lessons.md` rule 5's "at the gate's own scope" one layer down.
REPO_ROOT = Path(__file__).resolve().parents[1]

LESSONS = REPO_ROOT / "tasks" / "lessons.md"
ARCHIVE_DIR = REPO_ROOT / "docs" / "archive"

#: A heading that names an incident rather than a theme. Every shape the 88 appended sections used:
#: an ISO date at the start, an ISO date in trailing parentheses, a bare year in parentheses, and a
#: month name. The pattern is deliberately looser than any single one of them, because the appended
#: headings varied and the next author's will too.
_INCIDENT_IN_HEADING = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|\(\s*(19|20)\d{2}\b"
    r"|\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",
    re.IGNORECASE,
)

_RULE_START = re.compile(r"^(?P<marker>(?P<number>\d+)\.\s)")

_FOLD_INSTEAD = (
    "Fold the lesson into the rule it belongs under (sharpening that rule, and saying so when it "
    "recurred), and put the narrative in docs/archive/lessons-<YYYY>-<MM>.md. A dated section is "
    "what tasks/lessons.md's own header forbids, twice over: it is how the 1,937-line file grew and "
    "how the 3,212-line one that replaced it grew again."
)


def _lessons_text() -> str:
    assert LESSONS.is_file(), f"{LESSONS} is missing; resolved repository root was {REPO_ROOT}"
    return LESSONS.read_text(encoding="utf-8")


def _sections() -> list[tuple[str, int, list[tuple[int, str]]]]:
    """Every `##` section as (heading, heading line number, [(line number, text), ...])."""
    sections: list[tuple[str, int, list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] | None = None
    for number, line in enumerate(_lessons_text().split("\n"), start=1):
        if line.startswith("## "):
            current = []
            sections.append((line[3:].strip(), number, current))
        elif current is not None:
            current.append((number, line))
    return sections


def test_the_tree_under_test_is_the_one_this_file_lives_in() -> None:
    """The editable-install trap, asserted rather than trusted.

    `.venv/bin/python -m pytest` run from a linked worktree resolves `import chemclaw` to the main
    checkout, so a guard that reached the tree through the package would report on a file the
    worktree does not contain. This states the root out loud, and the other tests in this file reach
    the tree only through `REPO_ROOT`.
    """
    print(f"resolved repository root: {REPO_ROOT}")
    assert (REPO_ROOT / "tasks" / "lessons.md").is_file()
    assert (REPO_ROOT / "CLAUDE.md").is_file()
    assert Path(__file__).resolve().parent == REPO_ROOT / "tests"


def test_every_line_under_a_theme_belongs_to_a_numbered_rule() -> None:
    """The structural property, which is what a dated section actually violates.

    Inside a thematic section the only permitted content is a numbered rule and its continuation
    lines, indented to the width of the rule's own marker (three spaces for `1. `, four for `10. `).
    Narrative prose under a heading is unindented and starts with no number, so it fails here
    whatever the heading is called — which is the point, since a date regex is evaded by moving the
    date, dropping it, or writing the heading as a sentence.
    """
    offenders: list[str] = []
    for heading, heading_line, body in _sections():
        indent: int | None = None
        for number, line in body:
            if not line.strip():
                continue
            if match := _RULE_START.match(line):
                indent = len(match.group("marker"))
                continue
            # A continuation is indented to the width of its rule's marker; anything deeper is a
            # nested list or a code block inside the same rule, which is still the rule's.
            if indent is not None and line.startswith(" " * indent):
                continue
            offenders.append(
                f"  {LESSONS.name}:{number} under '## {heading}' "
                f"(heading at line {heading_line}): {line[:72]!r}"
            )

    assert not offenders, (
        "tasks/lessons.md holds prose that is not part of a numbered rule:\n"
        + "\n".join(offenders[:20])
        + f"\n\n{_FOLD_INSTEAD}"
    )


def test_no_section_heading_names_an_incident() -> None:
    """A `##` heading is a theme. The weaker, more legible half of the rule above.

    Kept because the failure message a reader gets should name the defect in the vocabulary they
    recognise — "this heading is dated" — and because it catches a *dated* heading whose body someone
    has indented under a number. It is not the primary assertion: most of the 88 appended headings
    carried their date in trailing parentheses, so the obvious `^## \d{4}-` form would have missed
    them, and seven carried no date at all.
    """
    dated = [
        f"  '## {heading}' (line {line})"
        for heading, line, _ in _sections()
        if _INCIDENT_IN_HEADING.search(heading)
    ]
    assert not dated, (
        "tasks/lessons.md has a section heading that names an incident rather than a theme:\n"
        + "\n".join(dated)
        + f"\n\n{_FOLD_INSTEAD}"
    )


def test_every_theme_appears_once() -> None:
    """Two sections with one name is two places a rule could go, which is how a duplicate starts."""
    headings = [heading for heading, _, _ in _sections()]
    duplicated = sorted({h for h in headings if headings.count(h) > 1})
    assert not duplicated, f"tasks/lessons.md declares a theme twice: {duplicated}"


def test_every_rule_number_is_unique() -> None:
    """Numbers are stable labels, so two rules sharing one makes every citation ambiguous.

    Gaps are deliberately *not* refused. `tasks/lessons.md` freezes its numbering for the reason
    `CLAUDE.md` freezes the `D-NNN` sequence — a gap is harmless and a moved number breaks every
    citation to it — so 31 is unallocated and stays that way.
    """
    numbers = [
        int(match.group("number"))
        for line in _lessons_text().split("\n")
        if (match := _RULE_START.match(line))
    ]
    assert numbers, "tasks/lessons.md contains no numbered rules at all"
    duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicated, (
        f"tasks/lessons.md reuses rule number(s) {duplicated}. A rule keeps its number wherever it "
        "moves; a new rule takes the next free number at the end of its section."
    )


def _rule_numbers() -> set[int]:
    return {
        int(match.group("number"))
        for line in _lessons_text().split("\n")
        if (match := _RULE_START.match(line))
    }


def _cause_letters() -> set[str]:
    return set(re.findall(r"\*\*\(([a-z])\)", _lessons_text()))


def _citations() -> list[tuple[Path, str, str]]:
    """Every `rule N` / `cause (x)` written near a `tasks/lessons.md` mention in `src/` or `tests/`.

    Derived from the tree rather than listed here, so a new citation is covered the day it is
    written — the same reason the middleware sweep keys on `scratchpad_tools()` rather than on a list
    beside it.
    """
    found: list[tuple[Path, str, str]] = []
    for directory in ("src", "tests"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            if "lessons.md" not in text:
                continue
            for mention in re.finditer(r"lessons\.md", text):
                window = text[max(0, mention.start() - 240) : mention.end() + 240]
                for number in re.findall(r"\brule (\d+)\b", window):
                    found.append((path, "rule", number))
                for letter in re.findall(r"\bcause \(([a-z])\)", window):
                    found.append((path, "cause", letter))
    return found


def test_the_tree_cites_this_file_by_rule_number_at_all() -> None:
    """The basis for the test below, derived and asserted so a rename cannot empty it silently.

    `tasks/lessons.md`'s own rule 66: derive the scope, do not assert that it is non-empty. The
    derivation is `_citations()` walking `src/` and `tests/`; this assertion guards only the residue
    that derivation cannot see — a repository-wide rename of the file, or of the phrase "rule N",
    which would leave the check below vacuously green.
    """
    citations = _citations()
    print(f"{len(citations)} rule/cause citations of tasks/lessons.md found under src/ and tests/")
    assert citations, (
        "no module or test cites tasks/lessons.md by rule number any more. Either the file was "
        "renamed — in which case fix this guard's derivation — or the citations were removed, in "
        "which case the check below is measuring nothing."
    )


@pytest.mark.parametrize("kind", ["rule", "cause"])
def test_every_rule_citation_in_the_tree_resolves(kind: str) -> None:
    """Folding a lesson must not silently invalidate a docstring that cites it.

    `tests/test_tool_framing.py` cites rule 9, `tests/test_middleware_order.py` rule 10, and three
    tests cite causes (e), (f) and (g) of the vacuous-guard diagnostic. None of those is reachable by
    mypy, ruff or any other gate, so a renumbering is invisible until a reader follows the pointer to
    the wrong paragraph — which is worse than a dangling one, because it reads as verified.
    """
    available = {"rule": {str(n) for n in _rule_numbers()}, "cause": _cause_letters()}[kind]
    broken = [
        f"  {path.relative_to(REPO_ROOT)} cites {kind} {token}"
        for path, citation_kind, token in _citations()
        if citation_kind == kind and token not in available
    ]
    assert not broken, (
        f"a citation of tasks/lessons.md names a {kind} the file does not define:\n"
        + "\n".join(broken)
        + f"\n\navailable: {sorted(available)}\n"
        "Repair the citation in the same commit as the fold, or keep the label."
    )


def test_the_digest_is_shorter_than_the_narrative_it_replaced() -> None:
    """The reading-cost bound, derived from the archives rather than typed here.

    The file's claim is that it can be read at session start *because* it is short, and the evidence
    for what "too long" means is the narrative it was extracted from. So the ceiling is the shortest
    archive on disk: the digest that has grown past the file it replaced has stopped being a digest.
    Deliberately the weakest assertion in this module — a line count says nothing about why the file
    grew, and both times it grew it grew by narrative, which the tests above are about.
    """
    archives = sorted(ARCHIVE_DIR.glob("lessons-*.md"))
    assert archives, f"no narrative archive under {ARCHIVE_DIR}; this bound has no basis"
    lengths = {path.name: len(path.read_text(encoding="utf-8").splitlines()) for path in archives}
    digest = len(_lessons_text().splitlines())
    ceiling = min(lengths.values())
    print(f"digest {digest} lines; archives {lengths}")
    assert digest < ceiling, (
        f"tasks/lessons.md is {digest} lines, at or past the {ceiling} lines of the shortest "
        f"narrative it replaced ({lengths}). It is not readable at session start any more, which is "
        "the only reason it works. Fold repeats into one rule and move the incidents to the archive."
    )


def test_the_header_links_every_archive_on_disk() -> None:
    """An archived incident nobody can find from the digest is an incident that is gone.

    The header's job is to say where the long form went. Derived from the directory, so a third
    archive owes a link the day it is written rather than whenever somebody notices.
    """
    header = _lessons_text().split("\n## ", 1)[0]
    unlinked = [
        path.name for path in sorted(ARCHIVE_DIR.glob("lessons-*.md")) if path.name not in header
    ]
    assert not unlinked, (
        f"tasks/lessons.md's header does not link {unlinked}. Link it beside the other archives, so "
        "a reader whose paragraph is not enough can reach the incident behind it."
    )
