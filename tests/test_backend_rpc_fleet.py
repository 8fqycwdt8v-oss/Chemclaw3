"""The RPC client against the fleet's own `calc` app: the two transports answer alike.

`tests/test_backend_rpc.py` holds the client against a stand-in built from the contracts; this file
holds it against the real server, started from the sibling checkout's interpreter, so a change on
either side of `POST /rpc/<tool>` that the stand-in cannot see fails here. Skipped (and counted by
`tests/conftest.py`'s epilogue) wherever no built `Chemclaw3-mcp` checkout is found, or the one
found predates `/rpc`.
"""

import asyncio
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from chemclaw_contracts.calc import PredictPkaRequest, PredictSolubilityRequest

from chemclaw.connectors.calc import remote
from chemclaw.connectors.calc.remote import CalcToolError, cached_remote, remote_key
from chemclaw.core import backend_rpc, mcp_session
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.science.calc.store import InMemoryStore
from tests.conftest import _free_port, timeout_scale
from tests.siblings import SIBLING_SKIP, sibling_python, sibling_root

TOKEN = "fleet-parity-token"
TOKEN_ENV = "CHEMCLAW_CALC_TOKEN"
STARTUP_SECONDS = 120.0

#: SMILES-in tools that need neither a binary nor a geometry, so the real server answers here.
_FIXTURE = [
    PredictSolubilityRequest(smiles="CCO"),
    PredictSolubilityRequest(smiles="c1ccccc1O"),
    PredictPkaRequest(smiles="CC(=O)O"),
]


def _source_path(root: Path) -> str:
    """The checkout's own package sources first, so the server under test is this checkout's."""
    sources = [*root.glob("packages/*/src"), *root.glob("servers/calc/src")]
    return os.pathsep.join(str(path) for path in sources if path.is_dir())


@contextmanager
def _calc_app(log: Path) -> Iterator[str]:
    """The fleet's `calc` app on loopback, or a skip saying why it could not be run."""
    interpreter, reason = sibling_python("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    root, _ = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if interpreter is None or root is None:
        pytest.skip(f"{SIBLING_SKIP} the real calc app was NOT exercised: {reason}")
    port = _free_port()
    environment = {
        **os.environ,
        TOKEN_ENV: TOKEN,
        "PYTHONPATH": _source_path(root),
    }
    with log.open("w", encoding="utf-8") as sink:
        server = subprocess.Popen(
            [
                str(interpreter),
                "-m",
                "uvicorn",
                "chemclaw_mcp_calc.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=environment,
            stdout=sink,
            stderr=subprocess.STDOUT,
            cwd=str(root),
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + STARTUP_SECONDS * timeout_scale()
            while True:
                if server.poll() is not None:
                    pytest.skip(
                        f"{SIBLING_SKIP} the sibling's calc app did not start: "
                        f"{log.read_text(encoding='utf-8')[-400:]}"
                    )
                try:
                    health = httpx.get(f"{base}/healthz", timeout=2.0)
                except httpx.HTTPError:
                    health = None
                if health is not None and health.status_code == 200:
                    break
                if time.monotonic() > deadline:
                    pytest.skip(f"{SIBLING_SKIP} the sibling's calc app was not ready in time")
                time.sleep(0.2)
            served = tuple(
                int(p) for p in str(health.json().get("contract_version", "0")).split(".")
            )
            if served < backend_rpc.RPC_MIN_CONTRACT_VERSION:
                pytest.skip(
                    f"{SIBLING_SKIP} the sibling checkout reports contract_version {served}, which "
                    "predates /rpc"
                )
            yield f"{base}/mcp"
        finally:
            server.terminate()
            server.wait(timeout=20)


@pytest.fixture(scope="module")
def calc_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """One real calc app for the module: its MCP URL and the file its log goes to."""
    log = tmp_path_factory.mktemp("fleet-calc") / "calc.log"
    with _calc_app(log) as url:
        yield url, log


@pytest.fixture
def pointed(calc_app: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> str:
    """The settings of both transports aimed at the real app, with the bearer in the environment."""
    url, _ = calc_app
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setattr(settings, "calc_server_url", url)
    monkeypatch.setattr(settings, "calc_server_token_env", TOKEN_ENV)
    return url


def _leaf(exc: BaseException) -> BaseException:
    """The exception inside whatever groups wrap it.

    An exception raised inside `open_session`'s block leaves it nested in `ExceptionGroup`s (the
    SDK's task group); the RPC path has no task group and raises it bare.
    """
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


async def _keyed(
    monkeypatch: pytest.MonkeyPatch, transport: str, request: Any
) -> remote.KeyedCalculation | None:
    """`request`'s calculation key, derived over `transport`."""
    monkeypatch.setattr(settings, "calc_transport", transport)
    try:
        async with remote.calc_session() as session:
            return await remote_key(session, request)
    finally:
        await backend_rpc.close_pools_of_this_loop()


@pytest.mark.parametrize("request_", _FIXTURE, ids=lambda r: f"{r.tool_name}-{r.wire()}")
async def test_both_transports_derive_the_same_calculation_key(
    pointed: str, monkeypatch: pytest.MonkeyPatch, request_: Any
) -> None:
    """The cache key, calc_version and structure_id are identical whichever wire carried them."""
    over_mcp = await _keyed(monkeypatch, "mcp", request_)
    over_rpc = await _keyed(monkeypatch, "rpc", request_)
    assert over_rpc is not None
    assert over_rpc == over_mcp


async def test_both_transports_compute_the_same_payload_through_the_cache(
    pointed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A miss computes on the server and a hit does not, with an equal payload on either wire."""
    request = PredictSolubilityRequest(smiles="CCO")
    payloads = {}
    for transport in ("mcp", "rpc"):
        monkeypatch.setattr(settings, "calc_transport", transport)
        store = InMemoryStore()
        miss, cached_miss = await cached_remote(store, request)
        hit, cached_hit = await cached_remote(store, request)
        assert (cached_miss, cached_hit) == (False, True)
        assert hit == miss
        payloads[transport] = miss
    await backend_rpc.close_pools_of_this_loop()
    assert payloads["rpc"] == payloads["mcp"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("predict_solubility", {"smiles": "not a smiles((("}),
        ("no_such_tool", {}),
    ],
)
async def test_a_refusal_has_the_same_class_and_wording_on_both_transports(
    pointed: str, tool: str, arguments: dict[str, Any]
) -> None:
    """The server's refusal reaches the caller as the same exception, whichever wire it crossed."""
    outcomes = []
    for transport in ("mcp", "rpc"):
        try:
            if transport == "rpc":
                async with backend_rpc.rpc_session(
                    pointed, token_env=TOKEN_ENV, timeout_seconds=60.0
                ) as session:
                    await session.invoke(tool, arguments)
            else:
                async with mcp_session.open_session(
                    pointed, token_env=TOKEN_ENV, timeout_seconds=60.0
                ) as session:
                    await mcp_session.invoke(session, tool, arguments)
        except BaseException as exc:
            leaf = _leaf(exc)
            outcomes.append((type(leaf), str(leaf)))
        else:
            pytest.fail(f"{tool} did not refuse over {transport}")
    await backend_rpc.close_pools_of_this_loop()
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] is mcp_session.McpRequestRefused


async def test_the_clients_own_refusal_class_is_raised_bare_over_rpc(
    pointed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal through the calc client is `CalcToolError`, not a group around one."""
    monkeypatch.setattr(settings, "calc_transport", "rpc")
    try:
        with pytest.raises(CalcToolError, match="invalid SMILES"):
            await cached_remote(InMemoryStore(), PredictSolubilityRequest(smiles="not a smiles((("))
    finally:
        await backend_rpc.close_pools_of_this_loop()


async def test_a_wrong_bearer_is_refused_on_rpc_as_on_mcp(
    pointed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """401 from the fleet's own middleware is the credential-refused class the client names."""
    monkeypatch.setenv(TOKEN_ENV, "not-the-token")
    monkeypatch.setattr(settings, "calc_transport", "rpc")
    try:
        with pytest.raises(CalcToolError, match="refused this client's credential"):
            await cached_remote(InMemoryStore(), PredictSolubilityRequest(smiles="CCO"))
    finally:
        await backend_rpc.close_pools_of_this_loop()


async def test_the_real_server_logs_the_identity_the_client_sent(
    calc_app: tuple[str, Path], pointed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actor, session and the dry-run flag arrive on the request the server logs for /rpc."""
    _, log = calc_app
    monkeypatch.setattr(settings, "calc_transport", "rpc")
    identity = set_current_identity("fleet-actor", frozenset({"process-chemist"}))
    session_token = set_current_session_id("fleet-session")
    try:
        await cached_remote(InMemoryStore(), PredictSolubilityRequest(smiles="CCO"))
    finally:
        reset_current_session_id(session_token)
        reset_current_identity(identity)
        await backend_rpc.close_pools_of_this_loop()
    await asyncio.sleep(0.3)
    lines = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if "path=/rpc/calculation_key" in line and "actor=fleet-actor" in line
    ]
    assert lines, "the server logged no /rpc request carrying the actor"
    assert "session=fleet-session" in lines[-1]
    assert "dry_run=false" in lines[-1]
