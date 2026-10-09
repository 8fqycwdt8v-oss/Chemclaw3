"""The live lane's shell scripts, driven offline.

`infra/live/processes.sh`, `bootstrap.sh` and `e2e-full-stack/up.sh` otherwise run only in a live
bring-up, which CI does not do. These run the scripts' own functions (or the whole script, with
its programs stubbed on `PATH`) against a temporary directory, so each property is checked on
every push.
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
from typing import Any

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

    Everything above `mint_probe_token()` is the environment every verb inherits. Copied beside
    `siblings.sh`, which the script sources relative to itself.
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
    """The dev-posture lane sets `execute`, so durable jobs launched from a turn are not refused.

    Without it the dev posture inherits a `plan_only` approval gate with nobody to approve, and
    every launch raises `PlanNotApprovedError`.
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
    """A later invocation (e.g. `restart api`) comes up in the persisted lane environment.

    The persisted file is read back with the caller's own values winning, and a line that is not an
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


# ------------------------------------------------------------------------ the interactive workers


def _interactive_names(**env: str) -> list[str]:
    """`processes.sh::interactive_worker_names` against this checkout's own registry."""
    script = (
        f"{_function(_PROCESSES, 'interactive_worker_names')}"
        f"interactive_worker_names {sys.executable}\n"
    )
    return _bash(script, _clean_env(**env), cwd=REPO_ROOT).split()


def _shipped_queueing_bundles(*, opted_in: frozenset[str] = frozenset()) -> list[str]:
    """Read independently of the registry: the bundles, here or the fleet's, that list `queued:`."""
    import chemclaw_contracts
    import yaml

    manifests = [
        *(REPO_ROOT / "src/chemclaw/connectors").glob("*/connector.yaml"),
        *chemclaw_contracts.manifests_dir().glob("*/connector.yaml"),
    ]
    names = []
    for manifest in sorted(manifests, key=lambda path: path.parent.name):
        body = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        on = body.get("default_enabled", True) or manifest.parent.name in opted_in
        if on and (body.get("endpoint") or {}).get("queued"):
            names.append(manifest.parent.name)
    return names


def test_the_lane_starts_an_interactive_worker_for_every_bundle_that_queues_tools() -> None:
    """The lane starts an interactive worker for every bundle that queues tools.

    Without a poller on `connector-<name>-interactive`, every queued call waits out its inline wait
    and becomes a job nothing runs. The set is derived from the enabled manifests, checked here
    against a direct reading of the YAML.
    """
    expected = _shipped_queueing_bundles()
    assert expected, "no shipped bundle queues a tool; this test would pass on an empty loop"
    assert sorted(_interactive_names()) == expected
    enabled = ":".join([*_shipped_queueing_bundles(opted_in=frozenset({"kinetics"}))])
    with_opt_in = sorted(_interactive_names(CHEMCLAW_CONNECTORS_ENABLED=enabled))
    assert "kinetics" in with_opt_in, "an enabled opt-in bundle's queue was left unpolled"


def test_the_lane_runs_the_interactive_worker_the_chart_runs() -> None:
    """The same module, under the same component name, as `deploy/entrypoint.sh`'s case."""
    module = "python -m chemclaw.connectors.interactive_worker"
    entrypoint = (REPO_ROOT / "deploy/entrypoint.sh").read_text(encoding="utf-8")
    assert "interactive-worker-*)" in entrypoint and module in entrypoint
    body = _function(_PROCESSES, "up")
    assert 'start_worker "interactive-worker-$name"' in body
    assert '-m chemclaw.connectors.interactive_worker "$name"' in body


def test_the_front_door_starts_only_after_every_worker_polls() -> None:
    """The front door starts only after every worker is ready and polling the broker.

    Otherwise the front door reports a bundle such as `bo` as unpolled on a cold start.
    """
    body = _function(_PROCESSES, "up")
    polled = body.index('wait_for_pollers "$python" "${workers[@]}"')
    assert polled < body.index("start api ")
    assert body.index('wait_for "$worker"') < polled


def _wait_for_pollers(tmp_path: Path, address: str, *workers: str, attempts: int = 30) -> Any:
    """Run `processes.sh::wait_for_pollers` against the broker at `address`."""
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    for worker in workers:
        (run / f"{worker}.pid").write_text(str(os.getpid()), encoding="utf-8")
    script = (
        'die() { echo "$*" >&2; exit 1; }\n'
        f"RUN_DIR={run} LIVE_DIR={tmp_path} READY_ATTEMPTS={attempts}\n"
        f"{_function(_PROCESSES, 'wait_for_pollers')}"
        f"wait_for_pollers {sys.executable} {' '.join(workers)}\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(CHEMCLAW_TEMPORAL_ADDRESS=address),
        cwd=REPO_ROOT,
        check=False,
    )


def test_wait_for_pollers_passes_on_a_polled_queue_and_names_an_unpolled_one(
    tmp_path: Path,
) -> None:
    """Against a real broker: a worker polling its queue passes, a queue nobody polls fails.

    The queue is derived from the worker's name — here `interactive-worker-fixture` →
    `connector-fixture-interactive` — which is the convention the lane names its workers by.
    """
    import asyncio

    from temporalio.worker import Worker

    from chemclaw.connectors.queued_call import call_queued_tool
    from chemclaw.connectors.queued_workflow import QueuedToolWorkflow
    from tests.temporal_env import start_local_env_or_skip

    async def _measure() -> tuple[Any, Any]:
        env = await start_local_env_or_skip()
        async with env:
            address = env.client.service_client.config.target_host
            async with Worker(
                env.client,
                task_queue="connector-fixture-interactive",
                workflows=[QueuedToolWorkflow],
                activities=[call_queued_tool],
            ):
                polled = await asyncio.to_thread(
                    _wait_for_pollers, tmp_path, address, "interactive-worker-fixture"
                )
                unpolled = await asyncio.to_thread(
                    _wait_for_pollers, tmp_path, address, "worker-ghost", attempts=2
                )
            return polled, unpolled

    polled, unpolled = asyncio.run(_measure())
    assert polled.returncode == 0, polled.stderr
    assert "connector-fixture-interactive" in polled.stdout
    assert unpolled.returncode != 0
    assert "worker-ghost (connector-ghost)" in unpolled.stderr


def test_wait_for_pollers_reports_a_dead_worker_at_once(tmp_path: Path) -> None:
    """A worker whose pid is gone is named, not waited out for the whole budget."""
    import asyncio

    from tests.temporal_env import start_local_env_or_skip

    async def _measure() -> Any:
        env = await start_local_env_or_skip()
        async with env:
            address = env.client.service_client.config.target_host
            run = tmp_path / "run"
            run.mkdir()
            # A pid no process holds: above every default `pid_max`.
            (run / "worker-bo.pid").write_text("4194305", encoding="utf-8")
            script = (
                'die() { echo "$*" >&2; exit 1; }\n'
                f"RUN_DIR={run} LIVE_DIR={tmp_path} READY_ATTEMPTS=300\n"
                f"{_function(_PROCESSES, 'wait_for_pollers')}"
                f"wait_for_pollers {sys.executable} worker-bo\n"
            )
            return await asyncio.to_thread(
                subprocess.run,
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=_clean_env(CHEMCLAW_TEMPORAL_ADDRESS=address),
                cwd=REPO_ROOT,
                check=False,
                timeout=60,
            )

    done = asyncio.run(_measure())
    assert done.returncode != 0
    assert "worker-bo exited before polling connector-bo" in done.stderr


# ------------------------------------------------------------------------ up.sh's persisted env


def _listed(name: str) -> set[str]:
    """The names in `up.sh`'s `readonly <name>=( … )` array."""
    listed = re.search(rf"^readonly {name}=\((.*?)\)", _UP.read_text(), re.M | re.S)
    assert listed is not None, f"{_UP} declares no {name}"
    return set(listed.group(1).split())


def test_every_variable_up_exports_is_persisted_for_a_later_restart() -> None:
    """A variable `up` exports and the file omits is one a restarted process silently lacks.

    Unless it is one the restarting shell decides — the model gateway — which is listed apart, so
    leaving it out is a stated choice rather than an omission this test cannot tell from one.
    """
    body = _function(_UP, "up")
    exported = set(re.findall(r"^\s*export (CHEMCLAW_[A-Z0-9_]+)=", body, re.M))
    persisted, per_invocation = _listed("LANE_ENV_VARS"), _listed("LANE_ENV_PER_INVOCATION")
    missing = exported - persisted - per_invocation
    assert exported and not missing, f"`up` exports {sorted(missing)} and never persists them"
    assert not persisted & per_invocation, "a name cannot be both persisted and per-invocation"
    assert "persist_lane_env" in body


def test_the_gateway_and_its_key_are_never_persisted() -> None:
    """The gateway settings and its key are never persisted to `lane-env.sh`.

    Otherwise a restart from a shell naming no gateway would reload the paid gateway from disk, and
    the credential would sit in plain text.
    """
    gateway = {"CHEMCLAW_LLM_BASE_URL", "CHEMCLAW_LLM_MODEL", "CHEMCLAW_LLM_API_KEY"}
    assert gateway <= _listed("LANE_ENV_PER_INVOCATION")
    assert not gateway & _listed("LANE_ENV_VARS")


def test_a_gateway_an_older_file_persisted_is_not_read_back(prelude: Path, tmp_path: Path) -> None:
    """A lane started by the old `up.sh` holds the gateway in its file; a restart must ignore it.

    Restarted with nothing named, the front door resolves the mock (`processes.sh` starts it on the
    default address); restarted with a gateway named, the caller's own value is the one used.
    """
    run = tmp_path / ".live/run"
    run.mkdir(parents=True)
    (run / "lane-env.sh").write_text(
        "export CHEMCLAW_LLM_BASE_URL=https://paid.example/api/v1\n"
        "export CHEMCLAW_LLM_MODEL=paid/model\n"
        "export CHEMCLAW_LLM_API_KEY=sk-on-disk\n"
        "export CHEMCLAW_DATA_SOURCES=graph,eln-json\n",
        encoding="utf-8",
    )
    echo = (
        'printf "%s|%s|%s|%s\\n" "${CHEMCLAW_LLM_BASE_URL-unset}" "${CHEMCLAW_LLM_MODEL-unset}" '
        '"${CHEMCLAW_LLM_API_KEY-unset}" "$CHEMCLAW_DATA_SOURCES"'
    )
    assert _run_prelude(prelude, tmp_path, echo).strip() == "unset|unset|unset|graph,eln-json"
    named = _run_prelude(
        prelude,
        tmp_path,
        echo,
        CHEMCLAW_LLM_BASE_URL="https://named.example/v1",
        CHEMCLAW_LLM_MODEL="named/model",
        CHEMCLAW_LLM_API_KEY="from-the-shell",
    )
    assert named.strip() == "https://named.example/v1|named/model|from-the-shell|graph,eln-json"


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
        _clean_env(
            CHEMCLAW_CONNECTORS_DIR="/x:/y z",
            CHEMCLAW_PYEXEC_TOKEN="k'$(true)",
            CHEMCLAW_LLM_API_KEY="sk-must-not-land",
            CHEMCLAW_LLM_BASE_URL="https://paid.example/api/v1",
        ),
    )
    written = live / "run/lane-env.sh"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600, "it holds the connector tokens"
    assert "CHEMCLAW_CALC_TOKEN" not in written.read_text(), "an unset variable was written"
    assert "CHEMCLAW_LLM_" not in written.read_text(), "the gateway reached the file"
    assert "sk-must-not-land" not in written.read_text()
    out = _run_prelude(
        prelude,
        tmp_path,
        'printf "%s|%s\\n" "$CHEMCLAW_CONNECTORS_DIR" "$CHEMCLAW_PYEXEC_TOKEN"',
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
    """A lane clone made under the old remote rule is re-pointed, keeping its notes."""
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
    """Bootstrap adopts Postgres and Temporal that already answer instead of provisioning them.

    Every provisioning tool is stubbed to record its call and fail; the run must succeed without
    calling one.
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


# ------------------------------------------------------------------------ the configured backends


def _backend_address(name: str, **env: str) -> str:
    """`processes.sh::backend_address` for `name`, against this checkout's own settings."""
    script = f"{_function(_PROCESSES, 'backend_address')}backend_address {sys.executable} {name}\n"
    return _bash(script, _clean_env(**env), cwd=REPO_ROOT).strip()


def test_every_configured_backend_is_addressed_by_the_setting_its_client_reads() -> None:
    """Every configured backend is started at the address its client reads.

    The set is a literal (`CONFIGURED_BACKENDS`) because those manifests are not discoverable by
    core; each address is derived from the `<name>_server_url` / `<name>_server_token_env` pair,
    read from settings independently and with a moved URL so the port is not a transcription.
    """
    from chemclaw.core.config import settings

    listed = re.search(r"^readonly CONFIGURED_BACKENDS=\((.*?)\)", _PROCESSES.read_text(), re.M)
    assert listed is not None, f"{_PROCESSES} declares no CONFIGURED_BACKENDS"
    backends = listed.group(1).split()
    assert {"calc", "rxnlabel"} <= set(backends), backends
    for name in backends:
        port = re.search(r":(\d+)/", getattr(settings, f"{name}_server_url"))
        assert port is not None
        token_var = getattr(settings, f"{name}_server_token_env")
        assert _backend_address(name) == f"{port.group(1)} {token_var}"
    moved = _backend_address("rxnlabel", CHEMCLAW_RXNLABEL_SERVER_URL="http://127.0.0.1:18865/mcp")
    assert moved == "18865 CHEMCLAW_RXNLABEL_TOKEN"


def _start_backend(tmp_path: Path, name: str, *, served: bool = False, **env: str) -> Any:
    """Drive `processes.sh::start_backend` with the launch, the poll and the address stubbed."""
    fleet = tmp_path / "fleet"
    fleet.mkdir(exist_ok=True)
    script = (
        'die() { echo "DIE $*"; exit 1; }\n'
        "running() { return 1; }\n"
        f"curl() {{ return {0 if served else 1}; }}\n"
        'start() { echo "START $* IN $PWD"; }\n'
        'wait_for() { echo "WAIT $*"; }\n'
        f"MCP_REPO={fleet}\n"
        "BACKEND_TOKEN_VARS=()\n"
        f"{_function(_PROCESSES, 'backend_address')}{_function(_PROCESSES, 'start_backend')}"
        f"start_backend {name} {sys.executable} /fleet/python\n"
        'echo "TOKENS ${BACKEND_TOKEN_VARS[*]} = ${CHEMCLAW_RXNLABEL_TOKEN:-unset}"\n'
    )
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script],
        capture_output=True,
        text=True,
        env=_clean_env(**env),
        cwd=REPO_ROOT,
        check=False,
    )


def test_the_labeller_starts_from_the_fleet_on_its_configured_port_with_both_token_halves(
    tmp_path: Path,
) -> None:
    """The labeller starts on the port the background worker dials, sharing one token.

    The same variable is what the worker sends and what the fleet's `app.py` verifies, so it is
    defaulted once here and a caller's own value wins.
    """
    done = _start_backend(tmp_path, "rxnlabel")
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == (
        f"START rxnlabel /fleet/python -m uvicorn chemclaw_mcp_rxnlabel.app:app "
        f"--host 127.0.0.1 --port 8865 IN {tmp_path / 'fleet'}"
    )
    assert lines[1] == "WAIT rxnlabel http://127.0.0.1:8865/healthz"
    assert lines[2] == "TOKENS CHEMCLAW_RXNLABEL_TOKEN = dev-token"

    own = _start_backend(tmp_path, "rxnlabel", CHEMCLAW_RXNLABEL_TOKEN="mine")
    assert own.stdout.splitlines()[-1] == "TOKENS CHEMCLAW_RXNLABEL_TOKEN = mine"


def test_a_backend_port_another_process_serves_is_refused_by_name(tmp_path: Path) -> None:
    """The collision guard `calc` had, carried to every configured backend."""
    done = _start_backend(tmp_path, "rxnlabel", served=True)
    assert done.returncode != 0
    assert "DIE rxnlabel: 127.0.0.1:8865 is already served" in done.stdout
    assert "START" not in done.stdout


def test_the_backends_come_up_before_the_workers_that_dial_them_and_their_tokens_persist() -> None:
    """The background worker inherits the labeller's token only if it starts after the export.

    And a second shell — `processes.sh env`, the devenv's restart — reads the same token back, or
    it 401s against a labeller that is plainly up.
    """
    body = _function(_PROCESSES, "up")
    backends = body.index('start_backend "$backend" "$python" "$fleet_python"')
    assert backends < body.index("start_worker worker-background")
    assert 'for backend in "${CONFIGURED_BACKENDS[@]}"; do' in body
    assert 'for token_var in $(bearer_token_vars "$python") "${BACKEND_TOKEN_VARS[@]}"; do' in body
    assert backends < body.index('> "$RUN_DIR/connector-env.sh"')


# ------------------------------------------------------------------------ up.sh's index step


def _index_corpus(
    tmp_path: Path, env: dict[str, str] | None = None, uv_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run `up.sh::index_corpus` with `uv` stubbed to record its call and print a report."""
    stubs, calls = tmp_path / "bin", tmp_path / "uv-calls"
    stubs.mkdir(exist_ok=True)
    _stub(
        stubs,
        "uv",
        f'echo "$* | NOTE_REPO=$CHEMCLAW_NOTE_REPO_DIR" >>{calls}\n'
        'echo "some log line"\n'
        'echo "Fingerprints: reaction fingerprints: 3 re-fingerprinted"\n'
        'echo "Labels: reaction-labels-lane-drain: labelled 7 reaction(s)"\n'
        f"exit {uv_exit}",
    )
    live = tmp_path / ".live"
    live.mkdir(exist_ok=True)
    script = (
        'log() { echo "LOG $*"; }\n'
        'die() { echo "DIE $*"; exit 1; }\n'
        f"REPO_ROOT={REPO_ROOT} LIVE_DIR={live}\n"
        f"{_function(_UP, 'index_corpus')}"
        "index_corpus\n"
        'echo "RETURNED"\n'
    )
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + script],
        capture_output=True,
        text=True,
        env=_clean_env(PATH=f"{stubs}:{os.environ['PATH']}", **(env or {})),
        check=False,
    ), calls


def test_the_bring_up_runs_the_index_step_bounded_and_on_the_lanes_own_note_clone(
    tmp_path: Path,
) -> None:
    """The bring-up runs the index step once per `up`, bounded, on the lane's own note clone.

    It passes on a timeout and points at `.live/knowledge-repo`, because an unset `note_repo_dir` is
    this checkout. Its summary lines reach the bring-up log.
    """
    done, calls = _index_corpus(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert calls.read_text().splitlines() == [
        "run python -m chemclaw.cli.live_index --timeout 600 "
        f"| NOTE_REPO={tmp_path / '.live/knowledge-repo'}"
    ]
    assert "LOG   Fingerprints: reaction fingerprints: 3 re-fingerprinted" in done.stdout
    assert "LOG   Labels: reaction-labels-lane-drain: labelled 7 reaction(s)" in done.stdout
    assert "some log line" not in done.stdout, "the full report belongs in its log file"
    assert "some log line" in (tmp_path / ".live/e2e-corpus-index.log").read_text()

    bounded, calls = _index_corpus(
        tmp_path, {"CHEMCLAW_LIVE_INDEX_TIMEOUT": "45", "CHEMCLAW_NOTE_REPO_DIR": "/elsewhere"}
    )
    assert bounded.returncode == 0
    assert calls.read_text().splitlines()[-1] == (
        "run python -m chemclaw.cli.live_index --timeout 45 | NOTE_REPO=/elsewhere"
    )


def test_a_quick_up_skips_the_index_step(tmp_path: Path) -> None:
    """`CHEMCLAW_LIVE_SKIP_INDEX=true` keeps a bring-up that needs no precedent answers quick."""
    done, calls = _index_corpus(tmp_path, {"CHEMCLAW_LIVE_SKIP_INDEX": "true"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert not calls.exists(), "the index step ran although it was skipped"
    assert "index step skipped" in done.stdout and "RETURNED" in done.stdout


def test_a_failed_index_step_warns_and_the_bring_up_goes_on(tmp_path: Path) -> None:
    """Non-fatal, like the backfill: every process is up, and the tools say what state they are in.

    The summary lines are still shown, because a failure in one index says nothing about the other.
    """
    done, _ = _index_corpus(tmp_path, uv_exit=1)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "WARNING: the index step reported a failure (exit 1)" in done.stdout
    assert "LOG   Labels: reaction-labels-lane-drain: labelled 7 reaction(s)" in done.stdout
    assert done.stdout.rstrip().endswith("RETURNED")


@pytest.mark.parametrize(
    ("name", "value", "said"),
    [
        ("CHEMCLAW_LIVE_INDEX_TIMEOUT", "ten", "must be a positive integer"),
        ("CHEMCLAW_LIVE_INDEX_TIMEOUT", "0", "must be a positive integer"),
        ("CHEMCLAW_LIVE_SKIP_INDEX", "maybe", "must be true or false"),
    ],
)
def test_a_malformed_index_setting_is_refused_before_anything_runs(
    tmp_path: Path, name: str, value: str, said: str
) -> None:
    """A typo in either setting is named, never read as "skip" or as an unbounded wait."""
    done, calls = _index_corpus(tmp_path, {name: value})
    assert done.returncode != 0
    assert said in done.stdout
    assert not calls.exists()


def test_the_index_step_runs_after_the_backfill_and_the_labeller_credential_is_checked() -> None:
    """After the backfill, so the drain sees the rows it started; the credential, like `calc`'s."""
    body = _function(_UP, "up")
    assert body.index("backfill_corpus") < body.index("index_corpus") < body.index("full stack up")
    assert re.search(r"assert_credential_accepted rxnlabel \"http://127\.0\.0\.1:8865/mcp\"", body)


def test_restarting_the_labeller_is_sent_to_the_script_that_owns_it(tmp_path: Path) -> None:
    """`up.sh restart rxnlabel` names `processes.sh` instead of killing a pidfile it never wrote."""
    script = (
        'die() { echo "DIE $*"; exit 1; }\n'
        f"REPO_ROOT={REPO_ROOT} MCP_REPO={tmp_path} RUN_DIR={tmp_path}\n"
        f"{_function(_UP, 'restart')}"
        "restart rxnlabel\n"
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=_clean_env(), check=False
    )
    assert done.returncode != 0
    assert "bash infra/live/processes.sh restart rxnlabel" in done.stdout
