"""Who may build a `NoteUnit`, the one value a note store's `put` writes.

`put` takes each note's `created_by` as given: a store cannot tell a person from a caller that says
it is one. So every module that builds a unit is named here with where its authorship comes from.
A new one (W4.8's `PUT /knowledge/notes/{id}` is the expected next) fails this test until it is
added, and it may only be added once it authenticates the author before it calls `put`: whoever
writes `created_by: human` must be the person, and an agent's write must say `agent`.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

#: Each module that builds a `NoteUnit`, and why the `created_by` it passes can be trusted.
_WRITERS = {
    # The one write path: `record_note` refuses any subject that is not agent-authored.
    "kg/record.py": "record_note refuses a subject not written by the agent",
    # Copies notes already in the authoritative tree, with the authorship the tree holds.
    "kg/importer.py": "the tree's notes, as committed",
    "kg/compare.py": "repairs Postgres from the tree's own note",
    # An operator benchmark over a throwaway corpus of its own.
    "cli/kg_store_bench.py": "a synthetic agent-authored corpus",
}


def _builds_a_unit(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "NoteUnit"
        for node in ast.walk(tree)
    )


def test_every_module_that_builds_a_note_unit_is_a_named_writer() -> None:
    """The modules under `src/` that call `NoteUnit(...)` are exactly the ones named above."""
    builders = {
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if _builds_a_unit(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert "kg/record.py" in builders, "the scan no longer finds the one write path"
    unnamed = builders - set(_WRITERS)
    assert not unnamed, (
        f"{sorted(unnamed)} build a NoteUnit, and a store's put trusts its created_by: "
        "authenticate the author before calling put, then name the module in _WRITERS"
    )
    assert not set(_WRITERS) - builders, "a named writer no longer builds a unit; drop it"
