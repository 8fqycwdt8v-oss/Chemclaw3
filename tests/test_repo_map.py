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
