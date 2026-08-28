"""The weekly mutation job's two self-checks, driven rather than read.

Both of the things this file pins were *stated* controls that could not act, and neither was
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
    so the only evidence left is the absent `.meta`.
    """
    dropped = "src/chemclaw/api/budget.py"
    stats = dict(_HEALTHY, total=750, killed=580)  # 77.3%, comfortably above the 72.0 floor
    result = _run_gate(_gate_workspace(tmp_path, stats=stats, missing=dropped))
    assert result.returncode != 0, result.stdout
    assert dropped in result.stdout + result.stderr
