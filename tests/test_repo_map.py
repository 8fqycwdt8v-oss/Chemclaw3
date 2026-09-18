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

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import yaml

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


def _is_cache(segment: str) -> bool:
    """Whether one path segment is a tool's scratch, rather than something a reader can open.

    Named rather than pattern-matched on `__`: `__pycache__` is a cache and `__init__.py` is the
    file that makes a directory a package, and a filter that cannot tell them apart hides the
    second (see `_tracked_directories`). Hidden segments stay excluded wholesale — `.mypy_cache`,
    `.ruff_cache` and `.pytest_cache` are all of that shape, and a dotfile is not documentation
    anyone clicks on GitHub.
    """
    return segment.startswith(".") or segment == "__pycache__"


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

    **What counts as a husk is a cache name, not any dunder name**, and the difference was a hole.
    The filter used to skip every path segment starting `__`, so a package holding only an
    `__init__.py` had no file this walk could see: it was skipped by both tests below, needing
    neither a README nor a map row. `__init__.py` is content — it is the file that makes the
    directory a package at all.
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
            and not any(_is_cache(part) for part in path.relative_to(directory).parts)
            for path in directory.rglob("*")
        )

    def tracked(directory: Path) -> bool:
        # A directory git ignores is not part of the repository, so it cannot need a row in the
        # repository map. Asked of git rather than kept as a name list here, because the failure
        # this fixes is a *tool's* scratch tree: `make mutants` materialises `mutants/` — a full
        # copy of the repo — for the length of its run, and `make test` in the same window went red
        # naming a directory that is not in the tree and never will be. A hard-coded exclusion
        # would have to be extended for the next tool; `.gitignore` already knows.
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", str(directory)],
                cwd=parent,
                capture_output=True,
            ).returncode
            != 0
        )

    return {
        entry.name
        for entry in parent.iterdir()
        if entry.is_dir()
        and not entry.name.startswith((".", "__"))
        and tracked(entry)
        and has_content(entry)
    }


def _python_packages(parent: Path) -> set[Path]:
    """Every directory *below* `parent`, at any depth, that holds Python modules.

    The recursive half of this file, and the half that did not exist. `_tracked_directories` reads
    `iterdir()` — direct children only — and `_ROW` cannot match a path with a slash, so a nested
    subpackage needed neither a README nor a map row: measured, adding
    `src/chemclaw/retrieval/rerank/{__init__,engine}.py` with neither left every test here green,
    while the same two files one level shallower failed four. Thirty-eight directories sat in that
    gap — every connector bundle but `results`, all three `science/` engines,
    `publish/{drivers,sinks}` — i.e. the seams a newcomer clicks into first, unexplained, in a tree
    whose map says that cannot happen.

    **Python is the predicate, and the narrowing is the decision** rather than an accident of what
    was easy to walk (`D-2026-09-07-a-driver-with-no-caller-is-not-a-capability`). A directory
    holding modules is a package someone has to read; a directory holding one manifest is described
    by that manifest and by the seam's own README, and demanding ten near-identical files of
    `ingest/sources/*` would buy a broad rule nobody satisfies in place of a narrow one everybody
    does. `deliver/channels/*`, `publish/sinks/postgres/` and `connectors/*/skills/*` are the same
    shape and are covered by their parents for the same reason.

    Reuses `_tracked_directories`' filters by recursing through it, so a husk, a cache and a
    git-ignored tool tree are excluded here for the reasons stated there.
    """
    found: set[Path] = set()
    for name in _tracked_directories(parent):
        directory = parent / name
        if any(child.suffix == ".py" for child in directory.iterdir() if child.is_file()):
            found.add(directory)
        found |= _python_packages(directory)
    return found


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


def test_a_package_holding_only_an_init_file_is_still_seen(tmp_path: Path) -> None:
    """The cache filter must exclude caches, not every dunder name.

    `has_content` skipped any file whose path below the directory had a segment starting `.` or
    `__`, so a package containing nothing but `__init__.py` counted as *empty* and vanished from
    both halves of this file: no README was demanded of it and no map row either. Constructed and
    measured on the tree this was written against — a package under `src/chemclaw/` holding nothing
    but an `__init__.py` passed all eight tests, and adding one non-dunder module beside it turned
    two of them red.

    A package with only an `__init__.py` is a plausible intermediate state during a split, which is
    exactly when the map is most likely to go stale, so the one state the guard cannot see is the
    one it is most needed in. `__pycache__` is what the filter is actually for.
    """
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    assert "pkg" in _tracked_directories(root), (
        "a package holding only `__init__.py` is invisible to the map guard"
    )


def test_every_python_package_has_a_readme() -> None:
    """Clicking a package on GitHub explains it without reading a single module — at any depth.

    This used to read `_tracked_directories(_PACKAGE)`, which is direct children only, so it
    asserted the promise for seventeen directories and made no claim about the fifty-two below
    them. `ARCHITECTURE.md` said the promise held for "a subpackage under `src/chemclaw/`" and
    both halves were "enforced, not requested" — a gate green while checking something narrower
    than the document beside it claims, which is the defect class this whole review kept finding.

    So the walk is recursive and the sentence in `ARCHITECTURE.md` now names *this* predicate: a
    directory holding Python modules. See `_python_packages` for why that is the line.
    """
    packages = _python_packages(_PACKAGE)
    assert packages, "no packages found under src/chemclaw — this test would assert nothing"
    missing = sorted(
        str(path.relative_to(_PACKAGE)) for path in packages if not (path / "README.md").is_file()
    )
    assert not missing, f"src/chemclaw/ packages with no README.md: {missing}"


# The one corpus that lives inside the package, and the argument for it. A benchmark's dataset is
# package data pinned to the surrogate that reads it — `objectives._reizman_suzuki` registers the
# fitted result under a name a `CampaignSpec` can carry, so swapping the file silently changes what
# that name means. `data/` is for corpora an operator configures, "each behind a `CHEMCLAW_*`
# setting", and this one must not be.
_CORPUS_IN_SRC = {"science/bo/benchmarks/data/reizman_suzuki_case_1.csv"}
# Extensions that carry a corpus rather than a declaration or an asset. `.yaml` is every manifest
# seam, `.md` is a README or a `SKILL.md`, and `api/static/`'s `.html`/`.js` are the front door's
# own page — none of them is data the code reads *as a dataset*.
_CORPUS_SUFFIXES = {".csv", ".tsv", ".parquet", ".jsonl", ".json", ".txt", ".sdf", ".smi"}


def test_no_corpus_lives_outside_data_except_the_one_that_is_argued() -> None:
    """`data/` holds every corpus the code reads at runtime — the direction nothing checked.

    `tests/test_deploy_chart.py` asserts the forward half (a directory the image COPYs exists) and
    nothing asserted the reverse, so a dataset could be dropped anywhere under `src/` and read with
    `Path(__file__).parent`. One already was, and this test exists because the honest resolution was
    to keep it and *name* it rather than to move it into a seam it does not fit
    (`D-2026-09-07-a-driver-with-no-caller-is-not-a-capability`): `data/vendored/` is a `DataSource`
    with a manifest contract — checksum, licence, `text_column` — and a benchmark's training grid is
    none of those things.

    An exception that is enumerated is a decision; an exception that is merely tolerated is how the
    next four arrive. So the set is exactly one file, and a second one fails here.
    """
    corpora = sorted(
        str(path.relative_to(_PACKAGE))
        for path in _PACKAGE.rglob("*")
        if path.is_file()
        and path.suffix in _CORPUS_SUFFIXES
        and not any(_is_cache(part) for part in path.relative_to(_PACKAGE).parts)
    )
    assert set(_CORPUS_IN_SRC) <= set(corpora), (
        f"the argued exception is no longer on disk: {sorted(set(_CORPUS_IN_SRC) - set(corpora))}. "
        "If it moved to `data/`, delete it from `_CORPUS_IN_SRC` and from ARCHITECTURE.md's rule."
    )
    unargued = sorted(set(corpora) - _CORPUS_IN_SRC)
    assert not unargued, (
        f"corpora under src/chemclaw/ with no argument: {unargued}. `data/` holds every corpus the "
        "code reads at runtime, each behind a `CHEMCLAW_*` setting; adding one here means adding "
        "the argument to ARCHITECTURE.md and the path to `_CORPUS_IN_SRC`."
    )


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

    It said "Six bundles" and listed six, omitting one entirely — while the same document, twenty
    lines later, explained where that bundle's job lived. A count in prose goes stale silently;
    this is the same claim in a form that fails loudly
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

    `calc`, `bo` and `results` each declare `jobs:`, so each runs a second Deployment for its own
    Temporal worker. The runbook said only `bo`, nine lines after calling `calc` "the worked
    example (five jobs, one workflow, one queue, its own worker)" — a document disagreeing with
    itself, which is what an unchecked claim looks like once someone edits half of it.

    `qm` was the fourth until `D-2026-08-26-semiempirical-is-the-whole-tier` removed the HPC/DFT
    tier, and this test is what caught the runbook paragraph still naming it — which is the whole
    point of pinning the set rather than trusting the prose.
    """
    durable = _bundles_owning_durable_work()
    assert durable == {"bo", "calc", "results"}, (
        f"the set of bundles owning durable work changed to {sorted(durable)}; update the runbook "
        "paragraph that names them, which is the claim this test exists to keep honest"
    )


def test_no_tracked_text_file_carries_an_unresolved_conflict_marker() -> None:
    """`<<<<<<<` in a committed file is invisible to every check that parses *structure*.

    `test_decision_log.py` already asserts this — and only over `docs/decisions/`, because that is
    where it was found the first time. Scoping a guard to the directory that produced the defect is
    what let the same defect sit on `main` in `docs/planning/DEFERRED.md`: three marker lines and a
    row duplicated on both sides of the conflict, while `test_deferred_register.py` passed over it,
    because that file checks what the rows *say* and a marker line is not a row.

    The lesson is the scope, not the file, so this asks git for every tracked text file and reads
    the lines rather than the shapes. It is the cheapest check available and the one that
    generalises: a conflict marker is never correct in any of them.
    """
    root = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split("\0")

    offenders = []
    for name in filter(None, tracked):
        path = root / name
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or a symlink into a tree this checkout does not have
        for number, line in enumerate(content.splitlines(), start=1):
            # `=======` needs the exact-match form: a Markdown setext rule is a run of `=` too, and
            # matching a prefix would fail this repository's own documents.
            if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "=======":
                offenders.append(f"{name}:{number}")

    assert not offenders, f"unresolved merge conflict markers in tracked files: {offenders}"


# A cardinal: a digit run or a number word. Used to reject counts written into prose that the tree
# already answers (D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose). "one" is deliberately
# absent — "one workflow, one queue" is a claim about *shape*, and the test below derives it.
_CARDINAL = (
    r"\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    r"|sixteen|seventeen|eighteen|nineteen|twenty"
)
_COUNTED_JOBS = re.compile(rf"\b(?:{_CARDINAL})\s+(?:\w+\s+)?jobs?\b", re.IGNORECASE)


def _calc_manifest_sentences() -> list[str]:
    """The `calc` manifest's comment prose, as sentences.

    The manifest's header comments are where this bundle explains itself to the next reader, so
    they are prose by every rule this repository applies to prose — and they are read here rather
    than in `connector-validate`, which checks the declaration and not the commentary around it.
    """
    manifest = (_PACKAGE / "connectors" / "calc" / "connector.yaml").read_text(encoding="utf-8")
    comments = " ".join(
        line.strip().lstrip("#").strip()
        for line in manifest.splitlines()
        if line.strip().startswith("#")
    )
    return [sentence.strip() for sentence in re.split(r"(?<=[.:])\s", comments) if sentence.strip()]


def test_the_calc_bundle_teaches_its_shape_without_counting_its_jobs() -> None:
    """The runbook said five jobs and one workflow; the manifest declared twelve.

    The sentence teaches something real and worth keeping: every job goes down *one* durable path,
    so adding a job is adding a typed member to a union rather than a second workflow. The count
    beside it taught nothing and went stale silently, in three places at once — the runbook, the
    manifest's own header, and the section comment introducing the fan-outs, which said "four" over
    five jobs.

    So the shape is derived from the manifest and the counts are refused. The failure that motivates
    the second half is not that a reader is misinformed about a number; it is that a document
    disagreeing with itself stops being read as authoritative at all.
    """
    manifest = yaml.safe_load(
        (_PACKAGE / "connectors" / "calc" / "connector.yaml").read_text(encoding="utf-8")
    )
    workflows = {job["workflow"] for job in manifest["jobs"]}
    assert workflows == {"CalcJobWorkflow"}, (
        f"calc's jobs no longer share one workflow ({sorted(workflows)}); the runbook and the "
        "manifest both teach that they do"
    )

    sentences = _calc_manifest_sentences()
    assert sentences, "the calc manifest carries no comment prose; the parse or the layout moved"
    shape = [sentence for sentence in sentences if "one workflow" in sentence]
    assert shape, "the manifest no longer states the one-workflow shape this test derives"
    counted = [sentence for sentence in shape if re.search(rf"\b(?:{_CARDINAL})\b", sentence, re.I)]
    assert not counted, (
        f"the one-workflow sentence counts the jobs sharing it: {counted}. The manifest declares "
        "them; a number here is a second answer that goes stale on its own."
    )
    miscounted = [sentence for sentence in sentences if _COUNTED_JOBS.search(sentence)]
    assert not miscounted, f"the calc manifest counts its own jobs in prose: {miscounted}"

    runbook = (_ROOT / "docs" / "guides" / "runbook.md").read_text(encoding="utf-8")
    worked_example = [
        paragraph
        for paragraph in runbook.split("\n\n")
        if "worked example (" in paragraph and "connectors/calc" in paragraph
    ]
    assert len(worked_example) == 1, "the runbook's calc worked-example paragraph has moved"
    assert not _COUNTED_JOBS.search(worked_example[0]), (
        "the runbook counts calc's jobs again; it declared twelve while the sentence said five"
    )


#: A tool surface, counted: "all fifteen tools", "Fifteen tools", "all fifteen of its tools".
#:
#: The quantifier is optional and does **not** discriminate a whole-surface claim from a subset
#: one — this comment used to say it did, and measured, making `all\s+` mandatory matches the two
#: live-lane scripts and misses `Fifteen tools, and not one of them computes anything.`, which is
#: the module docstring that actually went stale and the reason the scope has a package half at
#: all. So the pattern is deliberately broad: in scope, any cardinal immediately before "tools" is
#: refused, subset or whole. A subset count is the same second answer over a smaller set — "the
#: two tools pinned to the `xtb` binary" goes stale the day a third is — and the remedy is the
#: same one this rule asks for everywhere, which is to name them instead of counting them.
_COUNTED_SURFACE = re.compile(
    rf"\b(?:all\s+)?(?:{_CARDINAL})\b(?:\s+of\s+(?:its|the|them|these))?\s+tools?\b",
    re.IGNORECASE,
)


#: The window the count and the bundle's name have to share. It was one *sentence*, and a full
#: stop is the most ordinary edit a prose author makes: splitting the live-lane comment into
#: "…and its whole tool surface. All fifteen tools stay here." took the claim out of scope and the
#: guard was `1 passed` over the file that carried the original defect. The count and the subject
#: it is a count *of* are rarely one sentence apart on purpose, so the window is the paragraph —
#: a blank-line-delimited block, which in the shell scripts that carried the defect means a whole
#: run of comment lines, since a lone `#` is not a blank line. Measured over the tree, widening
#: from sentence to paragraph costs **zero** new offenders.
_PARAGRAPH = re.compile(r"\n\s*\n")

#: One sentence out of the paragraph, for the failure message — the paragraph is the *pairing*
#: window, not what a reader wants quoted back at them.
_SENTENCE = re.compile(r"(?<=[.:!?])\s")


def _quote(paragraph: str, counted: re.Match[str]) -> str:
    """The sentence inside `paragraph` that carries the count, flattened for a failure message."""
    for sentence in _SENTENCE.split(paragraph):
        if _COUNTED_SURFACE.search(sentence):
            return " ".join(sentence.split())[:120]
    return " ".join(counted.group(0).split())[:120]


def _calc_bundle_pattern(name: str) -> re.Pattern[str]:
    """A sentence naming the bundle, from the name its own manifest declares.

    Two things were spelled here and both are now derived, for the reason
    `_CALC_SURFACE_MODULE`'s comment gives one constant up: a spelled string is a claim about a
    tree that can change underneath it.

    The **name** comes from `connector.yaml`, which is where a bundle's name is declared and what
    `validate_connectors` holds the served surface against, so renaming the bundle carries this
    with it instead of silently emptying half the scope.

    The **phrasing** admits up to two modifiers between the name and "bundle", because the literal
    `` `calc` bundle `` was the other half of that claim. Driven: rewording every live mention to
    `` `calc` connector bundle `` and re-adding the original defect to `infra/live/processes.sh`
    was `1 passed` — the script that carried the defect teaching "all fifteen tools" again, out of
    scope because one word had been inserted. Measured over the tree, the modifier run costs
    **zero** new offenders and picks up one further carrier file.
    """
    return re.compile(rf"`?{re.escape(name)}`?(?:\s+[a-z][a-z-]*){{0,2}}?\s+bundle", re.IGNORECASE)


#: Records, not descriptions — and the axis is written out because it decides an asymmetry a
#: reviewer asked about. A merged ADR is never edited (CLAUDE.md), `docs/archive/` is
#: pre-implementation design, and `tasks/` is a dated account of an afternoon; each is *supposed*
#: to hold the number that was true when it was written. `tasks/lessons.md` is exempt with the
#: rest of that directory deliberately, not by oversight: CLAUDE.md does call it live, and it is
#: read at session start, but it is append-only and every entry is dated, which is the merged-ADR
#: argument rather than the runbook's.
#:
#: `docs/planning/BACKLOG.md` and `docs/guides/runbook.md` are deliberately *not* here, and a row
#: in either that quotes the historical sentence beside "calc bundle" is *meant* to red. A queue
#: whose closed rows are deleted in the commit that closes them, and a runbook describing what is
#: true today, are both read as current; either cites the ADR rather than re-transcribing the
#: number out of it, which is what the ADR is for.
_HISTORICAL = ("docs/archive/", "docs/decisions/", "tasks/")

#: The one live file that has to write the sentences this refuses, because it quotes them to say
#: what they are. It passed without this by accident — the docstring below wrote "its own bundle"
#: where the live-lane scripts wrote "its own `calc` bundle", so the guard was green on its own
#: wording and nothing warned the next editor; restoring that one word red the suite. Derived from
#: `__file__` rather than spelled out, so renaming this module carries the exemption with it.
#:
#: **What is exempt is the quotation, not the file, and it was the file.** The `#:` above argued a
#: narrow thing and the implementation skipped a thousand lines: driven, a *fresh* count of
#: "all seventeen tools" beside the bundle's name, in a different test's docstring in this
#: module, was `1 passed`. That is not hypothetical, it is history: `a0573397` existed
#: solely to hand-delete two such counts from this file, and nothing here would have caught them.
#: So the skip is per *match*: inside this file a counted surface is exempt only where the phrase
#: is enclosed in quotation marks, which is what quoting a sentence to say what it is looks like
#: and what writing a fresh one does not.
_NAMES_WHAT_IT_FORBIDS = (Path(__file__).resolve().relative_to(_ROOT).as_posix(),)

#: The quotation marks that make a count a quotation. ASCII and typographic, because this file is
#: read and edited by people who use both.
_OPENS, _CLOSES = '"\u201c', '"\u201d'


def _first_live_count(paragraph: str, quotations_are_exempt: bool) -> re.Match[str] | None:
    """The first counted surface in `paragraph`, skipping quoted ones where that is allowed."""
    for match in _COUNTED_SURFACE.finditer(paragraph):
        if not quotations_are_exempt:
            return match
        before = paragraph[match.start() - 1 : match.start()]
        after = paragraph[match.end() : match.end() + 1]
        if not (before in _OPENS and after in _CLOSES):
            return match
    return None


#: The module that *defines* the calc tool surface — the thing the package half of the scope is
#: about. Named as an import path and resolved to a file, rather than spelled as a path, because a
#: spelled path is an unanchored string: `_CALC_SERVER_PACKAGE` used to be one, the whole half went
#: empty when the package moved, and the fix for that was an `assert scanned_in_package` which is
#: an *any* basis — the package holds four tracked files and moving only `tools.py` out left the
#: other three satisfying it, driven, `1 passed` over a docstring counting the whole surface.
#:
#: Resolution is what closes that rather than a stronger assertion. `find_spec` returns the
#: module's origin without executing it, the scope is the directory that origin sits in, so a
#: rename *carries* the scope instead of emptying it — and a rename this constant does not follow
#: fails here by name instead of shrinking the scan in silence.
_CALC_SURFACE_MODULE = "chemclaw.connectors.calc.server.tools"


def _calc_bundle_name(surface_file: str) -> str:
    """The bundle's declared name, found by walking up from the module that serves it."""
    for parent in (_ROOT / surface_file).resolve().parents:
        manifest = parent / "connector.yaml"
        if manifest.is_file():
            declared = yaml.safe_load(manifest.read_text(encoding="utf-8"))["name"]
            return str(declared)
    raise AssertionError(
        f"no connector.yaml above {surface_file}. The bundle's name is read from its manifest "
        "rather than spelled here, so this scan cannot say which sentences are about it."
    )


def _calc_surface_file() -> str:
    """Where the calc tool surface is defined, as a repository-relative path."""
    spec = importlib.util.find_spec(_CALC_SURFACE_MODULE)
    assert spec is not None and spec.origin is not None, (
        f"{_CALC_SURFACE_MODULE} does not resolve to a file. It is the module that defines the "
        "calc tool surface and the package half of the prose scan's scope is derived from it, so "
        "this is the rename that would otherwise take that half of the scope away in silence. "
        "Point the constant at wherever the surface lives now."
    )
    return Path(spec.origin).resolve().relative_to(_ROOT).as_posix()


def test_the_calc_tool_surface_is_not_counted_in_prose() -> None:
    """Three live sentences counted a tool surface the manifest already declares.

    The bundle's own module docstring opened "Fifteen tools", and both live-lane scripts taught
    that Chemclaw3 keeps its own `calc` bundle and "all fifteen tools" — measured at HEAD,
    `connector.yaml` and the module agreed with each other and with neither sentence. Nothing
    failed, because nothing read those sentences: the count is a second answer to a question the
    manifest already answers, which is `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`
    over a tool surface instead of a `make` target.

    So the count is refused rather than corrected, and no number is reasserted here — the
    declared-equals-served half is already held, in both directions, by
    `tests/test_validate_connectors.py::test_the_shipped_bundles_pass_their_own_gate` over
    `validate_connectors`'s rule 5, and duplicating it here would be the same defect one layer up.

    Scope is derived rather than listed: the package holding the module that defines the surface —
    resolved through `find_spec` rather than spelled, so a rename carries the scope with it instead
    of emptying it — plus any live file naming the bundle in the same sentence as the count, which
    is how the identical sentence reached two scripts. Historical records are exempt because their
    job is to hold what was true when they were written.

    **Each half of that scope has a basis, and for one of them that is new.** The package half's
    basis used to be `assert scanned_in_package`, which is an *any* basis;
    The package holds four tracked files and one of them carries the surface, so moving `tools.py`
    out with the stale sentence restored was `1 passed` — half the scope going mostly empty, in
    silence, which is the failure the basis was added to refuse. A basis that merely counts what a
    scan read cannot tell a full scope from a rump one; what it has to name is the thing the scope
    is *about*.

    The bundle half had **no basis at all** — the identical hole, in the same function, on the half
    that was not being fixed. Driven: reword every live `` `calc` bundle `` to `` `calc` connector
    bundle `` and re-add the original defect to `infra/live/processes.sh`, and this was `1 passed`
    with a live-lane script teaching "all fifteen tools" again. Its basis is that some live file
    *outside* the package still names the bundle, because a match only inside it would be doing
    nothing the package half does not already do. That is an *any* basis and is said to be one:
    unlike the package half, whose subject is the one module that defines the surface, the files
    that discuss a bundle from outside are not a derivable set. What carries the weight instead is
    that the predicate itself is derived — the name from the manifest, and a modifier run so an
    inserted word is not an exemption.

    **The window the two have to share is a paragraph, and it was a sentence.** A full stop is the
    most ordinary edit a prose author makes, and it was an exemption: splitting the live-lane
    comment into "…and its whole tool surface. All fifteen tools stay here." was `1 passed` over
    the file that carried the original defect. Widening to the paragraph costs zero new offenders,
    measured; the quoted sentence in the failure message is still a sentence, because the paragraph
    is the pairing window rather than what a reader wants quoted back at them.

    **This module is exempt only where it quotes.** It has to write the sentences it refuses, to
    say what they are; the implementation of that was a whole-file skip, and a fresh count in
    another test's docstring here passed. `a0573397` existed solely to hand-delete two such counts
    from this file, which is the same defect already happening once. So the skip is per match and
    the condition is quotation marks around the phrase.
    """
    surface_file = _calc_surface_file()
    package = surface_file.rsplit("/", 1)[0] + "/"
    names_the_bundle = _calc_bundle_pattern(_calc_bundle_name(surface_file))
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")

    offenders = []
    scanned_in_package = []
    scanned_naming_the_bundle = []
    for name in filter(None, tracked):
        if name.startswith(_HISTORICAL):
            continue
        quoting = name in _NAMES_WHAT_IT_FORBIDS
        try:
            content = (_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or a symlink into a tree this checkout does not have
        if name.startswith(package):
            scanned_in_package.append(name)
        elif names_the_bundle.search(content) and not quoting:
            scanned_naming_the_bundle.append(name)
        for paragraph in _PARAGRAPH.split(content):
            counted = _first_live_count(paragraph, quoting)
            if not counted:
                continue
            if name.startswith(package) or names_the_bundle.search(paragraph):
                offenders.append(f"{name}: {_quote(paragraph, counted)}")

    assert surface_file in scanned_in_package, (
        f"{surface_file} defines the calc tool surface and this scan did not read it, although "
        f"{len(scanned_in_package)} other file(s) under {package} were read. It is "
        "untracked, or unreadable as text. An *any* basis over the package is what this replaced: "
        "the package holds four tracked files, only one carries the surface, and moving that one "
        "out left the other three satisfying the basis while a docstring counted the whole "
        "surface — half the scope going mostly empty, in silence, which is the shape of guard "
        "this test exists to refuse."
    )

    assert scanned_naming_the_bundle, (
        f"no live file outside {package} names the bundle any more, so the second half of this "
        "scan's scope is reading nothing. That half exists because the identical stale sentence "
        "reached two live-lane scripts, which are outside the package; a match only *inside* it "
        "would be doing nothing the package half does not already do. Either every external "
        "mention has been reworded past the pattern, or the bundle was renamed and its manifest "
        "was not — this is the half that had no basis at all while the other one was being given "
        "its second."
    )

    assert not offenders, (
        f"the calc bundle's tool surface is counted in prose: {offenders}. The manifest declares "
        "it and validate_connectors holds the declaration against what the module serves; a "
        "number here is a second answer that goes stale on its own, as 'fifteen' did."
    )


def _ci_validators() -> set[str]:
    """Every `*-validate` target `make ci` runs, read off the recipe itself."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    ci_line = next(line for line in makefile.splitlines() if line.startswith(("ci:", "ci ")))
    return {word for word in re.split(r"[\s:#]+", ci_line) if word.endswith("-validate")}


def test_both_documents_name_every_validator_the_gate_runs() -> None:
    """Both documents said "the eight validators" and listed eight; `make ci` runs nine.

    `sink-validate` — the newest, guarding the `sink.yaml` seam — was in neither list, so a reader
    who ran the eight believed they had run what CI runs. The same file that refuses to state a
    target count six lines earlier ("the one that was said 23 while the file held 28") stated this
    one; a count is not the problem, an *unchecked* count is.

    Derived from the `ci` recipe rather than from `make help`, because the claim both documents make
    is about the gate, and a validator that exists but is not wired into `ci` would be the more
    dangerous omission of the two.
    """
    validators = _ci_validators()
    assert len(validators) >= 8, f"the ci recipe no longer lists validators: {validators}"
    for document in ("README.md", "CLAUDE.md"):
        text = (_ROOT / document).read_text(encoding="utf-8")
        missing = sorted(name for name in validators if f"`{name}`" not in text)
        assert not missing, f"{document} does not name {missing}, which `make ci` runs"


def _science_engines() -> set[str]:
    """Every subpackage of `science/`, named by its directory."""
    return _tracked_directories(_PACKAGE / "science")


def test_the_connector_readme_lists_the_science_packages_that_exist() -> None:
    """The boundary against `science/` is only useful while it names the right packages.

    It listed `calc`, `bo`, `safety` and `fingerprints`: `science/safety` had been deleted with the
    hazard gate that justified it (`D-2026-08-15-safety-is-a-tool-not-a-gate`) and `science/labels`
    was missing — a map of a layer wrong in both directions, three lines under the heading that
    calls that boundary a rule. `ARCHITECTURE.md` had it right, so the tree carried two maps of one
    layer that disagreed, which is worse than one map.
    """
    engines = _science_engines()
    assert engines, "no science subpackages found; the layout moved"
    readme = (_PACKAGE / "connectors" / "README.md").read_text(encoding="utf-8")
    boundary = readme.split("## The boundary against", 1)
    assert len(boundary) == 2, "the section that names the science packages has been renamed"
    section = boundary[1].split("\n## ", 1)[0]
    missing = sorted(name for name in engines if f"`{name}`" not in section)
    assert not missing, f"connectors/README.md's science list does not name {missing}"


def test_no_shipped_document_names_a_connector_bundle_that_is_gone() -> None:
    """`agent/README.md` advertised "the QM/DFT job" as a bundle three weeks after its deletion.

    The `D-2026-08-26` sweep caught the runbook — because the test above pins that paragraph — and
    missed this README, which nothing pinned. A bundle name is the one part of such a sentence a
    machine can resolve, so every "`name` bundle" spelling in the documents a reader navigates by
    must be a directory that exists.

    Scoped to that spelling on purpose: it is the phrase that makes a *present-tense* claim about
    the capability surface, and it cannot fire on prose that merely mentions a word.
    """
    named = re.compile(r"`([a-z][a-z0-9_]*)` bundle\b")
    documents = [
        _ROOT / "README.md",
        _ROOT / "CLAUDE.md",
        _ROOT / "ARCHITECTURE.md",
        *sorted((_PACKAGE).rglob("README.md")),
        *sorted((_ROOT / "docs" / "guides").glob("*.md")),
    ]
    bundles = _bundles()
    assert bundles, "no connector bundles found; the glob or the layout moved"
    stale = sorted(
        {
            f"{path.relative_to(_ROOT)}: `{name}`"
            for path in documents
            if path.is_file()
            for name in named.findall(path.read_text(encoding="utf-8"))
            if name not in bundles
        }
    )
    assert not stale, f"documents naming a connector bundle that does not exist: {stale}"


def _makefile_targets_and_phony() -> tuple[list[str], set[str]]:
    """Every target the Makefile declares, and every name its `.PHONY` lines list.

    Multi-target rules (`a b:`) are read as the several targets they are, because a rule that
    declares two and is parsed as none is a silent hole in the check below. GNU's own special
    targets are excluded by their leading dot: `.DELETE_ON_ERROR` and `.SUFFIXES` are directives
    rather than recipes, and listing one in `.PHONY` would be meaningless — without this the first
    person to add the standard hardening line gets a failure whose only remedy is wrong.
    """
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    phony: set[str] = set()
    for match in re.finditer(r"^\.PHONY:((?:.*\\\n)*.*)$", makefile, re.MULTILINE):
        phony.update(match.group(1).replace("\\", " ").split())
    rule = re.compile(r"^([A-Za-z0-9_.%-]+(?:[ \t]+[A-Za-z0-9_.%-]+)*)[ \t]*::?(?![=])")
    targets: list[str] = []
    inside_define = False
    for line in makefile.splitlines():
        # `define`/`endef` bodies are verbatim text, not Makefile syntax. This one holds a Python
        # program whose `if not rules:` reads as a three-target rule to any regex that does not
        # know it is inside a block — which is exactly what happened the first time this parser
        # learned to read multi-target rules.
        if line.startswith("define "):
            inside_define = True
            continue
        if line.startswith("endef"):
            inside_define = False
            continue
        if inside_define:
            continue
        found = rule.match(line)
        if found:
            targets.extend(
                name
                for name in found.group(1).split()
                if not name.startswith(".") and "%" not in name
            )
    return list(dict.fromkeys(targets)), phony


def test_the_phony_list_and_the_target_list_are_the_same_list() -> None:
    """A `.PHONY` list maintained by hand is a second declaration of the target list.

    That is the same shape as every other drift this file checks, and it had drifted: seven of the
    Makefile's targets were missing from it — `live-ab`, the three `live-e2e-full-stack*` targets,
    `upstream-check`, `share-estimate` and `share-sync`, all added after the line was last touched.
    `make` treats a non-phony target as a recipe for a *file*, so the day anything creates a path
    named `live-ab` in the repository root, `make live-ab` reports "up to date", runs nothing, and
    exits zero. A target that silently does nothing is worse than a missing one.

    **Equality, not containment, and the first version of this test got that wrong in the way that
    mattered.** It asked only which targets were missing from `.PHONY` *and named no file on
    disk* — so it went green in precisely the scenario the paragraph above describes: measured,
    removing `share-sync` from `.PHONY` and running `touch share-sync` left the test passing while
    `make share-sync` printed "is up to date" and did nothing. Worse, ten root paths already
    collide with plausible target names (`docs`, `tests`, `src`, `infra`, `schema`, `data`,
    `skills`, `knowledge`, `tasks`, `Makefile`), so a future `docs:` target would have been
    exempted from the day it was written.

    Equality also closes the other direction the old form could not see — a stale `.PHONY` entry
    for a target that no longer exists — which is the drift this file checks in both directions
    everywhere else. Every target here is phony today (67 of them, matching `make help`), so the
    stricter form costs nothing: a real file target would need its own exemption *and* an argument,
    which is the conversation this failing should start.
    """
    targets, phony = _makefile_targets_and_phony()
    assert set(targets) == phony, (
        f"targets missing from .PHONY: {sorted(set(targets) - phony)}; "
        f".PHONY names with no target: {sorted(phony - set(targets))}"
    )


def _data_row() -> str:
    """`ARCHITECTURE.md`'s single `data/` row, which enumerates the corpora inline."""
    for line in (_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("| `data/` |"):
            return line
    raise AssertionError("ARCHITECTURE.md has no `data/` row")


def _data_readme_table() -> str:
    """Only the table rows of `data/README.md`.

    Its prose names two directories that are *not* under `data/` — `knowledge/` and `skills/`,
    deliberately at the root — so a whole-file scan would read the exception as a claim.
    """
    return "\n".join(
        line
        for line in (_ROOT / "data" / "README.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `")
    )


def test_both_maps_of_data_name_every_corpus_that_exists() -> None:
    """The one map tier in this tree that had no both-directions check, and it was wrong.

    `ARCHITECTURE.md`'s `data/` row and `data/README.md`'s table both listed five corpora against
    six on disk: `commitments/` was in neither, though it has its own README and its own setting
    (`CHEMCLAW_COMMITMENT_EXPORT_DIR`). Nothing caught it — `test_the_map_lists_every_directory…`
    checks top-level directories and `src/` subpackages, and
    `test_every_runtime_data_directory_actually_exists` walks `_RUNTIME_DATA`, which is the level
    *above*. So the corpus tier was mapped twice and checked never.

    The omission was the worst available one: `commitment_export_dir` exists precisely because that
    directory's absence is *silent* — "a wrong directory reached a project leader as a truthful
    empty portfolio" — so the corpus a reader most needs the map to mention is the one it left out.
    """
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


# The `assert` statements in `src/` that are **type narrowing** rather than invariant enforcement,
# each with the reason a reader needs to agree it belongs here. The distinction is the whole point:
# `python -O` deletes every `assert`, so one that *enforces* something is a control conditional on
# how an operator started the process, while one that merely tells mypy a value is not `None` loses
# nothing when it vanishes — the code after it was already correct or already broken.
#
# `Chemclaw3-mcp` states this rule outright for its serving code and holds it with a test
# (`D-2026-09-12-an-assert-is-a-control-with-an-off-switch`). This repository had no equivalent, and
# the one assert that carried a consequence — `operations/activity.py`'s guard on SQL built by
# `str.replace()` — sat among these three looking exactly like them.
#
# **The key is the assert's own source text, not its file, and the first version got that wrong.**
# Keyed by file, an allowlisted module could gain any number of further asserts — enforcing ones
# included — and this test stayed green, which re-creates the exact failure the paragraph above
# describes one file narrower: a consequential assert hiding among narrowing ones. The text is what
# was reviewed, so the text is what is allowed; editing one of these lines fails this test and asks
# for the argument again.
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
    """An invariant enforced by `assert` is a control with an off switch, and `-O` is the switch.

    This does not ban `assert` outright, because the three that remain genuinely narrow a type for
    mypy and nothing depends on them running. It bans a *fourth* appearing without an argument: a
    new one has to be justified in the same commit, which is the moment to notice the statement
    should have been `if ...: raise`.

    **Parsed rather than grepped**, and the allowlist is keyed by the statement's own text. The
    line regex this started as had both failure directions: a docstring line beginning with the
    word `assert` was a false positive, and `if x: assert y` or an `assert` after a `;` was a false
    negative. `ast` has neither, and it hands over the exact line for the message. Both of those
    were benign on the tree as it stood — which is the reason to fix them now rather than after a
    commit makes one of them matter.
    """
    found: dict[str, dict[str, int]] = {}
    for path in sorted((_ROOT / "src").rglob("*.py")):
        relative = path.relative_to(_ROOT / "src" / "chemclaw").as_posix()
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assert):
                found.setdefault(relative, {})[lines[node.lineno - 1].strip()] = node.lineno

    unargued = {
        name: {text: line for text, line in statements.items() if text not in argued}
        for name, statements in found.items()
        for argued in [_NARROWING_ASSERTS.get(name, {})]
    }
    unargued = {name: statements for name, statements in unargued.items() if statements}
    assert not unargued, (
        "an `assert` in src/ that is not in _NARROWING_ASSERTS: "
        f"{unargued}. `python -O` deletes it. If it narrows a type, add its exact text to the list "
        "with the reason; if it enforces anything at all, write `if ...: raise` instead."
    )

    stale = sorted(
        f"{name}: {text}"
        for name, statements in _NARROWING_ASSERTS.items()
        for text in statements
        if text not in found.get(name, {})
    )
    assert not stale, f"_NARROWING_ASSERTS names asserts that are no longer in src/: {stale}"
