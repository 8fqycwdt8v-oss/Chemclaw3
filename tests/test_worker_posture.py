"""A Temporal worker refuses to boot with sign-in off unless that posture is stated.

`D-2026-09-26-a-worker-states-its-unauthenticated-posture`: the front door's refusal reads a *bind*
(`api/middleware._refuse_unauthenticated_exposure`), and a worker binds no request surface, so a
deployment that forgot `CHEMCLAW_ENTRA_REQUIRED` started every worker with the shared dev principal
and every authorization gate open while its front door refused to boot. The guard is
`durable/serve.refuse_unauthenticated_worker`.

**Driven as processes, in the shape `tests/test_llm_gateway_guard.py` established and for its
reason**: a test asserting the guard is *called* is the test that passes throughout the defect. Each
refusal has a positive control that differs in one variable and must get further — to the broker,
which is dialled only after the guard, on a loopback port nothing serves. That the control reached
the broker is read off the output by that port, which nothing before `connect()` prints.

The entrypoints are derived, not listed: `test_every_worker_entrypoint_refuses_before_it_connects`
walks every `Worker(` construction in `src/`, so a third worker cannot join the unguarded half.
"""

from __future__ import annotations

import ast
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.durable.serve import refuse_unauthenticated_worker

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "chemclaw"

#: The phrase the refusal carries: the setting that proceeds, named.
_REFUSAL = "CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED=true"

#: A gateway that is not loopback, so the *other* boot guard in the background worker
#: (`core/llm_gateway`) is passed in every arm and cannot be what an arm observes. Never dialled.
_REAL_GATEWAY = "http://internal-llm.llm.svc:8000/v1"

#: A complete enforced identity, for the arm that turns sign-in on — `Settings` refuses
#: `entra_required` without an audience and a tenant, before any guard could run.
_SIGN_IN_ON = {
    "CHEMCLAW_ENTRA_REQUIRED": "true",
    "CHEMCLAW_ENTRA_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "CHEMCLAW_ENTRA_AUDIENCE": "api://chemclaw",
}

#: The two entrypoint kinds `deploy/entrypoint.sh` dispatches to a worker. `results` stands for
#: every `connector-worker-*`: they share `connectors/worker.run_bundle_worker`, and it is the one
#: whose import is lightest.
_WORKERS = ("chemclaw.durable.background_worker", "chemclaw.connectors.results.worker")


def _free_port() -> int:
    """A loopback port nothing is listening on, so a dial to it fails promptly."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(module: str, extra: dict[str, str]) -> tuple[str, str]:
    """Start a worker module and return what it said, plus the broker address it was given.

    Every inherited `CHEMCLAW_*` and proxy variable is scrubbed, for the reasons
    `tests/test_llm_gateway_guard._run` gives: the child must see the shipped defaults, and an
    ambient proxy would be refused at config import before any guard ran.
    """
    broker = f"127.0.0.1:{_free_port()}"
    environment = {
        key: value
        for key, value in os.environ.items()
        if "proxy" not in key.lower() and not key.startswith("CHEMCLAW_")
    }
    environment["PYTHONPATH"] = str(_REPO_ROOT / "src")
    environment["CHEMCLAW_LLM_BASE_URL"] = _REAL_GATEWAY
    environment["CHEMCLAW_TEMPORAL_ADDRESS"] = broker
    environment["CHEMCLAW_TEMPORAL_NAMESPACE"] = "posture-probe"
    environment.update(extra)
    completed = subprocess.run(
        [sys.executable, "-m", module],
        capture_output=True,
        text=True,
        env=environment,
        cwd=_REPO_ROOT,
        timeout=180,
    )
    return completed.stdout + completed.stderr, broker


@pytest.mark.timeout(300)
@pytest.mark.parametrize("module", _WORKERS)
def test_a_worker_with_sign_in_off_refuses_to_boot(module: str) -> None:
    """The row this closes, on the process it was open in: nothing stated, sign-in off."""
    said, broker = _run(module, {})
    assert _REFUSAL in said, said[-2000:]
    assert broker not in said, (
        f"the worker reached the broker before refusing, so it would have polled: {said[-2000:]}"
    )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("module", _WORKERS)
def test_a_worker_that_states_the_posture_boots_past_the_guard(module: str) -> None:
    """The positive control: one variable different, and the worker gets as far as the broker."""
    said, broker = _run(module, {"CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED": "true"})
    assert _REFUSAL not in said, said[-2000:]
    assert broker in said, f"the control arm did not reach the broker: {said[-2000:]}"


@pytest.mark.timeout(300)
@pytest.mark.parametrize("module", _WORKERS)
def test_a_worker_with_sign_in_on_boots_past_the_guard_without_the_flag(module: str) -> None:
    """Sign-in on is the shipped chart posture, and the flag must not be needed there."""
    said, broker = _run(module, _SIGN_IN_ON)
    assert _REFUSAL not in said, said[-2000:]
    assert broker in said, f"the enforced arm did not reach the broker: {said[-2000:]}"


def test_the_predicate_refuses_only_an_unstated_unauthenticated_worker(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """All four corners of the two booleans, in one place, so a swapped condition shows.

    The suite's autouse fixture states the posture, so each corner sets both values itself.
    """
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "worker_allow_unauthenticated", False)
    with pytest.raises(RuntimeError, match="CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED"):
        refuse_unauthenticated_worker()

    monkeypatch.setattr(settings, "worker_allow_unauthenticated", True)
    with caplog.at_level(logging.WARNING, logger="chemclaw.durable.serve"):
        refuse_unauthenticated_worker()
    assert any("gates OPEN" in record.getMessage() for record in caplog.records), (
        "an unauthenticated worker booted without saying so in its log"
    )

    caplog.clear()
    monkeypatch.setattr(settings, "entra_required", True)
    for stated in (False, True):
        monkeypatch.setattr(settings, "worker_allow_unauthenticated", stated)
        with caplog.at_level(logging.WARNING, logger="chemclaw.durable.serve"):
            refuse_unauthenticated_worker()
    assert not caplog.records, "a worker with sign-in on warned about a posture it is not in"


def test_the_shipped_default_is_to_refuse() -> None:
    """Fail-closed is the default, not a deployment's chore: the field ships False."""
    from chemclaw.core.config import Settings

    assert Settings.model_fields["worker_allow_unauthenticated"].default is False


def _calls_named(function: ast.AST, name: str) -> list[int]:
    """Line numbers of every call to `name` (bare or as an attribute) inside `function`."""
    lines = []
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            target = node.func
            called = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
            if called == name:
                lines.append(node.lineno)
    return lines


def test_every_worker_entrypoint_refuses_before_it_connects() -> None:
    """Derived from every `Worker(` in `src/`, so a new worker cannot skip the guard by omission.

    The function that builds a `Worker` is the entrypoint that would poll, and it must call the
    guard before its first `connect()` — after it, a refused worker has already opened a broker
    connection it did not need, and a guard below `Worker(...)` could be reached by nothing if the
    constructor raised first.
    """
    builders: list[tuple[Path, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _calls_named(node, "Worker"):
                continue
            builders.append((path, node.name))
            guards = _calls_named(node, "refuse_unauthenticated_worker")
            connects = _calls_named(node, "connect")
            assert guards, f"{path.relative_to(_REPO_ROOT)}::{node.name} builds a Worker unguarded"
            assert connects and min(guards) < min(connects), (
                f"{path.relative_to(_REPO_ROOT)}::{node.name} checks the posture after connecting"
            )
    # The universe must be the one the chart runs, or the loop above asserted nothing.
    assert {name for _path, name in builders} == {
        "main",
        "run_bundle_worker",
        "run_interactive_worker",
    }, builders
