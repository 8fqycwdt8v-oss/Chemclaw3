"""The weekly mutation job's self-checks, driven rather than read.

- The failure notification: `gh issue create --label` fails on a label that does not exist, while
  `gh issue list --label` answers empty, so the step runs against a `gh` stand-in that behaves
  the same way.
- The kill rate's population: mutmut silently yields nothing for a `source_paths` entry that is
  neither file nor directory, so a moved module can raise the rate; the gate step runs against a
  synthetic `mutants/` tree with one module's results missing.
- The copied tree: `also_copy` must contain everything the selected tests read (`_NOT_COPIED`).
- The floor's population: the `source_paths`, test selection and settings the floor was measured
  over are pinned beside it, and the `no_tests` share has its own ceiling.
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


# The `gh` CI actually has: `issue list --label` on a missing label is an empty search result, while
# `issue create --label` on one fails before any issue is made (gh resolves the label to a node id
# first).
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
    """A module moved without `pyproject.toml` following it fails the gate.

    The survivors' rate is above the floor, which is the trap; mutmut says nothing about the missing
    entry, so the absent `.meta` is the only evidence.
    """
    dropped = "src/chemclaw/api/budget.py"
    stats = dict(_HEALTHY, total=750, killed=559)  # 74.5%, comfortably above the floor
    result = _run_gate(_gate_workspace(tmp_path, stats=stats, missing=dropped))
    assert result.returncode != 0, result.stdout
    assert dropped in result.stdout + result.stderr


# The mutation run executes inside `mutants/`, built from `source_paths` plus `also_copy`. Nothing
# relates that list to the selected tests, so a test reading an uncopied file raises `SystemExit` in
# the stats phase, before any mutant is scored, and looks like mutmut being broken.
#
# Deliberately absent:
_NOT_COPIED: dict[str, str] = {
    # mutmut's own output tree — the destination of every copy above, so copying it into itself
    # would recurse. `make mutants` writes it and `.gitignore` hides it.
    "mutants": "the destination of the copy, not a source for it",
    # Claude Code's project settings and hooks; no test reads them.
    ".claude": "editor-harness configuration, read by no test",
}


def _effective_also_copy() -> list[str]:
    """`also_copy` as mutmut resolves it: ours plus the defaults upstream appends.

    Read through `Config`, the accessor `mutmut.__main__.copy_also_copy_files` uses, so upstream
    dropping a default shows up here.
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
    """Every top-level name git tracks, files, directories and dotfiles included.

    From git rather than `iterdir()`, which would add untracked working-tree directories such as
    `htmlcov/` or `venv/` that no CI checkout has.
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
    """Every tracked root entry is either copied into the run or declared absent.

    The copied set comes from mutmut's config loader and the entries from git, so adding either side
    alone fails. Files and dotfiles are in scope: tests read `Makefile`, `.env.example` and
    `.github` as well as directories such as `schema/`.
    """
    copied = {name.rstrip("/") for name in _effective_also_copy()}
    uncovered = sorted(_tracked_root_entries() - copied - set(_NOT_COPIED))
    assert not uncovered, (
        f"root entries missing from [tool.mutmut] also_copy: {uncovered}. A test the run selects "
        "that reads one of these fails the run before it scores anything. Add it to `also_copy`, "
        "or to `_NOT_COPIED` here with the reason it must not be copied."
    )


def test_an_exemption_cannot_claim_a_directory_the_copy_already_makes() -> None:
    """An exemption cannot name an entry the copy already makes.

    If both claim one entry, only one is right, and this says which pair to look at.
    """
    copied = {name.rstrip("/") for name in _effective_also_copy()}
    contradicted = sorted(copied & set(_NOT_COPIED))
    assert not contradicted, (
        f"declared absent from the mutation tree and copied into it anyway: {contradicted}"
    )
    assert all(reason.strip() for reason in _NOT_COPIED.values()), "an exemption needs its reason"


def test_a_selection_that_stopped_covering_a_module_fails_the_gate(tmp_path: Path) -> None:
    """A selection that stopped covering a module fails the gate on its `no_tests` share.

    A mutant no test reaches inflates `total` and depresses the rate without being a weak test, so
    it is gated separately.
    """
    # Categories must still sum to `total`, or the accounting check fires first. Taken out of
    # `killed`, which also puts the rate under the floor, as in the real case, so the gate must
    # report both.
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
#: A rate gate is stable only while its population is: widening the set of modules changes the rate
#: without any code getting worse. Adding a module here fails until `make mutants` is re-run and the
#: new floor recorded beside the new list, one diff a reviewer sees together.
_THE_POPULATION_THE_FLOOR_WAS_MEASURED_OVER = (
    "src/chemclaw/agent/audit_store.py",
    "src/chemclaw/agent/authz.py",
    "src/chemclaw/agent/loop_cap.py",
    "src/chemclaw/agent/plan_gate.py",
    "src/chemclaw/agent/skill_backend.py",
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
#: Pairing test files with the modules they cover moves the rate as much as the module list does, so
#: both tuples are pinned and must move together.
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
    "tests/test_loop_cap_floor.py",
    "tests/test_metrics_bridge.py",
    "tests/test_note.py",
    "tests/test_note_visibility.py",
    "tests/test_plan_gate.py",
    "tests/test_postgres_store.py",
    "tests/test_properties_core.py",
    "tests/test_publish_end_to_end.py",
    "tests/test_quantities.py",
    "tests/test_relations.py",
    "tests/test_runner.py",
    "tests/test_skill_backend.py",
    "tests/test_spend_cap.py",
    "tests/test_store.py",
    "tests/test_templates.py",
    "tests/test_tool_authz.py",
)

#: The other settings that move the rate without touching either list: `timeout_multiplier`
#: reclassifies mutants between `killed` and `timeout`, and the `only_mutate`/`do_not_mutate`
#: filters change which mutants exist. Pinned as values.
_THE_KNOBS_THE_FLOOR_WAS_MEASURED_UNDER = {
    "timeout_multiplier": 4.0,
    "only_mutate": [],
    "do_not_mutate": [],
}


def test_the_floor_is_pinned_to_the_population_it_was_measured_over() -> None:
    """The floor is pinned to the population it was measured over.

    Equality rather than a subset check, so a removal fails too; no input can change alone.
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
