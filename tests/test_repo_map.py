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
    """The real, non-hidden, non-cache directories directly under `parent`.

    A directory holding no tracked file is skipped, and that is not a convenience. Git cannot store
    an empty directory, so when a restructure moves a package away it deletes the files and leaves
    the folders behind in every working tree that had them — `src/chemclaw/mcp/` after D-156 moved
    `fingerprints` out of it. Those husks are not part of the repository: they exist in no commit,
    reach no clone, and cannot be given a README or a map row because there is nothing to commit
    them with. Counting them made this suite fail on every developer's machine after merging the
    restructure while passing in CI, whose clone never had them — the worst shape a test can take,
    because the failure looks like the map is wrong when the map is right.

    Emptiness is judged by content rather than by asking git, so the check stays a plain filesystem
    walk with no subprocess: a directory whose whole subtree is caches and other husks has no file
    a reader could open, which is the same conclusion by a cheaper route.
    """

    def has_content(directory: Path) -> bool:
        # Judge each file by its path *below* `directory`, never by its absolute path. `rglob`
        # yields absolute paths, so testing every part meant one dot-segment anywhere above the
        # repo — `/home/u/.local/src/chemclaw`, or the `.claude/worktrees/<id>/` checkout every
        # background agent runs in — marked every file hidden, emptied every directory, and failed
        # this suite on where the clone sits rather than on what it contains. The guard is about
        # caches and husks *inside* the tree; the directory's own name is filtered below.
        return any(
            path.is_file()
            and not any(part.startswith((".", "__")) for part in path.relative_to(directory).parts)
            for path in directory.rglob("*")
        )

    return {
        entry.name
        for entry in parent.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "__")) and has_content(entry)
    }


def _mapped_names() -> set[str]:
    """Every directory `ARCHITECTURE.md` claims exists, from either of its two tables."""
    return set(_ROW.findall(_ARCHITECTURE.read_text(encoding="utf-8")))


def test_directories_are_found_from_a_checkout_under_a_dot_directory(tmp_path: Path) -> None:
    """Where the clone sits must not change what this suite sees.

    `_tracked_directories` walks with `rglob`, which yields absolute paths, so judging the
    hidden/cache filter on every part meant a single dot-segment *above* the repo emptied every
    directory: `_tracked_directories` returned `set()`, and the guards in the tests below turned
    a real map error into "no subpackages found". That is not hypothetical — every background
    agent works in a `.claude/worktrees/<id>/` checkout, where all four tests in this file failed
    on path location alone while CI stayed green.

    A fixture tree under a dot-named parent is the whole proof: the same content must be found
    there as anywhere else, and a genuinely hidden child inside it must still be ignored.
    """
    root = tmp_path / ".agent-worktree" / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (root / "cached" / "__pycache__").mkdir(parents=True)
    (root / "cached" / "__pycache__" / "mod.pyc").write_text("", encoding="utf-8")

    found = _tracked_directories(root)

    assert "pkg" in found, "a real directory vanished because the checkout sits under a dot-path"
    assert "cached" not in found, "a directory holding only caches must still count as empty"


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


def _bundles() -> set[str]:
    """Every connector bundle on disk, named by its directory."""
    return {path.parent.name for path in (_PACKAGE / "connectors").glob("*/connector.yaml")}


def _bundles_owning_durable_work() -> set[str]:
    """The bundles that declare `jobs:` and therefore run their own Temporal worker."""
    return {
        name
        for name in _bundles()
        if any(
            line.startswith("jobs:")
            for line in (_PACKAGE / "connectors" / name / "connector.yaml")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    }


def test_the_runbook_names_the_bundles_that_actually_ship() -> None:
    """The runbook describes the bundle set, so the set has to be checked rather than remembered.

    It said "Six bundles" and listed six, omitting `qm` entirely — while the same document, twenty
    lines later, explained that the QM run lives in `connectors/qm/` now. A count in prose goes
    stale silently; this is the same claim in a form that fails loudly
    (D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose).
    """
    runbook = (_ROOT / "docs" / "guides" / "runbook.md").read_text(encoding="utf-8")
    marker = "**What ships today.**"
    assert marker in runbook, "the paragraph that enumerates the bundles has been renamed"
    # Scoped to that one paragraph, not the whole document. Searching the file finds `qm` in half a
    # dozen unrelated sentences, so a paragraph that had dropped a bundle still passed — which is
    # exactly the miss this test exists to prevent, and it survived the first mutation round.
    paragraph = runbook.split(marker, 1)[1].split("\n\n", 1)[0]
    bundles = _bundles()
    assert bundles, "no connector bundles found; the glob or the layout moved"
    missing = sorted(name for name in bundles if f"`{name}`" not in paragraph)
    assert not missing, f"the runbook's bundle paragraph does not name {missing}"


def test_the_runbook_names_every_bundle_that_owns_durable_work() -> None:
    """Calling `bo` "the one that also owns durable work" was wrong, in a way one grep settles.

    `calc`, `bo` and `qm` each declare `jobs:`, so each runs a second Deployment for its own
    Temporal worker. The runbook said only `bo`, nine lines after calling `calc` "the worked
    example (five jobs, one workflow, one queue, its own worker)" — a document disagreeing with
    itself, which is what an unchecked claim looks like once someone edits half of it.
    """
    durable = _bundles_owning_durable_work()
    assert durable == {"bo", "calc", "qm"}, (
        f"the set of bundles owning durable work changed to {sorted(durable)}; update the runbook "
        "paragraph that names them, which is the claim this test exists to keep honest"
    )
