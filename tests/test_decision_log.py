"""The ADR record's identity checks: ids, headings, the ledger, supersession links and the template.

An ADR id is cited across the tree, so an id naming two files, a file whose heading names another
id, or a ledger that drifted from the files beside it sends a reader to the wrong rationale. From
`_TEMPLATE_CURSOR` onward a new ADR must also carry the template's `## Options` section and, when
it declines something, a `Revisit when:` line. Prose quality is a review matter, not tested here.
"""

import re
from collections import Counter
from pathlib import Path

_DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
_INDEX = _DECISIONS / "README.md"

_NUMBERED = r"D-\d{3}"
_DATED = r"D-\d{4}-\d{2}-\d{2}-[a-z0-9-]+"
_FILENAME = re.compile(rf"^(?:{_NUMBERED}-[a-z0-9-]+|{_DATED})$")
_HEADING = re.compile(rf"^# ({_NUMBERED}|{_DATED}) — ", re.MULTILINE)
#: A ledger row: `| [id](file.md) | kind | title …`.
_INDEX_ROW = re.compile(rf"^\| \[({_NUMBERED}|{_DATED})\]\(([^)]*)\) \| ([a-z-]+) \|", re.MULTILINE)
_KINDS = {"decision", "defect-record"}
_SUPERSEDED_BY = re.compile(r"^\*\*Superseded-by:\*\* (.+)$", re.MULTILINE)
_LINK = re.compile(r"\]\(([^)]+\.md)\)")

#: ADRs dated on or after this carry the template (`TEMPLATE.md`); older ones are the archive.
_TEMPLATE_CURSOR = "D-2026-10-07"
_OPTIONS = re.compile(r"^## Options\b", re.MULTILINE)
_DECLINES = re.compile(r"\bdeclin(?:e|ed|es|ing)\b", re.IGNORECASE)
_TRIGGER = re.compile(r"^[ \t]*(?:[-*+]\s*)?(?:\*\*)?Revisit when\b", re.MULTILINE)


def _adr_id(path: Path) -> str:
    """The id a file carries: `D-NNN` for the numbered form, the whole stem for the dated one."""
    return path.stem[:5] if re.fullmatch(rf"{_NUMBERED}-.*", path.stem) else path.stem


def _sort_key(path: Path) -> tuple[int, int, str]:
    """Record order: numbered ADRs first, numerically, then dated ones chronologically."""
    stem = path.stem
    if re.fullmatch(rf"{_NUMBERED}-.*", stem):
        return (0, int(stem[2:5]), "")
    return (1, 0, stem)


def _adr_files() -> list[Path]:
    """Every ADR file in record order."""
    return sorted(_DECISIONS.glob("D-*.md"), key=_sort_key)


def _index_rows() -> list[tuple[str, str, str]]:
    """Every `(id, linked file, kind)` in the ledger, in file order."""
    return _INDEX_ROW.findall(_INDEX.read_text("utf-8"))


def _new_adrs() -> list[Path]:
    """The ADRs written under the template rule."""
    return [path for path in _adr_files() if path.stem >= _TEMPLATE_CURSOR]


def test_every_adr_id_is_unique() -> None:
    """No id names two decisions — two differently slugged `D-NNN` files would."""
    duplicates = sorted(adr for adr, n in Counter(map(_adr_id, _adr_files())).items() if n > 1)
    assert not duplicates, f"docs/decisions/ reuses ADR ids: {duplicates}"


def test_every_filename_matches_its_heading() -> None:
    """The id in the filename is the id in the single `# <id> — Title` heading."""
    for path in _adr_files():
        assert _FILENAME.match(path.stem), f"{path.name}: expected `D-YYYY-MM-DD-slug.md`"
        headings = _HEADING.findall(path.read_text("utf-8"))
        assert headings == [_adr_id(path)], f"{path.name} carries headings {headings}"


def test_the_ledger_lists_exactly_the_files_in_record_order() -> None:
    """`README.md` has one linked row per ADR file, in record order, each with a known kind."""
    rows = _index_rows()
    assert [adr for adr, _, _ in rows] == [_adr_id(path) for path in _adr_files()], (
        "docs/decisions/README.md must list exactly the ADR files beside it, in record order"
    )
    assert [link for _, link, _ in rows] == [path.name for path in _adr_files()]
    unknown = sorted({kind for _, _, kind in rows} - _KINDS)
    assert not unknown, f"ledger kinds must be one of {sorted(_KINDS)}, found {unknown}"


def test_every_superseded_by_target_exists() -> None:
    """A `**Superseded-by:**` line links only to ADR files that exist, never to itself."""
    names = {path.name for path in _adr_files()}
    for path in _adr_files():
        for line in _SUPERSEDED_BY.findall(path.read_text("utf-8")):
            targets = _LINK.findall(line)
            assert targets, f"{path.name}: Superseded-by names no linked ADR"
            missing = sorted(t for t in targets if t not in names or t == path.name)
            assert not missing, f"{path.name}: Superseded-by targets {missing} are not other ADRs"


def test_new_adrs_follow_the_template() -> None:
    """A new ADR weighs options, declines carry a trigger, and none is a defect record."""
    kinds = {adr: kind for adr, _, kind in _index_rows()}
    for path in _new_adrs():
        text = path.read_text("utf-8")
        assert _OPTIONS.search(text), f"{path.name} has no `## Options` section (TEMPLATE.md)"
        if _DECLINES.search(text):
            assert _TRIGGER.search(text), f"{path.name} declines something with no `Revisit when:`"
        assert kinds.get(_adr_id(path)) == "decision", (
            f"{path.name} is ledgered as a defect record; a defect fix is a commit and a test"
        )


def test_the_record_order_puts_numbered_ids_first_and_numerically() -> None:
    """`D-900` must sort before any dated id, which plain string order gets wrong."""
    paths = [Path("D-2026-01-02-b.md"), Path("D-900-x.md"), Path("D-2025-12-31-a.md")]
    assert [p.stem for p in sorted(paths, key=_sort_key)] == [
        "D-900-x",
        "D-2025-12-31-a",
        "D-2026-01-02-b",
    ]


def test_a_malformed_id_is_rejected() -> None:
    """The filename shape stays a real gate."""
    for bad in ("D-2026-7-31-slug", "D-2026-07-31", "D-999", "D-2026-07-31-Slug", "D-1234-slug"):
        assert not _FILENAME.match(bad), f"{bad} should not be a valid ADR filename"
