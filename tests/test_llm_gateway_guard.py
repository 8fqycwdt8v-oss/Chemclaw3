"""The gateway boot guard, driven as processes rather than asserted as a call.

Every process kind that can make a model call must refuse the shipped loopback gateway at boot,
not only the front door. The arms start real processes (`background_worker`, `mcp_face`,
`cli.chat`) and read what they do. Each has a positive control naming a real gateway, which must
then fail on the next step instead; both arms of a pair point Temporal or the service port at
something unreachable, so the gateway address is the only difference.

Proxy variables are scrubbed from every child, because `core.netguard.arm_from_settings` would
otherwise abort every arm before the guard under test ran, with a refusal that reads alike.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.llm_gateway import refuse_unconfigured_llm_gateway

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "chemclaw"

#: The phrase the guard's refusal carries, and the one every arm keys off.
_REFUSAL = "CHEMCLAW_LLM_BASE_URL names a loopback address"

#: A gateway address that is not loopback. Never dialled by anything here — the guard only reads it
#: — but it has to be a real host name, because `core.netguard` parses it to derive the allowlist.
_REAL_GATEWAY = "http://internal-llm.llm.svc:8000/v1"


def _free_port() -> int:
    """A port nothing is listening on, so a dial to it fails promptly rather than hanging."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(module: str, *, gateway: str | None, extra: dict[str, str], args: list[str]) -> str:
    """Start `module` as a process and return everything it said before exiting.

    `gateway=None` leaves `CHEMCLAW_LLM_BASE_URL` unset, i.e. the shipped loopback default.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if "proxy" not in key.lower() and not key.startswith("CHEMCLAW_")
    }
    environment["PYTHONPATH"] = str(_REPO_ROOT / "src")
    environment.update(extra)
    if gateway is not None:
        environment["CHEMCLAW_LLM_BASE_URL"] = gateway
    completed = subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        text=True,
        env=environment,
        cwd=_REPO_ROOT,
        timeout=180,
    )
    return completed.stdout + completed.stderr


# --------------------------------------------------------------------------- the background worker


@pytest.fixture(scope="module")
def unreachable_temporal() -> dict[str, str]:
    """A loopback Temporal address nothing serves: the step a worker reaches after the guard.

    Loopback because `core.netguard` permits it, so the control fails with a refused connection
    rather than an egress refusal that could be mistaken for the guard.
    """
    return {
        "CHEMCLAW_TEMPORAL_ADDRESS": f"127.0.0.1:{_free_port()}",
        "CHEMCLAW_TEMPORAL_NAMESPACE": "guard-probe",
        # Sign-in is off in these children, and a worker refuses that unless it is stated
        # (`tests/test_worker_posture.py` drives that guard). Stated here so the only difference
        # between the two arms stays the gateway address.
        "CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED": "true",
    }


@pytest.mark.timeout(300)
def test_a_worker_on_the_dev_gateway_refuses_to_boot(unreachable_temporal: dict[str, str]) -> None:
    """A background worker on the dev gateway refuses to boot rather than connecting and polling."""
    said = _run(
        "chemclaw.durable.background_worker",
        gateway=None,
        extra=unreachable_temporal,
        args=[],
    )
    assert _REFUSAL in said, said[-2000:]


@pytest.mark.timeout(300)
def test_a_worker_naming_a_real_gateway_boots_past_the_guard(
    unreachable_temporal: dict[str, str],
) -> None:
    """The positive control: the same process with a real gateway gets past the guard.

    The guard runs before `connect()`, so passing it means failing on the broker instead.
    """
    said = _run(
        "chemclaw.durable.background_worker",
        gateway=_REAL_GATEWAY,
        extra=unreachable_temporal,
        args=[],
    )
    assert _REFUSAL not in said, said[-2000:]
    assert "127.0.0.1" in said or "Connection refused" in said or "connect" in said.lower(), (
        "the control arm did not reach the broker, so it cannot show the guard was passed: "
        f"{said[-2000:]}"
    )


# ------------------------------------------------------------------------------------ the mcp face


@pytest.mark.timeout(300)
def test_the_mcp_face_on_the_dev_gateway_refuses_to_boot() -> None:
    """The MCP face on the dev gateway refuses to boot.

    It serves `condense_protocols`, which builds its own chat model (`agent/condense.py`), so the
    face is in scope even though `create_face_app` is not `create_app`.
    """
    said = _run(
        "chemclaw.api.mcp_face",
        gateway=None,
        extra={"CHEMCLAW_SERVICE_PORT": str(_free_port())},
        args=[],
    )
    assert _REFUSAL in said, said[-2000:]


@pytest.mark.timeout(300)
def test_the_mcp_face_naming_a_real_gateway_boots_past_the_guard() -> None:
    """The control: with a real gateway the face fails on an occupied port instead.

    The port is held by this process for the whole call, so uvicorn's bind refuses, a failure that
    can only happen after the guard.
    """
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        said = _run(
            "chemclaw.api.mcp_face",
            gateway=_REAL_GATEWAY,
            extra={"CHEMCLAW_SERVICE_HOST": "127.0.0.1", "CHEMCLAW_SERVICE_PORT": str(port)},
            args=[],
        )
    assert _REFUSAL not in said, said[-2000:]
    assert "address already in use" in said.lower(), (
        f"the control arm did not reach uvicorn's bind: {said[-2000:]}"
    )


# --------------------------------------------------------------------------------- the terminal CLI


@pytest.mark.timeout(300)
def test_the_chat_cli_on_the_dev_gateway_refuses_with_a_message_not_a_traceback() -> None:
    """The `chemclaw` console script refuses with a message and an exit code, not a traceback.

    The guard sits inside `main`'s `try`, which turns startup failures into one sentence.
    """
    said = _run("chemclaw.cli.chat", gateway=None, extra={}, args=["--help"])
    assert _REFUSAL in said, said[-2000:]
    assert "Traceback" not in said, said[-2000:]
    # One `error:` line, not a frame stack. Matched per line rather than on the whole output
    # because RDKit writes sanitisation warnings to stderr at import, which is noise this guard has
    # no business being measured against.
    assert any(line.startswith("error: SECURITY") for line in said.splitlines()), said[-2000:]


@pytest.mark.timeout(300)
def test_the_chat_cli_naming_a_real_gateway_reaches_its_own_argument_parsing() -> None:
    """The control: `--help` is what it does instead, which only happens past the guard."""
    said = _run("chemclaw.cli.chat", gateway=_REAL_GATEWAY, extra={}, args=["--help"])
    assert _REFUSAL not in said, said[-2000:]
    assert "usage:" in said, said[-2000:]


# ------------------------------------------------------------------------- the predicate itself


def test_a_loopback_gateway_is_refused_in_every_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback gateway is refused in every posture, including a loopback `service_host`.

    The exemption is a stated posture, not the front door's bind address; both bind values are
    driven.
    """
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:8820/v1")
    for host in ("127.0.0.1", "0.0.0.0"):
        monkeypatch.setattr(settings, "service_host", host)
        with pytest.raises(RuntimeError, match="loopback address"):
            refuse_unconfigured_llm_gateway()


def test_the_whole_of_127_is_loopback_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """All of `127.0.0.0/8` is loopback, via the one predicate `core.http.is_loopback_url`."""
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.2:8820/v1")
    with pytest.raises(RuntimeError, match="loopback address"):
        refuse_unconfigured_llm_gateway()


#: Connect to `argv[1]:argv[2]` and print the peer the kernel gave. Deliberately imports nothing
#: from this repository, so the answer is the operating system's rather than the guard's.
_PEER_PROBE = """
import socket, sys
with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5) as reached:
    print(reached.getpeername()[0])
"""

#: Spellings of "inside this pod" that `ipaddress.ip_address` rejects and `connect(2)` accepts.
#: `0.0.0.0` is here for a different reason from the other four — see the test.
_SPELLINGS_THAT_REACH_THIS_HOST = ("127.1", "2130706433", "0x7f.1", "0177.1", "0.0.0.0")


@pytest.mark.parametrize("spelling", _SPELLINGS_THAT_REACH_THIS_HOST)
def test_a_gateway_spelled_to_evade_the_parser_is_still_refused(
    spelling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway spelled to evade the parser is refused, and the spelling reaches this host.

    `inet_aton(3)` accepts short, decimal, octal and hex forms of `127.0.0.1`, which
    `ipaddress.ip_address` does not. `0.0.0.0` is not loopback as a bind but never leaves the host
    as a destination, so `core.llm_gateway` normalises it. The egress layers cannot catch these (the
    allowlist admits the literal and the interposer sees canonical loopback), so the boot guard is
    the only refusal. The second arm connects and reports the peer the kernel gave.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        # In a child, because this process has `core.netguard` armed and it refuses `0.0.0.0` as a
        # destination by design — which is the guard being consistent, not the fact under test. The
        # child imports no first-party module, so what it reports is the kernel's answer.
        reached = subprocess.run(
            [sys.executable, "-c", _PEER_PROBE, spelling, str(port)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert reached.stdout.strip() == "127.0.0.1", (
        f"{spelling} does not reach this host here, so it is not the address class this test is "
        f"about: {reached.stdout}{reached.stderr}"
    )
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", False)
    monkeypatch.setattr(settings, "llm_base_url", f"http://{spelling}:{port}/v1")
    with pytest.raises(RuntimeError, match="loopback address"):
        refuse_unconfigured_llm_gateway()


def test_a_real_gateway_passes_in_every_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, so the test above cannot pass by refusing everything."""
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", False)
    monkeypatch.setattr(settings, "llm_base_url", _REAL_GATEWAY)
    refuse_unconfigured_llm_gateway()


def test_the_stated_posture_boots_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The opt-in boots, loudly — the shape `service_allow_insecure` already has.

    A silent opt-in is a control that can be switched off without anybody who reads the logs
    finding out, which is the failure `chemclaw_egress_guard_armed` exists for one layer down.
    """
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", True)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:8820/v1")
    with caplog.at_level(logging.WARNING, logger="chemclaw.core.llm_gateway"):
        refuse_unconfigured_llm_gateway()
    assert any("goes to something on this host" in record.message for record in caplog.records)


def test_the_opt_in_does_not_warn_about_a_real_gateway(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning that fires on a correct configuration is a warning nobody reads."""
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", True)
    monkeypatch.setattr(settings, "llm_base_url", _REAL_GATEWAY)
    with caplog.at_level(logging.WARNING, logger="chemclaw.core.llm_gateway"):
        refuse_unconfigured_llm_gateway()
    assert not caplog.records


# -------------------------------------------------------------- which process kinds are in scope

#: The components `deploy/entrypoint.sh` dispatches, each mapped to whether it can reach a model
#: call and so needs the guard. A partition, not an allow-list: names are read from the script, so a
#: new component without a verdict here fails.
_COMPONENT_MAKES_MODEL_CALLS: dict[str, bool] = {
    # `api/runner.py` builds the graph for every turn.
    "service": True,
    # `durable/template_activities.run_agent_step` builds a graph inside an activity.
    "background-worker": True,
    # `condense_protocols` is advertised and builds a chat model.
    "mcp-face": True,
    # A bundle's own MCP surface: the science tools, and none of them reaches the gateway.
    "connector-*": False,
    # A bundle's own Temporal worker, serving only that bundle's registered activities. Core's
    # `template_activities` is not imported here — that is the whole point of the seam.
    "connector-worker-*": False,
    # A connector's interactive worker: one activity that makes an MCP call on a chemist's behalf
    # (`connectors/queued_call.py`) and one workflow. It builds no graph and no chat model.
    "interactive-worker-*": False,
    # The hook Jobs. None calls a model: `cli.schedules` manages Temporal Schedules,
    # `core.migrate`/`core.grants` issue DDL and GRANTs, and `agent.message_migration` rewrites
    # stored rows. `cli.schedules` imports `core.embeddings` transitively but calls nothing in it,
    # so the verdict is about the call, not the import.
    "schedules": False,
    "migrate": False,
    "convert": False,
}

#: The entrypoint module for each component that must call the guard.
_GUARDED_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "service": ("api/app.py", "create_app"),
    "background-worker": ("durable/background_worker.py", "main"),
    "mcp-face": ("api/mcp_face.py", "main"),
}


def _entrypoint_components() -> set[str]:
    """Every `CHEMCLAW_COMPONENT` value `deploy/entrypoint.sh` has a case for.

    Read off the script rather than transcribed, so a new component cannot go unconsidered.
    """
    script = (_REPO_ROOT / "deploy" / "entrypoint.sh").read_text()
    body = script[script.index('case "${component}" in') :]
    # A case label and nothing else: lowercase, digits, `-` and a trailing glob, then `)`. The
    # looser "ends with a paren" reading also matched `args+=(--no-access-log)`, which is the kind
    # of false positive that makes a derived list worse than a transcribed one.
    labels = re.findall(r"(?m)^\s{2}([a-z0-9][a-z0-9-]*\*?)\)\s*$", body)
    return set(labels)


def test_every_image_component_has_a_verdict_about_model_calls() -> None:
    """No process kind may be unclassified — that is how the worker was missed for a year."""
    assert _entrypoint_components() == set(_COMPONENT_MAKES_MODEL_CALLS)


def _calls_the_guard(relative: str, function: str) -> bool:
    """Whether `function` in `relative` contains a call to the guard, read off the tree."""
    tree = ast.parse((_SRC / relative).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function:
            return any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "refuse_unconfigured_llm_gateway"
                for inner in ast.walk(node)
            )
    raise AssertionError(f"{relative} has no {function}")


@pytest.mark.parametrize(("component", "where"), sorted(_GUARDED_ENTRYPOINTS.items()))
def test_each_model_calling_component_calls_the_guard_in_its_entrypoint(
    component: str, where: tuple[str, str]
) -> None:
    """Each model-calling component calls the guard in its entrypoint.

    A shape check kept beside the process arms: it notices a refactor moving the call out of the
    function the image actually execs.
    """
    assert _COMPONENT_MAKES_MODEL_CALLS[component] is True
    assert _calls_the_guard(*where), (
        f"{component}: {where[0]}::{where[1]} no longer calls the guard"
    )


#: Modules under `src/` that are processes of their own (a `main` plus a `__main__` block) and
#: import a model seam at module scope, mapped to whether they must call the guard. Checked against
#: the tree.
_MODEL_TOUCHING_CLIS: dict[str, bool] = {
    # Its own docstring: "needs a model credential; refuses without one rather than measuring a
    # mock" — and the shipped gateway *is* the mock, so the promise needed the guard to be true.
    "cli/verifier_margin.py": True,
    # `make reindex`, a local target against the local embedding endpoint. The note index is
    # regenerable, so a mock rebuild costs a re-run; the deployment's reindex runs in the guarded
    # `background-worker`.
    "retrieval/vector_index.py": False,
}

#: Module-scope imports that mean "this process can reach the model gateway".
_MODEL_SEAMS = frozenset({"chemclaw.agent.llm_provider", "chemclaw.core.embeddings"})


def _entrypoint_modules_that_reach_a_model() -> dict[str, bool]:
    """Every `src/` module that is its own process and imports a model seam at module scope."""
    found: dict[str, bool] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        imported = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module in _MODEL_SEAMS
        } | {
            alias.name
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name in _MODEL_SEAMS
        }
        if not imported:
            continue
        has_main = any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main"
            for node in tree.body
        )
        is_entrypoint = any(
            isinstance(node, ast.If) and ast.unparse(node.test).startswith("__name__")
            for node in tree.body
        )
        if has_main and is_entrypoint:
            found[path.relative_to(_SRC).as_posix()] = _calls_the_guard(
                path.relative_to(_SRC).as_posix(), "main"
            )
    return found


def test_every_model_touching_cli_has_a_verdict_and_matches_it() -> None:
    """Every model-touching CLI has a verdict and matches it.

    A partition: a new `python -m` module building a chat model or embedding client fails until
    someone records whether it must refuse a loopback gateway.
    """
    actual = _entrypoint_modules_that_reach_a_model()
    assert set(actual) == set(_MODEL_TOUCHING_CLIS), (
        "a module that is its own process and reaches a model seam has no verdict here: "
        f"{sorted(set(actual) ^ set(_MODEL_TOUCHING_CLIS))}"
    )
    assert actual == _MODEL_TOUCHING_CLIS, (
        f"a model-touching CLI disagrees with its verdict: {actual} vs {_MODEL_TOUCHING_CLIS}"
    )


def test_the_unguarded_components_cannot_reach_the_gateway() -> None:
    """The unguarded components cannot reach the gateway.

    A connector bundle reaches it only through `agent.llm_provider` or `core.embeddings`; no bundle
    imports either, and this turns red the day one does.
    """
    forbidden = {"chemclaw.agent.llm_provider", "chemclaw.core.embeddings"}
    offenders: dict[str, set[str]] = {}
    for path in sorted((_SRC / "connectors").rglob("*.py")):
        reached = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden:
                reached.add(node.module)
            elif isinstance(node, ast.Import):
                reached |= {alias.name for alias in node.names if alias.name in forbidden}
        if reached:
            offenders[str(path.relative_to(_SRC))] = reached
    assert not offenders, (
        "a connector bundle now reaches the model gateway, so its worker and server entrypoints "
        f"need the guard too: {offenders}"
    )
