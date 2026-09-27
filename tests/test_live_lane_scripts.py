"""The live lane's shell scripts, driven offline — each test is one defect a live run found.

`infra/live/processes.sh`, `bootstrap.sh` and `e2e-full-stack/up.sh` are only ever exercised by a
live bring-up, which CI does not do, so every defect in them has so far been found by the lane
failing in front of somebody. These run the scripts' own functions (or the whole script, with the
programs it would call stubbed on `PATH`) against a temporary directory, so the property each one
was fixed for is held by something that runs on every push. Found together by one four-repo run on
2026-09-27; each test's docstring names what that run saw.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.siblings import REPO_ROOT

_LIVE = REPO_ROOT / "infra/live"
_PROCESSES = _LIVE / "processes.sh"
_BOOTSTRAP = _LIVE / "bootstrap.sh"
_UP = _LIVE / "e2e-full-stack/up.sh"


def _clean_env(**extra: str) -> dict[str, str]:
    """The test process's environment minus every `CHEMCLAW_*`, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHEMCLAW_")}
    return env | extra


def _function(script: Path, name: str) -> str:
    """The source of shell function `name` in `script`, as text that defines it."""
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    assert match is not None, f"{script} has no `{name}()` function"
    return match.group(0)


def _bash(script: str, env: dict[str, str], cwd: Path | None = None) -> str:
    """Run `script` under `set -euo pipefail` and return its stdout; fail on a non-zero exit."""
    done = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


# ------------------------------------------------------------------------ processes.sh prelude


@pytest.fixture
def prelude(tmp_path: Path) -> Path:
    """`processes.sh` up to its first function, runnable on its own in a scratch lane directory.

    Everything above `mint_probe_token()` is the environment every verb inherits, which is where
    both the harness posture and the persisted lane environment are decided. Copied beside a copy
    of `siblings.sh`, because the script sources that relative to itself.
    """
    text = _PROCESSES.read_text(encoding="utf-8")
    head = text[: text.index("\nmint_probe_token() {")]
    live = tmp_path / "infra/live"
    live.mkdir(parents=True)
    shutil.copy(_LIVE / "siblings.sh", live / "siblings.sh")
    script = live / "prelude.sh"
    script.write_text(head + "\n", encoding="utf-8")
    return script


def _run_prelude(script: Path, tmp_path: Path, echo: str, **env: str) -> str:
    """Source the prelude with a scratch `CHEMCLAW_LIVE_DIR`, then run `echo`."""
    return _bash(
        f". {script}\n{echo}\n", _clean_env(CHEMCLAW_LIVE_DIR=str(tmp_path / ".live"), **env)
    )


def test_the_dev_posture_lane_states_execute(prelude: Path, tmp_path: Path) -> None:
    """Every durable job launched from a turn was refused in the dev lane.

    The `execute` statement lived inside the enforced-identity branch, and since the code default
    became harness-on + `plan_only` the dev posture inherited an approval gate with nobody to
    approve: `PlanNotApprovedError` on every launch, 0 `job_records` rows across 12 storm turns.
    """
    out = _run_prelude(prelude, tmp_path, 'echo "$CHEMCLAW_HARNESS_AUTONOMY"')
    assert out.strip() == "execute"
    overridden = _run_prelude(
        prelude,
        tmp_path,
        'echo "$CHEMCLAW_HARNESS_AUTONOMY"',
        CHEMCLAW_HARNESS_AUTONOMY="plan_only",
    )
    assert overridden.strip() == "plan_only", "the caller must still be able to run the gate"


def test_a_later_invocation_comes_up_in_the_lane_environment_the_caller_can_override(
    prelude: Path, tmp_path: Path
) -> None:
    """`processes.sh restart api` dropped mock-vendor, pyexec and the ELN/ORD sources.

    The four-repo lane's exports lived only in the shell that ran `up.sh`, and a restart runs in a
    fresh one. The persisted file is read back with the caller winning, and a line that is not an
    export is never executed.
    """
    run = tmp_path / ".live/run"
    run.mkdir(parents=True)
    canary = tmp_path / "executed"
    (run / "lane-env.sh").write_text(
        "export CHEMCLAW_DATA_SOURCES=graph,eln-json,eln-ord\n"
        "export CHEMCLAW_ELN_EXPORT_DIR=/a\\ path\\ with\\ spaces\n"
        f"touch {canary}\n",
        encoding="utf-8",
    )
    out = _run_prelude(
        prelude,
        tmp_path,
        'printf "%s|%s\\n" "$CHEMCLAW_DATA_SOURCES" "$CHEMCLAW_ELN_EXPORT_DIR"',
    )
    assert out.strip() == "graph,eln-json,eln-ord|/a path with spaces"
    assert not canary.exists(), "a non-export line in the lane file was executed"

    caller = _run_prelude(
        prelude, tmp_path, 'echo "$CHEMCLAW_DATA_SOURCES"', CHEMCLAW_DATA_SOURCES="graph"
    )
    assert caller.strip() == "graph"


def test_the_readiness_budget_is_a_setting(prelude: Path, tmp_path: Path) -> None:
    """`worker-bo` exceeded the fixed 300 attempts on a loaded host and the lane killed it."""
    text = _PROCESSES.read_text(encoding="utf-8")
    budget = text[text.index("readonly READY_ATTEMPTS=") : text.index("wait_for() {")]
    prelude.write_text(prelude.read_text(encoding="utf-8") + budget, encoding="utf-8")
    assert _run_prelude(prelude, tmp_path, 'echo "$READY_ATTEMPTS"').strip() == "300"
    assert (
        _run_prelude(
            prelude, tmp_path, 'echo "$READY_ATTEMPTS"', CHEMCLAW_LIVE_READY_ATTEMPTS="900"
        ).strip()
        == "900"
    )
    refused = subprocess.run(
        ["bash", "-c", f". {prelude}"],
        capture_output=True,
        text=True,
        env=_clean_env(
            CHEMCLAW_LIVE_DIR=str(tmp_path / ".live"), CHEMCLAW_LIVE_READY_ATTEMPTS="lots"
        ),
        check=False,
    )
    assert refused.returncode != 0 and "positive integer" in refused.stderr


# ------------------------------------------------------------------------ the fleet the lane binds


def _fleet_names(tmp_path: Path, **env: str) -> list[str]:
    """`processes.sh::fleet_bundle_names` against a scratch fleet publishing `chem` and `props`."""
    fleet = tmp_path / "fleet"
    for name in ("chem", "props"):
        (fleet / "manifests" / name).mkdir(parents=True, exist_ok=True)
        (fleet / "manifests" / name / "connector.yaml").write_text("name: x\n", encoding="utf-8")
    script = (
        f"{_function(_PROCESSES, 'fleet_bundle_names')}"
        f"REPO_ROOT={REPO_ROOT} MCP_REPO={fleet}\n"
        f"fleet_bundle_names {sys.executable}\n"
    )
    return _bash(script, _clean_env(**env), cwd=REPO_ROOT).split()


def test_the_lane_starts_no_fleet_server_the_front_door_does_not_bind(tmp_path: Path) -> None:
    """Five `default_enabled: false` servers were started and never called, reading as tested."""
    assert _fleet_names(tmp_path) == ["chem"]
    enabled = _fleet_names(tmp_path, CHEMCLAW_CONNECTORS_ENABLED="chem:props:bo")
    assert enabled == ["chem", "props"], "an enabled opt-in bundle must be started"


def test_the_four_repo_lane_enables_every_bundle_it_discovers() -> None:
    """The full-stack lane pays for the opt-in bundles, so it really tests them.

    Derived from the registry in `up.sh` rather than listed, and overridable like its neighbours.
    """
    body = _function(_UP, "up")
    assert "from chemclaw.connectors.registry import discovered" in body
    assert (
        'export CHEMCLAW_CONNECTORS_ENABLED="${CHEMCLAW_CONNECTORS_ENABLED:-$every_bundle}"' in body
    )


# ------------------------------------------------------------------------ up.sh's persisted env


def test_every_variable_up_exports_is_persisted_for_a_later_restart() -> None:
    """A variable `up` exports and the file omits is one a restarted process silently lacks."""
    body = _function(_UP, "up")
    exported = set(re.findall(r"^\s*export (CHEMCLAW_[A-Z0-9_]+)=", body, re.M))
    listed = re.search(r"^readonly LANE_ENV_VARS=\((.*?)\)", _UP.read_text(), re.M | re.S)
    assert listed is not None, f"{_UP} declares no LANE_ENV_VARS"
    missing = exported - set(listed.group(1).split())
    assert exported and not missing, f"`up` exports {sorted(missing)} and never persists them"
    assert "persist_lane_env" in body


def test_the_persisted_lane_env_round_trips_through_the_reader(
    prelude: Path, tmp_path: Path
) -> None:
    """Written by `up.sh`, read by `processes.sh`: odd values survive, unset ones stay absent."""
    text = _UP.read_text(encoding="utf-8")
    listed = re.search(r"^readonly LANE_ENV_VARS=\(.*?\)\n", text, re.M | re.S)
    assert listed is not None
    live = tmp_path / ".live"
    writer = (
        "log() { :; }\n"
        f"LIVE_DIR={live}\n"
        f"{listed.group(0)}{_function(_UP, 'persist_lane_env')}"
        "persist_lane_env\n"
    )
    _bash(
        writer,
        _clean_env(CHEMCLAW_CONNECTORS_DIR="/x:/y z", CHEMCLAW_LLM_API_KEY="k'$(true)"),
    )
    written = live / "run/lane-env.sh"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600, "it can hold the gateway credential"
    assert "CHEMCLAW_LLM_BASE_URL" not in written.read_text(), "an unset variable was written"
    out = _run_prelude(
        prelude,
        tmp_path,
        'printf "%s|%s\\n" "$CHEMCLAW_CONNECTORS_DIR" "$CHEMCLAW_LLM_API_KEY"',
    )
    assert out.strip() == "/x:/y z|k'$(true)"


# ------------------------------------------------------------------------ the UI's dependencies


@pytest.mark.parametrize(
    ("layout", "stale"),
    [
        ("no-install", True),
        ("installed-current", False),
        ("lockfile-newer", True),
        ("node-modules-without-record", True),
    ],
)
def test_the_ui_install_follows_the_lockfile_not_the_directory(
    tmp_path: Path, layout: str, stale: bool
) -> None:
    """`node_modules` existing skipped every install after the first, however the lockfile moved."""
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "package.json").write_text("{}", encoding="utf-8")
    lock = ui / "package-lock.json"
    lock.write_text("{}", encoding="utf-8")
    if layout != "no-install":
        (ui / "node_modules").mkdir()
    record = ui / "node_modules/.package-lock.json"
    if layout in ("installed-current", "lockfile-newer"):
        record.write_text("{}", encoding="utf-8")
        older, newer = (lock, record) if layout == "installed-current" else (record, lock)
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(ui / "package.json", (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
    script = (
        f"UI_REPO={ui}\n{_function(_UP, 'ui_dependencies_stale')}"
        "if ui_dependencies_stale; then echo stale; else echo current; fi\n"
    )
    assert _bash(script, _clean_env()).strip() == ("stale" if stale else "current")


# ------------------------------------------------------------------------ bootstrap.sh


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A one-commit repository standing in for the developer's checkout."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=repo)
    return repo


def _ensure_note_repo(tmp_path: Path, checkout: Path) -> tuple[Path, Path]:
    clone, bare = tmp_path / ".live/knowledge-repo", tmp_path / ".live/knowledge-origin.git"
    script = (
        "log() { :; }\n"
        f"REPO_ROOT={checkout} NOTE_REPO_DIR={clone} NOTE_ORIGIN_DIR={bare}\n"
        f"{_function(_BOOTSTRAP, 'ensure_note_origin')}{_function(_BOOTSTRAP, 'ensure_note_repo')}"
        "ensure_note_repo\n"
    )
    _bash(script, _clean_env())
    return clone, bare


def _push_a_note(clone: Path) -> None:
    (clone / "note.md").write_text("an agent note\n", encoding="utf-8")
    _git("add", "note.md", cwd=clone)
    _git("-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qm", "note", cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)


def test_a_note_push_never_lands_in_the_developer_checkout(
    tmp_path: Path, source_repo: Path
) -> None:
    """The note writer's clone had the working checkout as its origin, so pushes landed there."""
    before = _git("rev-parse", "main", cwd=source_repo)
    clone, bare = _ensure_note_repo(tmp_path, source_repo)
    assert _git("remote", "get-url", "origin", cwd=clone) == str(bare)
    _push_a_note(clone)
    assert _git("rev-parse", "main", cwd=source_repo) == before
    assert _git("rev-parse", "main", cwd=bare) == _git("rev-parse", "HEAD", cwd=clone)


def test_a_clone_made_under_the_old_rule_is_re_pointed_with_its_notes(
    tmp_path: Path, source_repo: Path
) -> None:
    """A lane bootstrapped before the fix still pushed to the checkout on every later run."""
    clone = tmp_path / ".live/knowledge-repo"
    clone.parent.mkdir(parents=True)
    _git("clone", "-q", str(source_repo), str(clone))
    (clone / "old.md").write_text("a note from before\n", encoding="utf-8")
    _git("add", "old.md", cwd=clone)
    _git("-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qm", "old", cwd=clone)
    before = _git("rev-parse", "main", cwd=source_repo)

    clone, bare = _ensure_note_repo(tmp_path, source_repo)
    assert _git("remote", "get-url", "origin", cwd=clone) == str(bare)
    _push_a_note(clone)
    assert _git("rev-parse", "main", cwd=source_repo) == before
    assert "old.md" in _git("ls-tree", "--name-only", "main", cwd=bare)


@pytest.fixture
def listening_port() -> Iterator[int]:
    """A TCP port something is accepting on — a broker, as far as a port check can tell."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server.getsockname()[1]


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="runs the non-root path")
def test_bootstrap_adopts_services_that_already_answer_instead_of_provisioning(
    tmp_path: Path, listening_port: int
) -> None:
    """The native branch built pgvector and the Temporal CLI and made a cluster unconditionally.

    So a host already serving Postgres and Temporal on the configured ports died on a missing
    `pg_config`, `go` or server headers it never needed. Every provisioning tool is stubbed to
    record its call and fail; the run must succeed without calling one.
    """
    stubs, calls = tmp_path / "bin", tmp_path / "calls"
    stubs.mkdir()
    _stub(stubs, "docker", "exit 1")
    _stub(stubs, "pg_config", f'[ "$1" = --bindir ] && echo {stubs}')
    _stub(stubs, "pg_isready", "exit 0")
    _stub(
        stubs,
        "psql",
        'case "$*" in *pg_database*) echo 1 ;; *extversion*) echo 0.8.6 ;; esac',
    )
    for tool in ("temporal", "go", "make", "initdb", "pg_ctl", "createdb"):
        _stub(stubs, tool, f"echo {tool} >>{calls}; exit 1")
    live = tmp_path / ".live"
    clone = live / "knowledge-repo"
    clone.mkdir(parents=True)
    _git("init", "-q", cwd=clone)
    git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    env = _clean_env(
        PATH=f"{stubs}:{git_dir}:/usr/bin:/bin",
        CHEMCLAW_LIVE_DIR=str(live),
        CHEMCLAW_LIVE_TEMPORAL_PORT=str(listening_port),
    )
    done = subprocess.run(
        ["bash", str(_BOOTSTRAP), "up"], capture_output=True, text=True, env=env, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert not calls.exists(), f"provisioned anyway: {calls.read_text()}"
    assert "adopting" in done.stdout
