"""The trigger of `D-2026-09-27-a-literature-index-waits-for-a-corpus-and-a-licence`, made executable.

That ADR declines building a literature index and names two conditions that reopen it. One is a
deployment's own act and no file here can see it. The other is a row in the sibling fleet's
`MODULES.md`: `litsearch` — the server that would give `deep-research` a real index — is listed as
`proposed`. When that status changes, the decline's premise has moved, and `CLAUDE.md` asks for a
trigger that reds rather than one somebody has to remember to watch (`D-092`'s was met twice, in
that same fleet, and nobody noticed).
"""

import re

import pytest

from tests.siblings import SIBLING_SKIP, sibling_root

_ADR = "D-2026-09-27-a-literature-index-waits-for-a-corpus-and-a-licence"

#: A `MODULES.md` table row: `| \`litsearch\` | <port> | <status> | ...`.
_LITSEARCH_ROW = re.compile(r"^\|\s*`litsearch`\s*\|\s*\d+\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)


def test_the_literature_server_is_still_only_proposed() -> None:
    """Fails when `litsearch` leaves `proposed` in the fleet's module plan — the ADR's trigger."""
    checkout, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if checkout is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}; the trigger of {_ADR} is NOT checked")
    modules = checkout / "MODULES.md"
    assert modules.is_file(), f"{checkout} has no MODULES.md to read `litsearch`'s status from"
    found = _LITSEARCH_ROW.findall(modules.read_text(encoding="utf-8"))
    assert len(found) == 1, (
        f"expected one `litsearch` row in {modules}, found {len(found)}: the row this trigger "
        f"reads has moved or been removed, so re-read {_ADR} and re-point the trigger"
    )
    assert found[0] == "proposed", (
        f"`litsearch` is now {found[0]!r} in {modules}. That is the trigger of {_ADR}: a literature "
        "index may exist to bind, so the decline and skills/deep-research/SKILL.md's statement "
        "that there is no literature index both need revisiting"
    )


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("| `litsearch` | 8880 | proposed | `search_literature` | x |", "proposed"),
        ("| `litsearch` | 8880 | shipped | `search_literature` | x |", "shipped"),
    ],
)
def test_the_row_pattern_reads_the_status_column(text: str, status: str) -> None:
    """The pattern reads the third column, so a changed status is seen rather than missed."""
    assert _LITSEARCH_ROW.findall(text) == [status]
