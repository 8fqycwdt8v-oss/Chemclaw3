"""The weekly mutation job's self-checks, driven rather than read.

The first two things this file pins were *stated* controls that could not act, and neither was
visible from the workflow's own prose:

- **the notification.** The failure step files an issue under a `mutation-testing` label that does
  not exist in the repository. `gh issue create` resolves label names to node ids before it issues
  the mutation, so the step died with `could not add label: 'mutation-testing' not found` and no
  issue was filed — while `gh issue list --label` routes through search and answers empty with exit
  0 for an unknown label, so the dedup branch never matched either. A weekly job whose failure
  notification is itself broken is the defect the schedule was added to fix, one step along.
- **the coverage of the kill rate.** The gate divides `killed` by `total` and never asks *which*
  mutants are in `total`. mutmut's `walk_all_files` falls through to `walk(path)` for a
  `source_paths` entry that is neither a file nor a directory, which yields nothing, silently — so
  a module moved without `pyproject.toml` following it leaves the run mutating six modules instead
  of seven, and the rate usually goes *up*, because the aggregate loses a below-average module.

Neither is checkable by reading the YAML for a string: what matters is what the shell and the
Python in it *do*. So the notification step runs against a `gh` stand-in that refuses an unknown
label exactly as the real one does, and the gate step runs against a synthetic `mutants/` tree with
one module's results missing.

The third is not about the workflow but about whether the run can start at all: `also_copy` builds
the tree the run executes in, and the selected tests read files from it. See `_NOT_COPIED` below.

The fourth is the gate's own numbers. A kill rate is a claim about a population, and the recorded
floor outlived two widenings of it — so `_THE_POPULATION_THE_FLOOR_WAS_MEASURED_OVER` pins the
`source_paths` the floor was measured against, and the `no_tests` ceiling is gated beside it
because a mutant nothing reaches depresses the rate without being a weak test.
"""

import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "mutants.yml"


def _steps() -> dict[str, Any]:
    """The workflow's steps by name — the unit each test below drives."""
    workflow: Any = yaml.safe_load(_WORKFLOW.read_text())
    return {step["name"]: step for step in workflow["jobs"]["mutants"]["steps"] if "name" in step}


def _script(step_name: str) -> str:
    """The shell body of one named step."""
    return str(_steps()[step_name]["run"])


def _heredoc(script: str) -> str:
    """The Python inside a `python - <<'PY' ... PY` step."""
    body = script.split("<<'PY'\n", 1)[1]
    return body.rsplit("PY", 1)[0]


# The `gh` this repository's CI actually has: `issue list --label` on a label that does not exist is
# an empty *search* result rather than an error, and `issue create --label` on one is a hard
# failure before any issue is made. Reproduced against gh 2.63.2 and 2.82.1 before being written
# down here; both generations resolve the label to a node id first.
_FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_GH_STATE"])
data = json.loads(state.read_text())
argv = sys.argv[1:]


def save() -> None:
    state.write_text(json.dumps(data))


def option(name):
    return argv[argv.index(name) + 1] if name in argv else None


if argv[:2] == ["label", "create"]:
    name = argv[2]
    if name in data["labels"] and "--force" not in argv:
        sys.exit(f"label already exists: {name}")
    if name not in data["labels"]:
        data["labels"].append(name)
    save()
    sys.exit(0)

if argv[:2] == ["issue", "list"]:
    # Search semantics: an unknown label matches nothing and is not an error.
    label = option("--label")
    print("\\n".join(str(n) for n in data["issues"] if label in data["labels"]))
    sys.exit(0)

if argv[:2] == ["issue", "create"]:
    label = option("--label")
    if label is not None and label not in data["labels"]:
        sys.exit(f"could not add label: '{label}' not found")
    data["issues"].append(len(data["issues"]) + 1)
    save()
    sys.exit(0)

if argv[:2] == ["issue", "comment"]:
    data["comments"].append(argv[2])
    save()
    sys.exit(0)

sys.exit(f"fake gh does not implement {argv!r}")
"""


@pytest.fixture
def fake_gh(tmp_path: Path) -> Path:
    """A `gh` on `PATH` carrying this repository's real label set, and a state file to read back."""
    state = tmp_path / "gh-state.json"
    # `bug` and `dependencies` exist in 8fqycwdt8v-oss/Chemclaw3; `mutation-testing` does not.
    state.write_text(json.dumps({"labels": ["bug", "dependencies"], "issues": [], "comments": []}))

    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir()
    binary.write_text(_FAKE_GH)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return state


def _run_notification(state: Path) -> subprocess.CompletedProcess[str]:
    """The failure step, run the way a runner runs it: `bash -e`, with `gh` on `PATH`."""
    env = dict(os.environ)
    env["PATH"] = f"{state.parent / 'bin'}{os.pathsep}{env['PATH']}"
    env["FAKE_GH_STATE"] = str(state)
    env["GH_TOKEN"] = "not-a-real-token"
    env["RUN_URL"] = "https://example.invalid/run/1"
    return subprocess.run(
        ["bash", "-e", "-c", _script("File an issue when the run reports something")],
        capture_output=True,
        text=True,
        env=env,
        cwd=state.parent,
    )


def test_the_failure_notification_files_an_issue_with_the_label_it_asks_for(fake_gh: Path) -> None:
    """The whole point of the step: a red run leaves an issue behind, not just a red run."""
    result = _run_notification(fake_gh)
    assert result.returncode == 0, result.stderr
    assert json.loads(fake_gh.read_text())["issues"] == [1], result.stderr


def test_a_second_failure_comments_on_the_open_issue_instead_of_filing_another(
    fake_gh: Path,
) -> None:
    """Dedup is what keeps three red weeks from being three issues — it needs the label to exist."""
    assert _run_notification(fake_gh).returncode == 0
    second = _run_notification(fake_gh)
    assert second.returncode == 0, second.stderr

    state = json.loads(fake_gh.read_text())
    assert state["issues"] == [1]
    assert state["comments"] == ["1"]


def _gate_workspace(tmp_path: Path, *, stats: dict[str, int], missing: str | None) -> Path:
    """A `mutants/` tree as `make mutants` leaves one, optionally with a module's results absent."""
    source_paths = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["mutmut"][
        "source_paths"
    ]
    (tmp_path / "pyproject.toml").write_text(
        "[tool.mutmut]\nsource_paths = " + json.dumps(source_paths) + "\n"
    )
    for path in source_paths:
        if path == missing:
            continue
        meta = tmp_path / "mutants" / (path + ".meta")
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text("{}")
    stats_path = tmp_path / "mutants" / "mutmut-cicd-stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(json.dumps(stats))
    return tmp_path


_HEALTHY = {
    "total": 825,
    "killed": 634,
    "survived": 155,
    "no_tests": 34,
    "timeout": 2,
    "suspicious": 0,
    "segfault": 0,
}


def _run_gate(workspace: Path) -> subprocess.CompletedProcess[str]:
    """The gate step's Python, run in `workspace` with the floor the workflow declares."""
    step = _steps()["Gate on the kill rate, on the coverage, and on the harness having worked"]
    env = dict(os.environ) | {str(k): str(v) for k, v in step["env"].items()}
    return subprocess.run(
        [sys.executable, "-"],
        input=_heredoc(str(step["run"])),
        capture_output=True,
        text=True,
        env=env,
        cwd=workspace,
    )


def test_a_run_that_mutated_every_declared_module_passes(tmp_path: Path) -> None:
    """The control: the gate is not merely refusing everything."""
    result = _run_gate(_gate_workspace(tmp_path, stats=_HEALTHY, missing=None))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_source_path_that_stopped_resolving_fails_the_gate(tmp_path: Path) -> None:
    """A module moved without `pyproject.toml` following it: six modules scored, gate must not pass.

    The rate on the survivors is *higher* than the recorded floor here — that is the trap. mutmut
    yields nothing for an entry that is neither a file nor a directory and says nothing about it,
    so the only evidence left is the absent `.meta`. No floor value is named in this comment: the
    one that was named went stale in the commit that moved the floor, two functions below.
    """
    dropped = "src/chemclaw/api/budget.py"
    stats = dict(_HEALTHY, total=750, killed=559)  # 74.5%, comfortably above the floor
    result = _run_gate(_gate_workspace(tmp_path, stats=stats, missing=dropped))
    assert result.returncode != 0, result.stdout
    assert dropped in result.stdout + result.stderr


# The mutation run executes inside `mutants/`, a tree built by copying: `source_paths` for the
# modules being mutated, `also_copy` for everything else. Nothing relates that list to the tests
# the run selects, so widening the selection can import a file the copy never made — which is a
# `SystemExit` in the *stats* phase, before a single mutant is scored, and reads as mutmut being
# broken rather than as a missing directory.
#
# Deliberately absent, and the only one:
_NOT_COPIED: dict[str, str] = {
    # mutmut's own output tree — the destination of every copy above, so copying it into itself
    # would recurse. `make mutants` writes it and `.gitignore` hides it.
    "mutants": "the destination of the copy, not a source for it",
}


def _effective_also_copy() -> list[str]:
    """`also_copy` as mutmut resolves it — ours plus the defaults upstream appends to it.

    Read through `Config`, which is the accessor `mutmut.__main__.copy_also_copy_files` itself
    uses, rather than by restating upstream's defaults here: `tests/`, `pyproject.toml` and the
    lock files are copied because upstream appends them, and the day it stops this test is what
    says so.
    """
    probe = (
        "import json\n"
        "from mutmut.configuration import Config\n"
        "Config.ensure_loaded()\n"
        "print(json.dumps([str(p) for p in Config.get().also_copy]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-"], input=probe, capture_output=True, text=True, cwd=_ROOT
    )
    assert result.returncode == 0, result.stderr
    return list(json.loads(result.stdout.strip().splitlines()[-1]))


def _tracked_root_entries() -> set[str]:
    """Every top-level name git tracks — files and directories, dotfiles included.

    From git rather than `iterdir()`, and both halves of that matter. `iterdir()` misses nothing
    but *adds* whatever the working tree happens to hold: `.gitignore` already anticipates
    `htmlcov/`, `build/`, `dist/`, `coverage`, `site`, `target/` and `venv/`, and any of them
    present would red this guard for a directory no CI checkout has. Git also gives the dotfiles
    and the root files, which `iterdir()` would have handed over and the first version of this
    guard then filtered away — see the docstring below for why that mattered.
    """
    listed = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=True,
    )
    return {name.strip() for name in listed.stdout.splitlines() if name.strip()}


def test_every_tracked_root_entry_is_either_copied_into_the_run_or_declared_absent() -> None:
    """A root entry the repository has and the mutation tree does not is a run that cannot start.

    Driven rather than read: the copied set comes from mutmut's own config loader and the entries
    come from git, so adding either side without the other is what goes red. `schema` is why this
    exists — `tests/test_publish_end_to_end.py` joined the selection, reached `cli/sink_schema.ddl`,
    and globbed `schema/result-store/*.sql` inside a `mutants/` that had no `schema/` at all.

    **Files and dotfiles are in scope, and the first version of this guard filtered both out.**
    Driven then: removing `Makefile`, `.env.example` or `.github` from `also_copy` left it green,
    because it only looked at non-dot directories — and `tests/test_logging.py` and
    `tests/test_audit.py`, one of them a file the same change added to the selection, both already
    mention `.env.example`. The trigger case was itself a file glob that happened to bottom out in
    a directory.
    """
    copied = {name.rstrip("/") for name in _effective_also_copy()}
    uncovered = sorted(_tracked_root_entries() - copied - set(_NOT_COPIED))
    assert not uncovered, (
        f"root entries missing from [tool.mutmut] also_copy: {uncovered}. A test the run selects "
        "that reads one of these fails the run before it scores anything. Add it to `also_copy`, "
        "or to `_NOT_COPIED` here with the reason it must not be copied."
    )


def test_an_exemption_cannot_claim_a_directory_the_copy_already_makes() -> None:
    """An exemption that is *also* copied is a reason nobody will re-read when it stops holding.

    `mutants/` cannot exist after this: the exemption above says copying it would recurse, and if
    a future `also_copy` names it anyway, exactly one of the two is right and this says which
    pair to look at.
    """
    copied = {name.rstrip("/") for name in _effective_also_copy()}
    contradicted = sorted(copied & set(_NOT_COPIED))
    assert not contradicted, (
        f"declared absent from the mutation tree and copied into it anyway: {contradicted}"
    )
    assert all(reason.strip() for reason in _NOT_COPIED.values()), "an exemption needs its reason"


def test_a_selection_that_stopped_covering_a_module_fails_the_gate(tmp_path: Path) -> None:
    """A mutant nothing reaches is a hole in the selection, and the kill rate cannot see it.

    Such a mutant is neither killed nor survived-under-test: it inflates `total` and depresses the
    rate without saying why. Measured at the config this workflow ran on before 2026-09-22, 27.7%
    of 3,640 mutants were in that state and the rate read 44.3% — a number that looks like badly
    tested code and was a selection that named six test files too few.
    """
    # Categories must still sum to `total`, or the new accounting check fires first and says
    # something true but different. Taken out of `killed`, which also puts the rate under the
    # floor — deliberately, because that is what the real case looks like and the gate must now
    # report *both*.
    stats = dict(_HEALTHY, no_tests=200, killed=468)  # 24.2% of 825, well over the ceiling
    result = _run_gate(_gate_workspace(tmp_path, stats=stats, missing=None))
    assert result.returncode != 0, result.stdout
    reported = result.stdout + result.stderr
    assert "no selected test" in reported
    # Both, not the first one reached: the canonical failure trips the rate as well, and exiting
    # on the rate alone is what would report the uninformative half.
    assert "below the recorded floor" in reported, reported


#: The `source_paths` the recorded floor was measured over, pinned beside it.
#:
#: **A rate gate is only stable while its population is, and this one was not.** 72.0 was measured
#: over 825 mutants and survived two widenings of `source_paths` to a population of 3,640, where
#: the same code scores 62.1% — so the floor was not a standard the config had fallen short of, it
#: was a number about a different set of modules, and the first run that completed would have
#: failed on it. The workflow's own comment argues for a *rate* because "a count breaks the first
#: time one of these modules legitimately grows"; that is true and incomplete, because a rate
#: breaks the first time the *set* of modules grows.
#:
#: So adding a module here reds this test until somebody re-runs `make mutants` and writes the new
#: floor beside the new list. Two edits in one file that a reviewer sees as one diff, which is the
#: shape `tests/test_compound_identity.py` uses to pin `STANDARDIZATION_VERSION`.
_THE_POPULATION_THE_FLOOR_WAS_MEASURED_OVER = (
    "src/chemclaw/agent/audit_store.py",
    "src/chemclaw/agent/authz.py",
    "src/chemclaw/agent/spend_cap.py",
    "src/chemclaw/api/budget.py",
    "src/chemclaw/api/runner_trace.py",
    "src/chemclaw/core/chem.py",
    "src/chemclaw/core/fulltext.py",
    "src/chemclaw/core/logging.py",
    "src/chemclaw/core/quantities.py",
    "src/chemclaw/kg/git_writer.py",
    "src/chemclaw/kg/note.py",
    "src/chemclaw/kg/record.py",
    "src/chemclaw/publish/outbox.py",
    "src/chemclaw/science/calc/store.py",
    "src/chemclaw/templates/resolve.py",
)


#: The test selection the recorded floor was measured over, pinned beside it.
#:
#: **This is the input that actually moved the number, and the first version of this pin left it
#: out.** With `source_paths` byte-identical, pairing six test files with the modules they cover
#: moved the rate 44.3% -> 62.1% and the no-test share 27.7% -> 12.3%. A pin on `source_paths`
#: alone holds the lever that did not move and leaves the one that did unguarded; the `no_tests`
#: ceiling is a backstop for it, but it is an 87-minute weekly one rather than a gate.
_THE_SELECTION_THE_FLOOR_WAS_MEASURED_OVER = (
    "tests/test_audit.py",
    "tests/test_audit_store.py",
    "tests/test_authz.py",
    "tests/test_budget.py",
    "tests/test_compound_identity.py",
    "tests/test_concurrency_claims.py",
    "tests/test_disconnect_teardown.py",
    "tests/test_fulltext.py",
    "tests/test_knowledge.py",
    "tests/test_logging.py",
    "tests/test_metrics_bridge.py",
    "tests/test_note.py",
    "tests/test_note_visibility.py",
    "tests/test_postgres_store.py",
    "tests/test_properties_core.py",
    "tests/test_publish_end_to_end.py",
    "tests/test_quantities.py",
    "tests/test_relations.py",
    "tests/test_runner.py",
    "tests/test_spend_cap.py",
    "tests/test_store.py",
    "tests/test_templates.py",
    "tests/test_tool_authz.py",
)

#: The other two settings that move the rate without touching either list above.
#:
#: `timeout_multiplier` reclassifies mutants between `killed` and `timeout` — 68 of them on the
#: measured run, 1.9 points of the rate — and the `only_mutate`/`do_not_mutate` filters change
#: which mutants exist at all. Neither is a population in the sense the two tuples above are, so
#: they are pinned as values rather than enumerated.
_THE_KNOBS_THE_FLOOR_WAS_MEASURED_UNDER = {
    "timeout_multiplier": 4.0,
    "only_mutate": [],
    "do_not_mutate": [],
}


def test_the_floor_is_pinned_to_the_population_it_was_measured_over() -> None:
    """Changing what is mutated, or what runs against it, changes what the rate means.

    Every direction, so no edit can be made alone: the literals above are what was in
    `pyproject.toml` when 62.1% was measured, and the floor in the workflow is what that
    measurement produced. Equality rather than a subset check, so a *removal* reds too.
    """
    mutmut = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["mutmut"]
    assert sorted(mutmut["source_paths"]) == sorted(_THE_POPULATION_THE_FLOOR_WAS_MEASURED_OVER), (
        "[tool.mutmut].source_paths changed, so the kill rate is over a different population and "
        "the recorded floor in .github/workflows/mutants.yml is a number about the old one. "
        "Re-run `make mutants`, write the new floor and the new list together, and say in the "
        "commit message what the rate moved from and to"
    )
    selection = mutmut["pytest_add_cli_args_test_selection"]
    assert sorted(selection) == sorted(_THE_SELECTION_THE_FLOOR_WAS_MEASURED_OVER), (
        "[tool.mutmut].pytest_add_cli_args_test_selection changed, which moves the kill rate "
        "without changing a line of source: it decides which mutants any test reaches at all. "
        "Re-run `make mutants` and write the new numbers beside the new list"
    )
    for knob, value in _THE_KNOBS_THE_FLOOR_WAS_MEASURED_UNDER.items():
        assert mutmut.get(knob, value) == value, (
            f"[tool.mutmut].{knob} changed, which moves the rate without changing a test. "
            "Re-measure before trusting the recorded floor"
        )
    gate = _steps()["Gate on the kill rate, on the coverage, and on the harness having worked"]
    assert gate["env"]["MUTATION_SCORE_FLOOR"] == "57.0", (
        "the recorded floor moved. Update the list above with it, or say beside the number which "
        "measurement it came from"
    )
    assert gate["env"]["MUTATION_NO_TESTS_CEILING"] == "16.0", (
        "the no-test ceiling moved; the same rule applies to it"
    )
