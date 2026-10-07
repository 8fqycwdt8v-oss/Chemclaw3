"""`ARCHITECTURE.md` maps the tree that exists, and every directory explains itself.

`src/` is all the code. Checked in both directions: every directory has a `README.md`, and the
map and the tree name the same directories. About presence, not prose quality.
"""

import ast
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ARCHITECTURE = _ROOT / "ARCHITECTURE.md"
_PACKAGE = _ROOT / "src" / "chemclaw"

#: Tooling and version-control directories, which belong to no layer and need no README.
_NOT_DOCUMENTED = {".git", ".github", ".venv", "src"}

#: A table row's first cell, `| `dirname/` | …`.
_ROW = re.compile(r"^\| `([A-Za-z0-9_.-]+)/?` \|", re.MULTILINE)


def _is_cache(segment: str) -> bool:
    """Whether one path segment is a tool's scratch (hidden, or `__pycache__`), not content."""
    return segment.startswith(".") or segment == "__pycache__"


def _tracked_directories(parent: Path) -> set[str]:
    """The non-hidden directories directly under `parent` that hold content and git does not ignore.

    A directory whose subtree is only caches is a husk git cannot store, so it is skipped. Content
    is judged relative to the directory, so a checkout under a dot-path is still seen.
    """

    def has_content(directory: Path) -> bool:
        return any(
            path.is_file()
            and not any(_is_cache(part) for part in path.relative_to(directory).parts)
            for path in directory.rglob("*")
        )

    def tracked(directory: Path) -> bool:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(directory)], cwd=parent, capture_output=True
        )
        return ignored.returncode != 0

    return {
        entry.name
        for entry in parent.iterdir()
        if entry.is_dir()
        and not entry.name.startswith((".", "__"))
        and tracked(entry)
        and has_content(entry)
    }


def _python_packages(parent: Path) -> set[Path]:
    """Every directory below `parent`, at any depth, that holds Python modules.

    A directory holding only a manifest is described by that manifest and its seam's README.
    """
    found: set[Path] = set()
    for name in _tracked_directories(parent):
        directory = parent / name
        if any(child.suffix == ".py" for child in directory.iterdir() if child.is_file()):
            found.add(directory)
        found |= _python_packages(directory)
    return found


def _mapped_names() -> set[str]:
    """Every directory `ARCHITECTURE.md` claims exists, from either of its tables."""
    return set(_ROW.findall(_ARCHITECTURE.read_text(encoding="utf-8")))


def test_directories_are_found_from_a_checkout_under_a_dot_directory(tmp_path: Path) -> None:
    """Where the clone sits (e.g. `.claude/worktrees/<id>/`) must not change what is seen."""
    root = tmp_path / ".agent-worktree" / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (root / "cached" / "__pycache__").mkdir(parents=True)
    (root / "cached" / "__pycache__" / "mod.pyc").write_text("", encoding="utf-8")

    found = _tracked_directories(root)

    assert "pkg" in found, "a real directory vanished because the checkout sits under a dot-path"
    assert "cached" not in found, "a directory holding only caches must still count as empty"


def test_a_package_holding_only_an_init_file_is_still_seen(tmp_path: Path) -> None:
    """`__init__.py` is content: the cache filter excludes caches, not every dunder name."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    assert "pkg" in _tracked_directories(root)


def test_every_python_package_has_a_readme() -> None:
    """Every directory under `src/chemclaw/` holding Python modules has a `README.md`."""
    packages = _python_packages(_PACKAGE)
    assert packages, "no packages found under src/chemclaw — this test would assert nothing"
    missing = sorted(
        str(path.relative_to(_PACKAGE)) for path in packages if not (path / "README.md").is_file()
    )
    assert not missing, f"src/chemclaw/ packages with no README.md: {missing}"


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
    assert not unmapped, f"directories with no row in ARCHITECTURE.md: {unmapped}"


def test_the_map_lists_nothing_that_has_gone() -> None:
    """A row for a vanished directory sends a reader somewhere empty."""
    on_disk = (
        _tracked_directories(_ROOT)
        | _tracked_directories(_PACKAGE)
        | {"src", ".github/workflows", ".github"}
    )
    stale = sorted(_mapped_names() - on_disk)
    assert not stale, f"ARCHITECTURE.md rows for directories that do not exist: {stale}"


def _data_row() -> str:
    """`ARCHITECTURE.md`'s `data/` row, which names the corpora inline."""
    for line in _ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        if line.startswith("| `data/` |"):
            return line
    raise AssertionError("ARCHITECTURE.md has no `data/` row")


def _data_readme_table() -> str:
    """Only the table rows of `data/README.md` (its prose names `knowledge/` and `skills/`)."""
    return "\n".join(
        line
        for line in (_ROOT / "data" / "README.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `")
    )


def test_both_maps_of_data_name_every_corpus_that_exists() -> None:
    """`ARCHITECTURE.md`'s `data/` row and `data/README.md` name exactly the corpora on disk."""
    on_disk = _tracked_directories(_ROOT / "data")
    assert on_disk, "found no corpora under data/ — this test would assert nothing"

    row, table = _data_row(), _data_readme_table()
    for name in sorted(on_disk):
        assert f"`{name}/`" in table, f"data/README.md's table has no row for data/{name}/"
        assert f"`{name}/`" in row, f"ARCHITECTURE.md's `data/` row does not name data/{name}/"

    for document, text in (("ARCHITECTURE.md's `data/` row", row), ("data/README.md", table)):
        named = {match.rstrip("/") for match in re.findall(r"`([a-z0-9][a-z0-9-]*/)`", text)}
        gone = sorted(named - on_disk - {"data"})
        assert not gone, f"{document} names corpora that are not there: {gone}"


def test_no_import_package_sits_beside_data() -> None:
    """`src/` is all the code: no top-level directory but `tests/` and `examples/` holds Python."""
    code_outside_src = sorted(
        name
        for name in _tracked_directories(_ROOT) - {"src", "tests", "examples"}
        if any((_ROOT / name).rglob("*.py"))
    )
    assert not code_outside_src, f"directories beside src/ containing Python: {code_outside_src}"


#: The one corpus inside the package: benchmark data pinned to the surrogate that reads it.
_CORPUS_IN_SRC = {"science/bo/benchmarks/data/reizman_suzuki_case_1.csv"}
_CORPUS_SUFFIXES = {".csv", ".tsv", ".parquet", ".jsonl", ".json", ".txt", ".sdf", ".smi"}


def test_no_corpus_lives_outside_data_except_the_one_that_is_argued() -> None:
    """`data/` holds every runtime corpus; `_CORPUS_IN_SRC` is the one exception inside `src/`."""
    corpora = sorted(
        str(path.relative_to(_PACKAGE))
        for path in _PACKAGE.rglob("*")
        if path.is_file()
        and path.suffix in _CORPUS_SUFFIXES
        and not any(_is_cache(part) for part in path.relative_to(_PACKAGE).parts)
    )
    assert set(_CORPUS_IN_SRC) <= set(corpora), (
        f"the argued exception is no longer on disk: {sorted(set(_CORPUS_IN_SRC) - set(corpora))}"
    )
    unargued = sorted(set(corpora) - _CORPUS_IN_SRC)
    assert not unargued, f"corpora under src/chemclaw/ outside data/: {unargued}"


def test_no_tracked_text_file_carries_an_unresolved_conflict_marker() -> None:
    """A `<<<<<<<` left in a committed file is invisible to every structural check."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")

    offenders = []
    for name in filter(None, tracked):
        try:
            content = (_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, deleted in the working tree, or a symlink out of the checkout
        for number, line in enumerate(content.splitlines(), start=1):
            # `=======` must match exactly: a Markdown setext rule is a run of `=` too.
            if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "=======":
                offenders.append(f"{name}:{number}")

    assert not offenders, f"unresolved merge conflict markers in tracked files: {offenders}"


#: `assert` statements in `src/` that only narrow a type for mypy, keyed by their exact text.
#: `python -O` deletes every `assert`, so one that enforces anything must be `if ...: raise`.
_NARROWING_ASSERTS = {
    "science/labels/store.py": {
        "assert row.labelled_at is not None  # the caller filtered on it": "narrows for mypy",
    },
    "agent/chemclaw_agent.py": {
        "assert profile.tool_names is not None  # only called when the profile narrows": (
            "narrows for mypy"
        ),
    },
    "cli/live_data.py": {
        "assert dataset.dataset_id is not None": "set by the request that just created it",
    },
}


def test_no_assert_in_src_enforces_an_invariant() -> None:
    """Every `assert` in `src/` is a listed type narrowing, and every listed one still exists."""
    found: dict[str, dict[str, int]] = {}
    for path in sorted((_ROOT / "src").rglob("*.py")):
        relative = path.relative_to(_PACKAGE).as_posix()
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assert):
                found.setdefault(relative, {})[lines[node.lineno - 1].strip()] = node.lineno

    unargued = {
        name: [text for text in statements if text not in _NARROWING_ASSERTS.get(name, {})]
        for name, statements in found.items()
    }
    unargued = {name: texts for name, texts in unargued.items() if texts}
    assert not unargued, (
        f"an `assert` in src/ not in _NARROWING_ASSERTS: {unargued}. If it narrows a type, list "
        "it with the reason; if it enforces anything, write `if ...: raise` instead."
    )

    stale = sorted(
        f"{name}: {text}"
        for name, statements in _NARROWING_ASSERTS.items()
        for text in statements
        if text not in found.get(name, {})
    )
    assert not stale, f"_NARROWING_ASSERTS names asserts that are no longer in src/: {stale}"


def _makefile_targets_and_phony() -> tuple[list[str], set[str]]:
    """Every target the Makefile declares (outside `define` blocks), and every `.PHONY` name."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    phony: set[str] = set()
    for match in re.finditer(r"^\.PHONY:((?:.*\\\n)*.*)$", makefile, re.MULTILINE):
        phony.update(match.group(1).replace("\\", " ").split())
    rule = re.compile(r"^([A-Za-z0-9_.%-]+(?:[ \t]+[A-Za-z0-9_.%-]+)*)[ \t]*::?(?![=])")
    targets: list[str] = []
    inside_define = False
    for line in makefile.splitlines():
        if line.startswith(("define ", "endef")):
            inside_define = line.startswith("define ")
            continue
        found = None if inside_define else rule.match(line)
        if found:
            targets.extend(
                name
                for name in found.group(1).split()
                if not name.startswith(".") and "%" not in name
            )
    return list(dict.fromkeys(targets)), phony


def test_the_phony_list_and_the_target_list_are_the_same_list() -> None:
    """A target missing from `.PHONY` silently does nothing once a same-named path exists."""
    targets, phony = _makefile_targets_and_phony()
    assert set(targets) == phony, (
        f"targets missing from .PHONY: {sorted(set(targets) - phony)}; "
        f".PHONY names with no target: {sorted(phony - set(targets))}"
    )
