"""The gateway boot guard, driven as processes rather than asserted as a call.

The defect this file exists to hold shut is not that a function was wrong — it was right — but that
it was **unreachable from three of the four process kinds that need it**. It lived in
`api/middleware.py` and `api/app.py` was its only caller, so a background worker, whose
`run_agent_step` activity builds a LangGraph agent, inherited the shipped loopback gateway and
dialled it silently (`D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard`).

**A test that asserts the guard function is called is the test that was always going to pass.** So
the arms below start real processes — `python -m chemclaw.durable.background_worker`,
`python -m chemclaw.api.mcp_face`, `python -m chemclaw.cli.chat` — and read what they do. Each has a
**positive control**, because a harness that never got as far as the guard would report every
refusal identically: the control names a real gateway and the process must then fail on the *next*
thing instead, which is how "it got past the guard" is observed without a broker, a database or a
model.

The differential is one environment variable. Both arms of a pair point `CHEMCLAW_TEMPORAL_ADDRESS`
(or `CHEMCLAW_SERVICE_PORT`) at something deliberately unreachable, so the only difference between
"refused by the guard" and "reached the next step" is the gateway address.

**Proxy variables are scrubbed from every child.** `core.netguard.arm_from_settings` refuses an
undeclared ambient proxy at `chemclaw.core.config` import
(`D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address`), which in a sandbox that has one
would abort every arm before the guard under test ran — and the two refusals read alike enough that
the suite would have looked green while measuring nothing.
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

    `gateway` of `None` leaves `CHEMCLAW_LLM_BASE_URL` unset, which is the case that matters: the
    shipped default is the loopback mock, so "a deployment that configured nothing" and "a
    deployment that never overrode this" are the same environment.
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
    """A loopback Temporal address nothing serves: the step a worker reaches *after* the guard.

    Loopback on purpose. `core.netguard` permits any loopback destination without allowlisting, so
    the failure the control arm reports is a refused connection rather than the egress guard's own
    refusal — two RuntimeErrors that would otherwise be easy to mistake for each other.
    """
    return {
        "CHEMCLAW_TEMPORAL_ADDRESS": f"127.0.0.1:{_free_port()}",
        "CHEMCLAW_TEMPORAL_NAMESPACE": "guard-probe",
    }


@pytest.mark.timeout(300)
def test_a_worker_on_the_dev_gateway_refuses_to_boot(unreachable_temporal: dict[str, str]) -> None:
    """The closable half of the backlog row, measured on the process it was open in.

    Driven before the fix, this same arm printed the worker's `connected` line and began polling:
    neither guard was in the module's namespace and `run_agent_step` was a registered activity, so
    the pod was one turn away from dialling its own loopback port.
    """
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
    """The positive control: the same process, one variable different, gets further.

    Without this arm the refusal above proves nothing — a harness that could not import the worker
    at all would fail identically. What "further" means here is observable and specific: the guard
    runs before `connect()`, so a control that passes it must fail on the broker instead.
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
    """The face makes model calls, which is why it is in scope and was never covered.

    `condense_protocols` is read-only, is not in `mcp_face.WITHHELD`, and builds a chat model of its
    own (`agent/condense.py`). So a face pointed at the shipped default serves a tool that cannot
    answer — and nothing said so at boot, because the guard was in `api/middleware.py` and
    `create_face_app` is not `create_app`.
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
    """The control: a bound port is what it fails on instead, so the guard was passed.

    The port is taken by this process for the whole call, so uvicorn's own bind is what refuses —
    a failure that can only happen *after* the guard, and one that does not need a model, a broker
    or a database to be reached.
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
    """The `chemclaw` console script: a refusal, translated, with an exit code.

    Placed inside `main`'s `try` precisely so it joins the three families that function already
    turns into one sentence — a startup failure here must not arrive as nine frames of asyncio, for
    the reason `cli/chat.main`'s own docstring gives.
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
    """The swap this ADR made: the exemption is a stated posture, not a bind.

    The old predicate returned early whenever `service_host` named a loopback interface, which made
    the guard a statement about the front door's socket. Both values of that field are driven here,
    because "it still refuses when the bind is loopback" is the half that is new.
    """
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", False)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:8820/v1")
    for host in ("127.0.0.1", "0.0.0.0"):
        monkeypatch.setattr(settings, "service_host", host)
        with pytest.raises(RuntimeError, match="loopback address"):
            refuse_unconfigured_llm_gateway()


def test_the_whole_of_127_is_loopback_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a set of literals: `core.http.is_loopback_url` is the one definition and it parses.

    A second address in `127.0.0.0/8` was the measured gap between this guard and the egress guard
    before they shared a predicate (`core/http.py`), and it is the shape a hand-kept set of three
    strings reintroduces the moment somebody adds a fourth.
    """
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
    """The refusal and the reason for it, measured in the same test.

    `is_loopback_host` parsed with `ipaddress.ip_address`, which accepts only the dotted-quad form,
    while what a socket is ultimately handed is `inet_aton(3)` — so the short, decimal, octal and
    hexadecimal spellings of `127.0.0.1` were *not* loopback to the guard and booted clean. The
    fifth, `0.0.0.0`, is a different failure with the same effect: it is genuinely not loopback as a
    **bind** (which is why `core.http` still answers False for it and two callers depend on that),
    and as a **destination** it never leaves the host, so `core.llm_gateway` normalises it itself.

    Neither egress layer catches the follow-on: `derive_allowed` puts the same literal on the
    allowlist, and the compiled interposer sees `inet_ntop`'s canonical `127.0.0.1`, which is
    loopback-exempt. So the boot guard is the only layer that can refuse this, and the second arm
    here is what makes the first mean something — it connects and reports the peer the kernel
    actually gave, rather than asserting from a table that the spelling is equivalent.
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

#: The components `deploy/entrypoint.sh` dispatches, and for each one whether it can reach a model
#: call — which is the question that decides whether the guard belongs in its entrypoint.
#:
#: **A partition, not an allow-list**, for `mcp_face.WITHHELD`'s reason: the test below reads the
#: component names out of the script, so a component added there without a verdict here fails
#: rather than quietly joining the unguarded half.
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
    # The hook Jobs, which became components in
    # `D-2026-09-12-the-layer-that-binds-grpc-is-libc-not-socket-py` because a chart `command:`
    # replaces the image ENTRYPOINT and so skipped the arming block. None of the three reaches a
    # model: `cli.schedules` creates and prunes Temporal Schedules, `core.migrate`/`core.grants`
    # issue DDL and GRANTs, and `agent.message_migration` rewrites stored rows. `cli.schedules`
    # *imports* `core.embeddings` transitively through `durable.schedules` (measured) and calls
    # nothing in it, which is why the verdict is about the call and not about the import — the
    # import-closure proxy below is applied to connector bundles, where the seam is the point.
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

    Read off the script rather than transcribed, because a component added to the image is a
    process kind this guard has to have an answer about, and a list here would go stale silently —
    the failure `MODULES.md`'s port registry and this repository's own `make`-target counts record.
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
    """The shape assertion, kept *beside* the effect ones rather than instead of them.

    It earns its place on the one thing a process arm cannot say cheaply: `service` is driven by
    `create_app` in-process above, and this is what notices if a future refactor moves the call out
    of the function the image actually execs.
    """
    assert _COMPONENT_MAKES_MODEL_CALLS[component] is True
    assert _calls_the_guard(*where), (
        f"{component}: {where[0]}::{where[1]} no longer calls the guard"
    )


#: Modules under `src/` that are a process in their own right (a `main` plus a `__main__` block)
#: **and** import a model seam at module scope, mapped to whether they must call the guard. Derived
#: against, not transcribed: the module docstring of `core/llm_gateway` used to promise "every
#: process that makes a model call" while one of these two was unguarded and nothing looked.
_MODEL_TOUCHING_CLIS: dict[str, bool] = {
    # Its own docstring: "needs a model credential; refuses without one rather than measuring a
    # mock" — and the shipped gateway *is* the mock, so the promise needed the guard to be true.
    "cli/verifier_margin.py": True,
    # `make reindex` / `make reindex-full`, a documented local target against the local embedding
    # endpoint. The note index is regenerable by definition (D-011's sibling argument), so a local
    # rebuild against the mock costs a re-run rather than a wrong answer to a chemist — and the
    # deployment's own reindex is the `background-worker`'s scheduled job, which is guarded.
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
    """The promise in `core/llm_gateway`'s docstring, made checkable in both directions.

    A partition rather than a list of the guarded ones: a new `python -m` module that builds a chat
    model or an embedding client fails here until somebody writes down whether it must refuse a
    loopback gateway. The three deployment components have their own arms above; this covers the
    processes an operator starts by hand, which is where the promise was wider than the test.
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
    """The other half of the partition, checked rather than asserted in prose.

    A connector bundle reaches the model gateway only through `agent.llm_provider` (a chat model)
    or `core.embeddings` (the embedding endpoint, which is the same address). Neither is imported by
    any bundle, which is what makes leaving `connector-*` and `connector-worker-*` unguarded a
    measurement rather than an oversight — and what turns it red the day a bundle grows a model
    call.
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
