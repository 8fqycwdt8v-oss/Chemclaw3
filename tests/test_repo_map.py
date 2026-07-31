"""`ARCHITECTURE.md` describes the tree that actually exists, and every directory explains itself.

The repository was restructured twice in a week — `services/` flattened (D-146), eighteen packages
regrouped under `src/chemclaw/` (D-148), the last false duplicate dissolved and the corpora folded
into `data/` (D-156). Each pass ended with a map written by hand, and `ARCHITECTURE.md` closes by
*promising* to stay in sync ("adding a top-level directory means adding a row here") with nothing
enforcing it. A map that has quietly drifted is worse than no map: it is the first thing a newcomer
reads, and it is believed.

So the two halves of "can a human navigate this?" are checked mechanically:

1. **Every directory has a `README.md`.** GitHub renders one the moment you click a folder, which
   makes it the highest-leverage documentation in the repository — and it was present in five of
   fourteen packages before D-156.
2. **The map and the tree name the same directories, in both directions.** A row for a directory
   that no longer exists sends a reader somewhere empty; a directory with no row is invisible to
   anyone who trusted the map.

Deliberately about *presence*, not content: whether a README is any good is a review matter, and a
test that graded prose would be gamed by padding.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ARCHITECTURE = _ROOT / "ARCHITECTURE.md"
_PACKAGE = _ROOT / "src" / "chemclaw"

# Tooling and version-control directories, which document themselves by convention and belong to no
# layer. `.github/` is named in the map anyway (it is where CI actually lives, which D-146 learned
# the hard way) but it needs no README of its own.
_NOT_DOCUMENTED = {".git", ".github", ".venv", "src"}

# A table row's first cell, `| `dirname/` | …` or `| `dirname` | …`, with the trailing slash and the
# backticks optional so the map can read naturally.
_ROW = re.compile(r"^\| `([A-Za-z0-9_.-]+)/?` \|", re.MULTILINE)


def _tracked_directories(parent: Path) -> set[str]:
    """The real, non-hidden, non-cache directories directly under `parent`."""
    return {
        entry.name
        for entry in parent.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "__"))
    }


def _mapped_names() -> set[str]:
    """Every directory `ARCHITECTURE.md` claims exists, from either of its two tables."""
    return set(_ROW.findall(_ARCHITECTURE.read_text(encoding="utf-8")))


def test_every_subpackage_has_a_readme() -> None:
    """Clicking a package on GitHub explains it without reading a single module."""
    subpackages = _tracked_directories(_PACKAGE)
    assert subpackages, "no subpackages found under src/chemclaw — this test would assert nothing"
    missing = sorted(name for name in subpackages if not (_PACKAGE / name / "README.md").is_file())
    assert not missing, f"src/chemclaw/ subpackages with no README.md: {missing}"


def test_every_top_level_directory_has_a_readme() -> None:
    """The same, for the directories a visitor sees first."""
    directories = _tracked_directories(_ROOT) - _NOT_DOCUMENTED
    assert directories, "no top-level directories found — this test would assert nothing"
    missing = sorted(name for name in directories if not (_ROOT / name / "README.md").is_file())
    assert not missing, f"top-level directories with no README.md: {missing}"


def test_the_map_lists_every_directory_that_exists() -> None:
    """A directory absent from `ARCHITECTURE.md` is invisible to anyone who trusts the map."""
    on_disk = (_tracked_directories(_ROOT) - {"src"}) | _tracked_directories(_PACKAGE)
    assert on_disk, "found no directories to check — this test would assert nothing"
    unmapped = sorted(on_disk - _mapped_names())
    assert not unmapped, (
        f"directories with no row in ARCHITECTURE.md: {unmapped}. Adding a directory means adding "
        "a row — that promise is the last section of that file."
    )


def test_the_map_lists_nothing_that_has_gone() -> None:
    """The other direction: a row for a vanished directory sends a reader somewhere empty.

    This is the half a restructure breaks. `src/chemclaw/mcp/` was dissolved in D-156 and its row
    would have sat in the map indefinitely, describing a package with a rationale that no longer
    applied to anything.
    """
    on_disk = (
        _tracked_directories(_ROOT)
        | _tracked_directories(_PACKAGE)
        | {"src", ".github/workflows", ".github"}
    )
    stale = sorted(_mapped_names() - on_disk)
    assert not stale, f"ARCHITECTURE.md rows for directories that do not exist: {stale}"


def test_no_import_package_sits_beside_data() -> None:
    """`src/` is all the code — the one rule the map opens with, asserted rather than asserted-to.

    A top-level directory holding `.py` files means the rule has quietly acquired an exception, and
    every "where does this live?" answer gets longer. `tests/` and `examples/` are first-party code
    that deliberately does not ship (`test_packaging.py` owns that distinction).
    """
    code_outside_src = sorted(
        name
        for name in _tracked_directories(_ROOT) - {"src", "tests", "examples"}
        if any((_ROOT / name).rglob("*.py"))
    )
    assert not code_outside_src, (
        f"directories beside src/ containing Python: {code_outside_src}. `src/` is all the code; "
        "everything beside it is data, configuration or documents."
    )
