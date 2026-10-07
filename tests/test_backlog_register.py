"""`docs/planning/BACKLOG.md` keeps the shape its header states.

Open rows are checkboxes of at most two lines with no duplicates, every `path::symbol` anchor
resolves in the tree, the file states no live count of itself, and every deferred item carries a
trigger. Whether a row is still *true* needs reading the code and is deliberately not checked.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKLOG = _ROOT / "docs" / "planning" / "BACKLOG.md"
_ROW_TITLE = re.compile(r"^- \[ \] \*\*(.+?)\*\*", re.MULTILINE)
_ANCHOR = re.compile(r"`([\w./-]+/[\w.-]+\.(?:py|c|sh|ya?ml|sql|toml|tpl)::[\w.]+)`")
_STATED_COUNT = re.compile(
    r"\b\d[\d,]*\s+(?:of its\s+)?(?:open\s+)?(?:rows|findings|items)\b|\b\d[\d,]*\s+are\s+open\b"
)
_MAX_ROW_LINES = 2


def _text() -> str:
    """The register's text."""
    return _BACKLOG.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """The body of the `## heading` section, up to the next `## `."""
    body = _text().split(f"\n## {heading}\n", 1)
    assert len(body) == 2, f"BACKLOG.md has no `## {heading}` section"
    return body[1].split("\n## ", 1)[0]


def _rows() -> list[list[str]]:
    """Each open row as its lines: the `- [ ]` line plus the indented lines directly under it."""
    rows: list[list[str]] = []
    current: list[str] | None = None
    for line in _text().splitlines():
        if line.startswith("- [ ] "):
            current = [line]
            rows.append(current)
        elif current is not None and line.startswith("  ") and line.strip():
            current.append(line)
        else:
            current = None
    return rows


def _resolve(anchor: str) -> str:
    """`""` when every component of `anchor`'s symbol appears in its file, else why not."""
    path, _, symbol = anchor.partition("::")
    for base in ("", "src/chemclaw/"):
        candidate = _ROOT / (base + path)
        if candidate.exists():
            body = candidate.read_text(encoding="utf-8", errors="replace")
            absent = [p for p in symbol.split(".") if not re.search(rf"\b{re.escape(p)}\b", body)]
            return f"{base + path} has no {', '.join(absent)}" if absent else ""
    return f"no such file: {path}"


def test_open_rows_are_parsed() -> None:
    """Guard the parse: a shape change would otherwise pass every check below vacuously."""
    assert len(_rows()) > 5, "no open rows parsed from BACKLOG.md; the row shape moved"


def test_every_open_row_is_at_most_two_lines() -> None:
    """A row is a pointer, not a report: measurements belong in the commit or the ADR."""
    long_rows = [row[0][:80] for row in _rows() if len(row) > _MAX_ROW_LINES]
    assert not long_rows, f"BACKLOG.md rows longer than {_MAX_ROW_LINES} lines: {long_rows}"


def test_no_row_appears_twice() -> None:
    """One item, one row: a second copy reads as a second item and drifts from the first."""
    titles = _ROW_TITLE.findall(_text())
    repeated = sorted({t for t in titles if titles.count(t) > 1})
    assert not repeated, f"rows appearing more than once in BACKLOG.md: {repeated}"


def test_every_anchor_resolves_to_something_in_the_tree() -> None:
    """An anchor is what the next reader greps; a dead one sends them to the wrong place."""
    anchors = sorted(set(_ANCHOR.findall(_text())))
    assert len(anchors) > 5, "no `path::symbol` anchors parsed from BACKLOG.md"
    dead = {a: reason for a in anchors if (reason := _resolve(a))}
    assert not dead, f"BACKLOG.md anchors that resolve to nothing: {dead}"


def test_the_register_states_no_live_count_of_itself() -> None:
    """The header shows the `grep` that counts rows; a number in prose goes stale."""
    assert "grep -c '^- \\[ \\]'" in _text(), "the header lost the command that counts rows"
    stated = _STATED_COUNT.findall(_text())
    assert not stated, f"BACKLOG.md states a count of its own rows: {stated}"


def test_every_deferred_item_is_one_line_with_a_trigger() -> None:
    """Deferred means postponed until something happens, so each item names that something."""
    items = [line for line in _section("Deferred").splitlines() if line.startswith("- ")]
    assert len(items) > 10, "no deferred items parsed from BACKLOG.md"
    missing = [line[:80] for line in items if "*Revisit:*" not in line]
    assert not missing, f"deferred items without a `*Revisit:*` trigger: {missing}"
    lines = _section("Deferred").splitlines()
    wrapped = [lines[i - 1][:80] for i, line in enumerate(lines) if line.startswith("  ")]
    assert not wrapped, f"deferred items wrapped over several lines: {wrapped}"
