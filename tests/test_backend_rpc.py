"""The backend RPC client: pooled typed HTTP/JSON, failing exactly as the MCP client fails.

A server on a real socket (`tests/rpc_backend_fake.py`, built from `chemclaw_contracts`) answers
`POST /rpc/<tool>` and `/healthz`, so what is asserted is what the wire carried: the headers of each
request, the TCP connection it used, the number of version checks. The failure mapping is held
against `core.mcp_session.invoke` itself, fed the text the fleet's MCP transport produces for the
same condition, so the two classifications cannot drift. `tests/test_backend_rpc_fleet.py` runs the
same client against the fleet's own `calc` app.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from chemclaw_contracts.calc import (
    CALC_REQUESTS,
    CalculationKeyRequest,
    PredictSolubilityRequest,
)
from chemclaw_contracts.rxnlabel import RXNLABEL_REQUESTS
from mcp import ClientSession
from mcp.types import CallToolResult, TextContent
from starlette.responses import PlainTextResponse

from chemclaw.connectors.calc import remote
from chemclaw.connectors.calc.remote import (
    CalcBusyError,
    CalcServerError,
    CalcTimeBudgetError,
    CalcToolError,
    cached_remote,
    remote_call,
)
from chemclaw.core import backend_rpc, mcp_session
from chemclaw.core.call_identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
)
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.mcp_session import (
    McpAtCapacity,
    McpConnectFailed,
    McpCredentialRefused,
    McpRequestRefused,
    McpServerFault,
    McpTimeBudget,
)
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.core.turn_flags import reset_dry_run, set_dry_run
from chemclaw.ingest.labels import labeller
from chemclaw.ingest.labels.labeller import LabelServerError, LabelToolError, RxnLabelServer
from chemclaw.science.calc.store import InMemoryStore
from tests.conftest import _free_port
from tests.rpc_backend_fake import RpcBackendFake, rpc_error
from tests.served import ServerThread

TOKEN_ENV = "CHEMCLAW_TEST_RPC_TOKEN"
_KEY = {
    "calc_type": "solubility",
    "calc_version": "esol-delaney@2004/rdkit-2026.3.5/u-0.75",
    "input_hash": "a7d334ebee616d78",
    "params_hash": "3ba6ef80c850abd1",
}
_FULL_POD = "[calc-at-capacity] 4 of 4 calculation slots are taken"
_SPENT_CLOCK = "[calc-time-budget] the inline wall clock of 780 s ran out"
_FAULT = "an internal error occurred (error id 1f2e3d4c)"


def _calc_backend() -> RpcBackendFake:
    """A calc stand-in that keys and computes `predict_solubility`."""
    fake = RpcBackendFake(CALC_REQUESTS)
    fake.answers["calculation_key"] = lambda body: {
        "tool": body["tool"],
        "calc_version": _KEY["calc_version"],
        "key": _KEY,
        "structure_id": None,
    }
    fake.answers["predict_solubility"] = lambda body: {"log_s_mol_per_l": -0.12, "smiles": "CCO"}
    return fake


@dataclass(frozen=True)
class Served:
    """A running stand-in, and the MCP-style URL settings name it by."""

    fake: RpcBackendFake
    url: str


@asynccontextmanager
async def serving(
    fake: RpcBackendFake, monkeypatch: pytest.MonkeyPatch, *, timeout: float = 30.0
) -> AsyncIterator[Served]:
    """Serve `fake`, point both backends at it on the rpc transport, and close the pools after.

    The pools are closed here because they belong to the test's loop and would otherwise outlive
    the server they connect to.
    """
    monkeypatch.setenv(TOKEN_ENV, fake.token)
    with ServerThread(fake.app()) as server:
        url = f"http://127.0.0.1:{server.port}/mcp"
        for prefix in ("calc", "rxnlabel"):
            monkeypatch.setattr(settings, f"{prefix}_server_url", url)
            monkeypatch.setattr(settings, f"{prefix}_server_token_env", TOKEN_ENV)
            monkeypatch.setattr(settings, f"{prefix}_server_timeout_seconds", timeout)
        monkeypatch.setattr(settings, "calc_transport", "rpc")
        try:
            yield Served(fake, url)
        finally:
            await backend_rpc.close_pools_of_this_loop()


def _session(served: Served, timeout: float = 30.0) -> backend_rpc.RpcSession:
    """A handle on the pooled client for `served`, as `calc_session` makes one per call."""
    return backend_rpc.RpcSession(
        backend_rpc.rpc_base_url(served.url), TOKEN_ENV, timeout_seconds=timeout
    )


# ---- the happy path: keyed, looked up, computed, on one connection -------------------------


async def test_a_calculation_is_keyed_and_computed_over_rpc_on_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key, lookup, compute: the same calls as over MCP, N sessions, one TCP connection.

    Each `cached_remote` opens its own `calc_session`; the pool is what the sessions share, so the
    connection count is the measure of whether the per-call cost is really gone.
    """
    async with serving(_calc_backend(), monkeypatch) as served:
        store = InMemoryStore()
        request = PredictSolubilityRequest(smiles="CCO")
        first, cached_first = await cached_remote(store, request)
        results = [await cached_remote(store, request) for _ in range(7)]

        assert (cached_first, {cached for _, cached in results}) == (False, {True})
        assert {payload["log_s_mol_per_l"] for payload, _ in results} == {-0.12}
        assert first["smiles"] == "CCO"
        tools = [seen.tool for seen in served.fake.seen]
        assert tools == ["calculation_key", "predict_solubility"] + ["calculation_key"] * 7
        assert (
            served.fake.seen[0].body
            == CalculationKeyRequest(tool="predict_solubility", arguments=request.wire()).wire()
        )
        assert len(served.fake.peers()) == 1, "each session opened its own connection"
        assert served.fake.health_hits == 1, "the version is asked once per pool, not per call"


async def test_concurrent_calls_share_the_pool_and_do_not_exceed_their_own_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six callers at once use at most six connections, and the next six reuse them."""
    async with serving(_calc_backend(), monkeypatch) as served:
        request = PredictSolubilityRequest(smiles="CCO")
        for _ in range(2):
            await asyncio.gather(*(remote_call(request) for _ in range(6)))
        assert len(served.fake.peers()) <= 6
        assert backend_rpc.open_pool_count() == 1


# ---- identity and credential ---------------------------------------------------------------


async def test_the_turn_identity_travels_on_every_request_from_the_calling_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actor, session, correlation id and dry-run reach the server, per request.

    Two turns call at once through one pooled client; each request carries its own turn's
    identity, which is what the MCP path needs a connection per call to guarantee.
    """
    async with serving(_calc_backend(), monkeypatch) as served:

        async def as_turn(actor: str, session: str, correlation: str, dry_run: bool) -> None:
            identity = set_current_identity(actor, frozenset({"process-chemist"}))
            session_token = set_current_session_id(session)
            correlation_token = set_current_correlation_id(correlation)
            dry_token = set_dry_run(dry_run)
            try:
                await asyncio.sleep(0)
                await remote_call(PredictSolubilityRequest(smiles="CCO"))
            finally:
                reset_dry_run(dry_token)
                reset_current_correlation_id(correlation_token)
                reset_current_session_id(session_token)
                reset_current_identity(identity)

        await asyncio.gather(
            as_turn("user-1", "session-1", "turn-1", False),
            as_turn("user-2", "session-2", "turn-2", True),
        )

        seen = {seen.headers[HEADER_ACTOR.lower()]: seen.headers for seen in served.fake.seen}
        assert set(seen) == {"user-1", "user-2"}
        assert seen["user-1"][HEADER_SESSION.lower()] == "session-1"
        assert seen["user-1"][HEADER_CORRELATION.lower()] == "turn-1"
        assert seen["user-1"][HEADER_DRY_RUN.lower()] == "false"
        assert seen["user-2"][HEADER_SESSION.lower()] == "session-2"
        assert seen["user-2"][HEADER_CORRELATION.lower()] == "turn-2"
        assert seen["user-2"][HEADER_DRY_RUN.lower()] == "true"


async def test_no_turn_means_no_identity_headers_but_the_dry_run_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off the request path the actor, session and correlation id are absent, never empty."""
    async with serving(_calc_backend(), monkeypatch) as served:
        await remote_call(PredictSolubilityRequest(smiles="CCO"))
        headers = served.fake.seen[0].headers
        assert headers[HEADER_DRY_RUN.lower()] == "false"
        assert {
            HEADER_ACTOR.lower(),
            HEADER_SESSION.lower(),
            HEADER_CORRELATION.lower(),
        }.isdisjoint(headers)


async def test_the_bearer_is_read_from_the_environment_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotated credential needs no restart; an unset one is refused by the server, not guessed."""
    fake = _calc_backend()
    async with serving(fake, monkeypatch):
        await remote_call(PredictSolubilityRequest(smiles="CCO"))
        assert fake.seen[-1].headers["authorization"] == f"Bearer {fake.token}"

        monkeypatch.setenv(TOKEN_ENV, "rotated-away")
        with pytest.raises(CalcToolError, match="refused this client's credential") as refused:
            await remote_call(PredictSolubilityRequest(smiles="CCO"))
        assert TOKEN_ENV in str(refused.value) and "HTTP 401" in str(refused.value)

        monkeypatch.delenv(TOKEN_ENV)
        with pytest.raises(CalcToolError, match="refused this client's credential"):
            await remote_call(PredictSolubilityRequest(smiles="CCO"))

        fake.token = "rotated-in"
        monkeypatch.setenv(TOKEN_ENV, "rotated-in")
        await remote_call(PredictSolubilityRequest(smiles="CCO"))


# ---- the failure mapping --------------------------------------------------------------------


def _mcp_text(text: str) -> CallToolResult:
    """What the fleet's MCP transport answers for a tool that failed with `text`."""
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


class _McpSession:
    """The one method `core.mcp_session.invoke` calls, answering a fixed result."""

    def __init__(self, result: CallToolResult) -> None:
        self._result = result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        del name, arguments
        return self._result


_LEAD = "Error executing tool predict_solubility: "
# (kind, status, message, what MCP's transport says for the same condition)
_KINDS: list[tuple[str, int, str, str]] = [
    ("refused", 400, "invalid SMILES 'c1(('", f"{_LEAD}invalid SMILES 'c1(('"),
    ("unknown_op", 404, "this server serves no operation", "Unknown tool: predict_solubility"),
    ("at_capacity", 503, _FULL_POD, f"{_LEAD}{_FULL_POD}"),
    ("time_budget", 422, _SPENT_CLOCK, f"{_LEAD}{_SPENT_CLOCK}"),
    ("failed", 500, _FAULT, f"{_LEAD}{_FAULT}"),
]


@pytest.mark.parametrize(("kind", "status", "message", "mcp_text"), _KINDS)
async def test_each_rpc_error_kind_raises_what_the_mcp_path_raises_for_the_same_condition(
    monkeypatch: pytest.MonkeyPatch, kind: Any, status: int, message: str, mcp_text: str
) -> None:
    """Class, wording and fault flag are those of `invoke` fed the MCP transport's text.

    The comparison is made against the real classifier rather than a table of expected classes, so a
    change to either side that separates them fails here.
    """
    fake = _calc_backend()
    fake.answers["predict_solubility"] = lambda body: rpc_error(kind, message, status)
    async with serving(fake, monkeypatch) as served:
        with pytest.raises(Exception) as over_rpc:
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        with pytest.raises(Exception) as over_mcp:
            await mcp_session.invoke(
                cast(ClientSession, _McpSession(_mcp_text(mcp_text))),
                "predict_solubility",
                {"smiles": "CCO"},
            )
    assert type(over_rpc.value) is type(over_mcp.value)
    assert str(over_rpc.value) == str(over_mcp.value)
    if isinstance(over_mcp.value, McpServerFault):
        assert isinstance(over_rpc.value, McpServerFault)
        assert over_rpc.value.internal == over_mcp.value.internal is True
    classes = {
        "refused": McpRequestRefused,
        "unknown_op": McpRequestRefused,
        "at_capacity": McpAtCapacity,
        "time_budget": McpTimeBudget,
        "failed": McpServerFault,
    }
    assert type(over_rpc.value) is classes[kind]


@pytest.mark.parametrize(
    ("status", "expected", "status_text"),
    [(421, McpConnectFailed, "Invalid Host header"), (400, McpConnectFailed, "bad Content-Type")],
)
async def test_a_transport_guard_rejection_reads_as_the_mcp_path_reads_it(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: type[Exception], status_text: str
) -> None:
    """Host or Content-Type refused: an unreachable server to callers, a warning for the operator.

    On MCP the same refusal fails `initialize` with a non-credential HTTP status, which
    `open_session` raises as `McpConnectFailed`.
    """
    fake = _calc_backend()
    fake.answers["predict_solubility"] = lambda body: rpc_error("rejected", status_text, status)
    async with serving(fake, monkeypatch) as served:
        with pytest.raises(expected):
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})

        fake.answers["predict_solubility"] = lambda body: rpc_error(
            "rejected", "Invalid Origin", 403
        )
        with pytest.raises(McpCredentialRefused) as refused:
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert refused.value.status == 403


async def test_the_layers_below_the_route_answer_in_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """413 is a refusal, a bare 5xx is the server stopping, 401 is the credential."""
    fake = _calc_backend()
    async with serving(fake, monkeypatch) as served:
        fake.answers["predict_solubility"] = lambda body: PlainTextResponse("too big", 413)
        with pytest.raises(McpRequestRefused, match="larger than the server takes"):
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})

        fake.answers["predict_solubility"] = lambda body: PlainTextResponse("bad gateway", 502)
        with pytest.raises(McpServerFault) as fault:
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert fault.value.internal is False

        fake.answers["predict_solubility"] = lambda body: PlainTextResponse("odd", 418)
        with pytest.raises(McpRequestRefused, match="HTTP 418"):
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})


async def test_an_answer_that_is_not_json_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """As `invoke` reads a tool that answered text: a refusal quoting the start of it."""
    fake = _calc_backend()
    fake.answers["predict_solubility"] = lambda body: PlainTextResponse("not json at all", 200)
    async with serving(fake, monkeypatch) as served:
        with pytest.raises(McpRequestRefused, match="returned no JSON: not json at all"):
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})


async def test_the_calc_client_gives_each_condition_the_chemist_facing_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `cached_remote`: a full pod is retryable, a spent clock and a refusal are not.

    These are the classes `durable/publish.py` reads, so the retry behaviour of a durable job does
    not change with the transport.
    """
    fake = _calc_backend()
    async with serving(fake, monkeypatch):
        request = PredictSolubilityRequest(smiles="CCO")
        for kind, message, expected in [
            ("at_capacity", _FULL_POD, CalcBusyError),
            ("time_budget", _SPENT_CLOCK, CalcTimeBudgetError),
            ("refused", "invalid SMILES", CalcToolError),
            ("failed", _FAULT, CalcServerError),
        ]:
            fake.answers["calculation_key"] = lambda body, k=kind, m=message: rpc_error(k, m)  # type: ignore[misc]
            with pytest.raises(expected) as raised:
                await cached_remote(InMemoryStore(), request)
            assert type(raised.value) is expected, kind
        assert isinstance(raised.value.__cause__, McpServerFault)


# ---- timeouts and an unreachable server -----------------------------------------------------


async def test_a_call_past_its_read_bound_is_the_server_stopping_and_the_pool_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow answer is `McpServerFault` (as the MCP path's 408 is), and the next call is fine."""
    fake = _calc_backend()

    async def slow(body: Any) -> dict[str, Any]:
        await asyncio.sleep(1.5)
        return {"log_s_mol_per_l": 0.0}

    fake.answers["predict_solubility"] = slow
    async with serving(fake, monkeypatch) as served:
        with pytest.raises(McpServerFault) as fault:
            await _session(served, timeout=0.3).invoke("predict_solubility", {"smiles": "CCO"})
        assert fault.value.internal is False

        fake.answers["predict_solubility"] = lambda body: {"log_s_mol_per_l": 1.0}
        answer = await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert answer == {"log_s_mol_per_l": 1.0}


async def test_the_read_bound_is_the_callers_not_the_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    """One pool serves a short bound and a long one: the bound is per request."""
    fake = _calc_backend()

    async def moderate(body: Any) -> dict[str, Any]:
        await asyncio.sleep(0.6)
        return {"log_s_mol_per_l": 2.0}

    fake.answers["predict_solubility"] = moderate
    async with serving(fake, monkeypatch) as served:
        with pytest.raises(McpServerFault):
            await _session(served, timeout=0.2).invoke("predict_solubility", {"smiles": "CCO"})
        assert await _session(served, timeout=10.0).invoke(
            "predict_solubility", {"smiles": "CCO"}
        ) == {"log_s_mol_per_l": 2.0}
        assert backend_rpc.open_pool_count() == 1


async def test_a_server_that_is_not_there_is_an_outage_and_leaves_no_load_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection refused is `CalcServerError`, and the in-flight gauge returns to zero."""
    monkeypatch.setattr(settings, "calc_transport", "rpc")
    monkeypatch.setattr(settings, "calc_server_url", f"http://127.0.0.1:{_free_port()}/mcp")
    monkeypatch.setattr(settings, "calc_server_token_env", TOKEN_ENV)
    try:
        for _ in range(3):
            with pytest.raises(CalcServerError, match="not answering"):
                await remote_call(PredictSolubilityRequest(smiles="CCO"))
        assert remote._IN_FLIGHT == 0
    finally:
        await backend_rpc.close_pools_of_this_loop()


# ---- the version check ----------------------------------------------------------------------


async def test_a_server_that_predates_rpc_is_refused_clearly_and_never_asked_over_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """contract_version 1.0.0 stops the call with an actionable error: nothing sent, no fallback.

    Retryable (`CalcServerError`), since a rollout in progress is the usual cause, and the check is
    not remembered, so the first call after the server is upgraded succeeds.
    """

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("fell back to an MCP session")

    monkeypatch.setattr(remote, "open_session", forbidden)
    fake = _calc_backend()
    fake.reported = "1.0.0"
    async with serving(fake, monkeypatch) as served:
        with pytest.raises(CalcServerError) as refused:
            await remote_call(PredictSolubilityRequest(smiles="CCO"))
        message = str(refused.value)
        assert (
            "1.0.0" in message and "CHEMCLAW_CALC_TRANSPORT=mcp" in message and "1.1.0" in message
        )
        assert isinstance(refused.value.__cause__, backend_rpc.RpcUnsupported)
        assert served.fake.seen == [], (
            "a request reached /rpc on a server that did not pass the check"
        )

        served.fake.reported = "1.1.0"
        await remote_call(PredictSolubilityRequest(smiles="CCO"))
        assert [seen.tool for seen in served.fake.seen] == ["predict_solubility"]


@pytest.mark.parametrize(
    ("reported", "accepted"),
    [
        ("1.0.0", False),
        (None, False),
        ("garbage", False),
        ("1.1.0", True),
        ("1.10.2", True),
        ("2.0.0", True),
    ],
)
async def test_the_contract_version_is_compared_as_numbers(
    monkeypatch: pytest.MonkeyPatch, reported: str | None, accepted: bool
) -> None:
    """1.10.2 is newer than 1.1.0; a missing or unreadable version is not an accepted one."""
    fake = _calc_backend()
    fake.reported = reported
    async with serving(fake, monkeypatch) as served:
        if accepted:
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        else:
            with pytest.raises(backend_rpc.RpcUnsupported) as refused:
                await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
            assert refused.value.served == (reported if reported != "garbage" else "garbage")


async def test_a_pod_without_the_route_is_unsupported_and_the_version_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During a rollout /healthz may be answered by a new pod and /rpc by an old one."""
    fake = _calc_backend()
    async with serving(fake, monkeypatch) as served:
        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        fake.route_missing = True
        with pytest.raises(backend_rpc.RpcUnsupported):
            await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        fake.route_missing = False
        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert fake.health_hits == 2


# ---- the pool's lifetime --------------------------------------------------------------------


async def test_the_pool_is_shared_by_url_and_closed_with_its_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One client per (loop, base URL); `pooling()` closes this loop's and no other's."""
    async with serving(_calc_backend(), monkeypatch) as served:
        assert backend_rpc.open_pool_count() == 0, "pools open lazily"
        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert backend_rpc.open_pool_count() == 1
        client = next(iter(backend_rpc._POOLS.values())).client

        async with backend_rpc.pooling():
            pass
        assert client.is_closed
        assert backend_rpc.open_pool_count() == 0

        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert backend_rpc.open_pool_count() == 1, "a closed pool is replaced on next use"


def test_a_pool_of_an_ended_loop_is_dropped_not_reused() -> None:
    """A client bound to a finished loop cannot be reused, so the registry forgets it."""

    async def open_one() -> None:
        backend_rpc._pool_for("http://127.0.0.1:1")

    asyncio.run(open_one())
    assert backend_rpc.open_pool_count() == 1

    async def another_loop() -> int:
        backend_rpc._pool_for("http://127.0.0.1:2")
        try:
            return backend_rpc.open_pool_count()
        finally:
            await backend_rpc.close_pools_of_this_loop()

    assert asyncio.run(another_loop()) == 1


async def test_an_idle_connection_is_retired_before_the_server_can_close_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After `backend_rpc_keepalive_seconds` idle, the next call opens a fresh connection."""
    monkeypatch.setattr(settings, "backend_rpc_keepalive_seconds", 0.2)
    async with serving(_calc_backend(), monkeypatch) as served:
        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert len(served.fake.peers()) == 1
        await asyncio.sleep(0.6)
        await _session(served).invoke("predict_solubility", {"smiles": "CCO"})
        assert len(served.fake.peers()) == 2


def test_the_rpc_base_is_the_mcp_url_without_its_endpoint() -> None:
    """`/mcp` and a trailing slash come off; a prefix the URL carries stays."""
    assert backend_rpc.rpc_base_url("http://calc:8860/mcp") == "http://calc:8860"
    assert backend_rpc.rpc_base_url("http://calc:8860/mcp/") == "http://calc:8860"
    assert backend_rpc.rpc_base_url("https://gw.example/fleet/calc/mcp") == (
        "https://gw.example/fleet/calc"
    )
    assert backend_rpc.rpc_base_url("http://calc:8860") == "http://calc:8860"


async def test_the_default_transport_is_mcp_and_opens_no_rpc_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Until the flip, a deployment that sets nothing opens an MCP session and no HTTP pool."""
    assert settings.calc_transport == "mcp"
    opened: list[str] = []

    @asynccontextmanager
    async def fake_open(url: str, **kwargs: Any) -> AsyncIterator[str]:
        opened.append(url)
        yield "mcp-session"

    monkeypatch.setattr(remote, "open_session", fake_open)
    async with remote.calc_session() as session:
        assert str(session) == "mcp-session"
    assert opened == [settings.calc_server_url]
    assert backend_rpc.open_pool_count() == 0


# ---- the labelling client -------------------------------------------------------------------


def _label_backend() -> RpcBackendFake:
    fake = RpcBackendFake(RXNLABEL_REQUESTS)
    fake.answers["labeller_version"] = lambda body: {"version": "rxnmapper@1.0/roles@3"}
    fake.answers["represent_reactions"] = lambda body: {
        "results": [
            {"id": item["id"], "mapped_smiles": "[CH3:1][OH:2]", "species": []}
            for item in body["reactions"]
        ]
    }
    fake.answers["name_reactions"] = lambda body: {
        "results": [{"id": item["id"], "name": "esterification"} for item in body["reactions"]]
    }
    return fake


async def test_the_labelling_client_speaks_rpc_with_the_same_identity_and_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """version, represent and name: the answers parse as over MCP, on one connection."""
    async with serving(_label_backend(), monkeypatch) as served:
        server = RxnLabelServer()
        identity = set_current_identity("labeller-7", frozenset({"process-chemist"}))
        try:
            version = await server.version()
            represented = await server.represent([("r1", "CC>>CC", [])])
            named = await server.name([("r1", "CC>>CC")])
        finally:
            reset_current_identity(identity)

        assert version == labeller.stamped("rxnmapper@1.0/roles@3")
        assert represented["r1"].mapped_smiles == "[CH3:1][OH:2]"
        assert named["r1"].id == "r1"
        assert [seen.tool for seen in served.fake.seen] == [
            "labeller_version",
            "represent_reactions",
            "name_reactions",
        ]
        assert len(served.fake.peers()) == 1
        assert {seen.headers[HEADER_ACTOR.lower()] for seen in served.fake.seen} == {"labeller-7"}


async def test_the_labelling_client_classifies_failures_as_it_does_over_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is bad data; a fault and a refused credential are read as the drain reads them."""
    fake = _label_backend()
    async with serving(fake, monkeypatch):
        fake.answers["labeller_version"] = lambda body: rpc_error("refused", "no such labeller")
        with pytest.raises(LabelToolError, match="no such labeller"):
            await RxnLabelServer().version()

        fake.answers["labeller_version"] = lambda body: rpc_error("failed", _FAULT)
        with pytest.raises(LabelServerError, match="failed while running labeller_version"):
            await RxnLabelServer().version()

        monkeypatch.setenv(TOKEN_ENV, "wrong")
        with pytest.raises(LabelToolError, match="refused this client's credential"):
            await RxnLabelServer().version()
        monkeypatch.setenv(TOKEN_ENV, fake.token)

        fake.reported = "1.0.0"
        await backend_rpc.close_pools_of_this_loop()
        with pytest.raises(LabelServerError, match="CHEMCLAW_CALC_TRANSPORT=mcp"):
            await RxnLabelServer().version()


async def test_both_backends_share_one_pool_when_they_share_a_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool is keyed by base URL, so a gateway fronting both is one set of connections."""
    fake = _calc_backend()
    fake.answers["labeller_version"] = lambda body: {"version": "v"}
    fake.requests = {**CALC_REQUESTS, **RXNLABEL_REQUESTS}
    async with serving(fake, monkeypatch):
        await remote_call(PredictSolubilityRequest(smiles="CCO"))
        await RxnLabelServer().version()
        assert backend_rpc.open_pool_count() == 1
