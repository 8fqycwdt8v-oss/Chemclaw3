"""The calculation client: the cache still decides, and nothing here derives a `calc_version`.

Two properties, both asserted here: a persisted result is never recomputed across the wire
(D-011, counted on a fake session), and no `calc_version` is derived in this repository (a static
check, because a locally derived version is well-formed, matches no ledger row and fails silently).
No live server is required.
"""

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, ErrorData

from chemclaw.connectors import registry
from chemclaw.connectors.calc import remote
from chemclaw.connectors.calc.remote import (
    CalcBusyError,
    CalcServerError,
    CalcTimeBudgetError,
    CalcToolError,
    cached_remote,
    remote_key,
)
from chemclaw.core import mcp_session
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.core.ids import stable_hash
from chemclaw.core.mcp_session import McpConnectFailed
from chemclaw.core.metrics import METRICS
from chemclaw.durable.publish import _BAD_DATA_TYPES
from chemclaw.science.calc.store import CALCULATION_EPOCH, InMemoryStore

# A version carrying both key delimiters (real model and calibration names), so a client that split
# the flat `type@version:input:params` form would reassemble a different key.
_AWKWARD_VERSION = "esol-delaney@2004/rdkit-2026.3.5/cal-0.28733:-29.3116"


class _FakeSession:
    """An MCP session that answers `calculation_key` and one compute tool, counting both."""

    def __init__(self, key: dict[str, Any] | None, payload: dict[str, Any]) -> None:
        self._key = key
        self._payload = payload
        self.key_calls = 0
        self.compute_calls = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "calculation_key":
            self.key_calls += 1
            return _Result({"key": self._key})
        self.compute_calls += 1
        return _Result(self._payload)


class _Result:
    """The `CallToolResult` shape the client reads: `isError` plus text content.

    A failed call carries plain text rather than JSON, as on the real wire; a JSON-wrapped marker
    would be the echoed shape `server_marked` rejects.
    """

    def __init__(self, payload: dict[str, Any] | str, is_error: bool = False) -> None:
        import json

        self.isError = is_error
        self.content = [_Text(payload if isinstance(payload, str) else json.dumps(payload))]


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


def _session(monkeypatch: pytest.MonkeyPatch, fake: _FakeSession) -> None:
    """Make `calc_session` yield `fake`, so no socket is opened."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session(timeout_seconds: float | None = None) -> Any:
        # Accepted and unused: the read bound is the caller's, and a sampling call passes a longer
        # one than a Hessian's (`calc_sampling_timeout_seconds`). Nothing here depends on which.
        del timeout_seconds
        yield fake

    monkeypatch.setattr("chemclaw.connectors.calc.remote.calc_session", _fake_session)


_KEY = {
    "calc_type": "solubility",
    "calc_version": _AWKWARD_VERSION,
    "input_hash": "07010a68dabf6858",
    "params_hash": "a075a6029c28d314",
}


async def test_a_persisted_result_is_never_recomputed_across_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-011 holds across the split: a cache hit makes no compute call.

    `key_calls` is checked too: a hit pays one cheap key round trip, and zero would mean the client
    derives keys locally.
    """
    fake = _FakeSession(_KEY, {"log_s_mol_per_l": -2.1268648})
    _session(monkeypatch, fake)

    store = InMemoryStore()
    first, cached_first = await cached_remote(store, "predict_solubility", {"smiles": "c1ccccc1"})
    second, cached_second = await cached_remote(store, "predict_solubility", {"smiles": "c1ccccc1"})

    assert (cached_first, cached_second) == (False, True)
    assert fake.compute_calls == 1, "a persisted result was recomputed"
    assert fake.key_calls == 2, "the hit path must still ask the server for the key"
    assert first == second


async def test_a_version_carrying_both_delimiters_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key crosses as four fields, so a version containing `@` and `:` survives it.

    Splitting the flat form on either delimiter would produce a key that matches nothing, silently.
    """
    fake = _FakeSession(_KEY, {})
    _session(monkeypatch, fake)

    from chemclaw.connectors.calc.remote import calc_session

    async with calc_session() as session:
        identity = await remote_key(session, "predict_solubility", {"smiles": "c1ccccc1"})
    assert identity is not None
    # A molecule-keyed calculator is about a compound, not a geometry, so it reports no structure
    # id.
    assert identity.structure_id == ""
    key = identity.key
    assert key.calc_version == _AWKWARD_VERSION
    assert key.calc_type == "solubility"
    # Three of the four parts are the server's verbatim; `params_hash` has `CALCULATION_EPOCH`
    # folded in on this side.
    assert key.as_str().startswith(f"solubility@{_AWKWARD_VERSION}:07010a68dabf6858:")
    assert key.params_hash != "a075a6029c28d314", (
        "the epoch is not in the key: bumping CALCULATION_EPOCH would invalidate nothing"
    )
    assert key.params_hash == stable_hash(
        {"epoch": CALCULATION_EPOCH, "remote_params": "a075a6029c28d314"}
    )


async def test_a_tool_the_server_will_not_key_is_refused_rather_than_quietly_recomputed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unkeyable tool reaching the cache is a miswiring and is refused, not recomputed.

    Every tool production passes to `cached_remote` is keyable, so a fallthrough would only hide a
    future miswiring that recomputes on every call. `CalcToolError` is non-retryable, so a durable
    job fails fast and names the tool.
    """
    fake = _FakeSession(None, {"logd": 0.65})
    _session(monkeypatch, fake)

    with pytest.raises(CalcToolError, match="no derivable cache key") as refused:
        await cached_remote(InMemoryStore(), "predict_logd", {"smiles": "c1ccncc1"})
    # It names the tool and what to do instead, because the reader is whoever miswired it.
    assert "predict_logd" in str(refused.value)
    assert "remote_call" in str(refused.value)
    assert fake.compute_calls == 0, "a tool with no key must not be computed anyway"


async def test_a_refused_call_and_an_unreachable_server_are_different_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused call and an unreachable server are different failures, because a durable job acts
    on it.

    An unreachable server is fixed by a retry; a refused request is not. Asserted on the hierarchies
    `durable/publish.py` matches: `CalcToolError` is a non-retryable `ChemclawError`, and
    `CalcServerError` is a retryable `SubsystemUnavailableError`.
    """

    class _Failing(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return _Result(
                "Error executing tool predict_pka: unparameterised solvent", is_error=True
            )

    _session(monkeypatch, _Failing(_KEY, {}))

    with pytest.raises(CalcToolError, match="calculation_key failed") as refused:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    # The server's own message is the whole content of a refusal — which solvent, which index.
    assert "unparameterised solvent" in str(refused.value)

    assert issubclass(CalcToolError, ChemclawError)
    assert issubclass(CalcServerError, SubsystemUnavailableError)
    assert not issubclass(CalcServerError, ChemclawError)


async def test_a_key_answered_with_no_content_is_a_refusal_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`invoke` returns `None` for a server that answered with zero content blocks (issue #516).

    `remote_key` read `.get` off whatever came back, so that `None` would have been an
    `AttributeError` — outside every class a durable activity's retry policy reads.
    """

    class _Silent(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            result = _Result({})
            result.content = []
            return result

    _session(monkeypatch, _Silent(_KEY, {}))

    with pytest.raises(CalcToolError, match="calculation_key returned NoneType"):
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})


async def test_the_servers_internal_error_is_an_outage_not_bad_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An infrastructure fault on the calc server stays retryable, though it arrives as isError.

    The server sanitises every non-`ValueError` exception to "an internal error occurred"; xtb
    timeouts, crashes and OOMs take that path and must not be classified as bad data.
    """

    class _Broken(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return _Result(
                "Error executing tool predict_pka: an internal error occurred (error id 4a7f21c9)",
                is_error=True,
            )

    _session(monkeypatch, _Broken(_KEY, {}))

    with pytest.raises(CalcServerError) as outage:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    assert "may work on a retry" in str(outage.value)


async def test_a_full_pod_is_backpressure_not_bad_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full pod is backpressure, not bad data.

    The at-capacity refusal must not become a non-retryable `CalcToolError`, or every cache miss
    fails permanently under load. The refusal text is the literal the server sends rather than this
    repository's constant: the two repositories share no package, so the literal is the contract.
    """

    class _Full(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "calculation_key":
                return _Result({"key": _KEY})
            return _Result(
                "Error executing tool predict_pka: [calc-at-capacity] this server has 0 "
                "of its 4 calculation slots free and predict_pka needs 1, so it was "
                "refused rather than queued. Retry once one finishes",
                is_error=True,
            )

    _session(monkeypatch, _Full(_KEY, {}))

    with pytest.raises(CalcBusyError) as busy:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    # What the chemist reads must not sound like a problem with their molecule, and must not
    # repeat the server's advice to retry as if a person had to act on it.
    assert "the calculation service is busy" in str(busy.value)
    assert "Nothing is wrong with what was asked" in str(busy.value)
    assert "CHEMCLAW_CALC_MAX_CONCURRENT_REQUESTS" not in str(busy.value)

    # The classification, which is the whole fix: `SubsystemUnavailableError` is the hierarchy
    # `tests/test_publish.py` asserts is *absent* from `_BAD_DATA_TYPES`, so this is retryable by
    # construction and cannot be made non-retryable without failing a test that says why.
    assert issubclass(CalcBusyError, SubsystemUnavailableError)
    assert not issubclass(CalcBusyError, ChemclawError)
    assert not issubclass(CalcBusyError, CalcToolError)
    assert CalcBusyError.__name__ not in _BAD_DATA_TYPES
    assert "CalcToolError" in _BAD_DATA_TYPES


async def test_a_domain_refusal_is_still_bad_data_when_the_marker_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A domain refusal without the marker is still bad data.

    A marker matched too loosely would retry a bad molecule to exhaustion. A refusal that talks
    about capacity in plain English stays bad data.
    """

    class _Wordy(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "calculation_key":
                return _Result({"key": _KEY})
            return _Result(
                "Error executing tool predict_pka: this molecule has more capacity for hydrogen "
                "bonding than the model",
                is_error=True,
            )

    _session(monkeypatch, _Wordy(_KEY, {}))

    with pytest.raises(CalcToolError) as refused:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    assert not isinstance(refused.value, CalcBusyError)


async def test_the_marker_cannot_be_forged_from_a_tool_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capacity marker cannot be forged from a tool argument.

    The calc server interpolates caller strings (e.g. `solvent`) into its refusals, so a marker
    matched anywhere would let a caller turn a bad input into backpressure: retries, backoff and the
    "scale the calculation tier" alert. The counter is asserted as well as the class, because the
    alert moves on the classification.
    """

    class _Echo(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "calculation_key":
                return _Result({"key": _KEY})
            return _Result(
                "Error executing tool predict_pka: GFN2-xTB's ALPB solvation model has no "
                "parameters for '[calc-at-capacity]'. It is an implicit model with a fixed set "
                "of parameterized solvents, so an unlisted one cannot be approximated.",
                is_error=True,
            )

    _session(monkeypatch, _Echo(_KEY, {}))
    before = METRICS.value("chemclaw_calc_backend_at_capacity_total")

    with pytest.raises(CalcToolError) as refused:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    assert not isinstance(refused.value, CalcBusyError), (
        "a marker echoed back inside a domain refusal is not the server saying it is full"
    )

    assert METRICS.value("chemclaw_calc_backend_at_capacity_total") == before, (
        "a tool argument moved the saturation series the capacity alert pages on"
    )


def test_the_marker_is_read_at_the_head_where_the_server_writes_it() -> None:
    """What `server_marked` accepts and refuses.

    The transport's own `Error executing tool …:` prefix and a bare leading marker pass; a marker
    anywhere else does not, because that is where a quoted argument lands.
    """
    marker = mcp_session.SERVER_AT_CAPACITY

    assert mcp_session.server_marked(f"{marker} 0 of 4 slots free", marker)
    assert mcp_session.server_marked(
        f"Error executing tool relax_structure: {marker} 0 free", marker
    )
    assert not mcp_session.server_marked(f"no parameters for {marker!r}", marker)
    assert not mcp_session.server_marked(
        f"Error executing tool relax_structure: no parameters for {marker!r}", marker
    )
    assert not mcp_session.server_marked("Unknown tool: " + marker, marker)


def test_a_black_holed_server_fails_to_connect_in_seconds_not_quarter_hours() -> None:
    """The connect bound is the connectors' 5 s, not the calculation's 900 s.

    A black-holed pod must fail to connect in seconds. The read leg is asserted to stay long,
    because a short httpx read timeout is swallowed by the client and the caller waits forever.
    """
    composed = httpx.Timeout(settings.calc_server_timeout_seconds, read=905.0)
    factory = mcp_session.short_connect_client(settings.calc_server_timeout_seconds)
    client = factory(headers=None, timeout=composed, auth=None)

    assert client.timeout.connect == registry._CONNECT_TIMEOUT_SECONDS
    assert client.timeout.read == 905.0


class _Wire:
    """`streamablehttp_client` — an async CM yielding the `(read, write, _)` triple.

    Kept separate from the session fake: conflating them fails at the tuple unpack, which the
    connection guard catches, and the test would pass without reaching the code under test.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> "_Wire":
        return self

    async def __aenter__(self) -> tuple[None, None, None]:
        return (None, None, None)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Transport:
    """`ClientSession` — the object `calc_session` initializes and yields to its caller."""

    def __init__(self, on_call: BaseException | None = None) -> None:
        self._on_call = on_call

    def __call__(self, *args: Any, **kwargs: Any) -> "_Transport":
        return self

    async def __aenter__(self) -> "_Transport":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def initialize(self) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._on_call is not None:
            raise self._on_call
        if name == "calculation_key":
            return _Result({"key": _KEY})
        return _Result({"log_s_mol_per_l": -2.1268648})


class _RaisingStore:
    """A store whose every method raises — the local cache failing under a healthy server."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __getattr__(self, name: str) -> Any:
        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise self._exc

        return _boom


def _real_session(monkeypatch: pytest.MonkeyPatch, transport: _Transport) -> None:
    """Run the genuine `calc_session`, with only its two transport objects faked."""
    monkeypatch.setattr("chemclaw.core.mcp_session.streamablehttp_client", _Wire())
    monkeypatch.setattr("chemclaw.core.mcp_session.ClientSession", transport)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        # The live one: `core/db.py::connect` re-raises a builtin `ConnectionError`, which is
        # neither `ChemclawError` nor `SubsystemUnavailableError`.
        (ConnectionError("postgres refused the connection"), ConnectionError),
        # The one that inverted a control: relabelled, this came back out *retryable*.
        (ChemclawError("the stored row is unusable"), ChemclawError),
        (ValueError("a bug in the composition code"), ValueError),
    ],
)
async def test_a_failure_inside_the_session_body_is_not_relabelled_as_an_outage(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException, expected: type[BaseException]
) -> None:
    """`calc_session` guards the connection only, not the caller's block.

    `@asynccontextmanager` re-raises the caller's exceptions at the `yield`, so a guard there would
    relabel a store failure as a retryable `CalcServerError` outage.
    """
    _real_session(monkeypatch, _Transport())

    with pytest.raises(expected) as caught:
        await cached_remote(_RaisingStore(raised), "predict_solubility", {"smiles": "c1ccccc1"})
    assert not isinstance(caught.value, CalcServerError)


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        # FastMCP answers `-32602` for arguments that fail a tool's own schema before its body
        # runs — the "atom index past the molecule" class, which no retry changes.
        (INVALID_PARAMS, CalcToolError, False),
        (METHOD_NOT_FOUND, CalcToolError, False),
        # The server's own fault, and a retry is the only thing that fixes it.
        (INTERNAL_ERROR, CalcServerError, True),
    ],
)
async def test_a_protocol_error_is_classified_by_who_is_at_fault(
    monkeypatch: pytest.MonkeyPatch, code: int, expected: type[Exception], retryable: bool
) -> None:
    """An `McpError` is classified by its code: rejected request versus broken server."""
    _real_session(monkeypatch, _Transport(McpError(ErrorData(code=code, message="refused"))))

    with pytest.raises(expected) as caught:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    # `ChemclawError` is the non-retryable hierarchy `durable/publish.py` matches on.
    assert isinstance(caught.value, ChemclawError) is not retryable


# Every name whose value is or contains a `calc_version`. A local definition of any of these is the
# defect: it would be derived from binaries and settings this process no longer has.
_DERIVATION_NAMES = frozenset(
    {"calc_version", "_calc_version", "backend_version", "binary_version"}
)
_SRC = Path(__file__).resolve().parent.parent / "src" / "chemclaw"
_SEARCHED = (
    pytest.param(_SRC / "connectors" / "calc", id="connectors"),
    pytest.param(_SRC / "science" / "calc", id="science"),
)


@pytest.mark.parametrize("root", _SEARCHED)
def test_no_module_here_derives_a_calc_version(root: Path) -> None:
    """No module here defines a function that derives a calc version.

    A locally derived version is well-formed, matches no ledger row, and makes `calculator_trust`
    report `UNCALIBRATED` silently. Only definitions are checked: reading a version off a result is
    correct and stays legal.
    """
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in _DERIVATION_NAMES
            ):
                offenders.append(f"{path.name}:{node.lineno} defines {node.name}")

    assert not offenders, (
        "a calc_version is derived in this repository: "
        + "; ".join(offenders)
        + ". The server returns it on every result and through `calculation_key`; deriving one "
        "here produces a well-formed version matching zero calibration rows, silently."
    )


def test_the_session_bounds_the_call_with_the_timeout_that_raises() -> None:
    """`calc_session` bounds the call with the timeout that raises.

    `ClientSession(read_timeout_seconds=...)` raises `McpError`; httpx's read timeout is swallowed
    by the transport and the caller waits forever. So the session timeout must be set to
    `calc_server_timeout_seconds`, asserted on the arguments handed to the transport and session.
    """
    import asyncio
    from datetime import timedelta

    from chemclaw.connectors.calc import remote as remote_module
    from chemclaw.core import mcp_session
    from chemclaw.core.config import settings

    seen: dict[str, Any] = {}

    class _NullSession:
        """Stands in for `ClientSession`, recording the bound it was constructed with."""

        def __init__(self, _read: Any, _write: Any, read_timeout_seconds: Any = None) -> None:
            seen["session_read_timeout"] = read_timeout_seconds

        async def __aenter__(self) -> "_NullSession":
            return self

        async def __aexit__(self, *_: Any) -> bool:
            return False

        async def initialize(self) -> None:
            """Accept the handshake without a server."""

    @asynccontextmanager
    async def _transport(url: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        yield (None, None, None)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(mcp_session, "streamablehttp_client", _transport)
        monkeypatch.setattr(mcp_session, "ClientSession", _NullSession)

        async def _run() -> None:
            async with remote_module.calc_session():
                pass

        asyncio.run(_run())
    finally:
        monkeypatch.undo()

    bound = settings.calc_server_timeout_seconds
    assert seen["session_read_timeout"] == timedelta(seconds=bound), (
        "the session's own bound is the one that raises; unset means wait forever"
    )
    assert seen["sse_read_timeout"] > timedelta(seconds=bound), (
        "httpx's read timeout must stay strictly behind the session's, or the answer is lost "
        "silently instead of raising"
    )


_CONFIG_MODULE = "chemclaw.core.config"

# Modules that derive the bytes of a calculation identity; none may read a setting, because a knob
# there re-keys a deployment.
_IDENTITY_MODULES = (
    Path("science") / "calc" / "models.py",
    Path("science") / "calc" / "store.py",
    Path("core") / "ids.py",
)

# The client reads settings legitimately (URL, bearer env var, timeouts), so the rule there is
# scoped to functions that hold an `arguments` payload, derived from their signatures so a new
# payload path is covered automatically.
_PAYLOAD_CLIENT = Path("connectors") / "calc" / "remote.py"
_PAYLOAD_PARAMETER = "arguments"


def _settings_aliases(tree: ast.Module) -> set[str]:
    """Every local dotted name bound to the one settings object in this module.

    Covers `import … as` aliases and dotted `import chemclaw.core.config` access, which a check on
    the literal name `settings` would miss.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _CONFIG_MODULE:
            aliases |= {a.asname or a.name for a in node.names if a.name == "settings"}
        elif isinstance(node, ast.Import):
            aliases |= {
                f"{a.asname or a.name}.settings" for a in node.names if a.name == _CONFIG_MODULE
            }
    return aliases


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` as a string for a name/attribute chain, `None` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _settings_reads(tree: ast.AST, aliases: set[str]) -> list[str]:
    """Every settings field read under `tree`, by whichever name the module bound the object to.

    Also covers an attribute taken off a call to a `…settings` accessor.
    """
    reads: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = _dotted(node.value)
        if base is not None and base in aliases:
            reads.append(node.attr)
        elif isinstance(node.value, ast.Call):
            func = _dotted(node.value.func) or ""
            if func.rsplit(".", 1)[-1].lower().endswith("settings"):
                reads.append(node.attr)
    return reads


def test_no_setting_shapes_the_bytes_the_server_hashes() -> None:
    """No setting shapes the bytes the server hashes.

    A local knob in the identity path would make every remote cache key miss and diverge from other
    deployments, with no error. Checked statically over every module that is the wire contract,
    resolving aliases and accessors rather than matching the spelling `settings`.
    """
    offenders: list[str] = []
    for relative in _IDENTITY_MODULES:
        module = _SRC / relative
        tree = ast.parse(module.read_text(encoding="utf-8"))
        reads = _settings_reads(tree, _settings_aliases(tree))
        if reads:
            offenders.append(f"{relative} reads settings {sorted(set(reads))}")

    client = ast.parse((_SRC / _PAYLOAD_CLIENT).read_text(encoding="utf-8"))
    aliases = _settings_aliases(client)
    payload_functions = [
        node
        for node in ast.walk(client)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(arg.arg == _PAYLOAD_PARAMETER for arg in node.args.args)
    ]
    assert payload_functions, (
        f"no function in {_PAYLOAD_CLIENT} takes `{_PAYLOAD_PARAMETER}` — this check is reading a "
        "module that no longer carries the payload, so it is proving nothing."
    )
    for function in payload_functions:
        reads = _settings_reads(function, aliases)
        if reads:
            offenders.append(
                f"{_PAYLOAD_CLIENT}:{function.lineno} {function.name}() reads settings "
                f"{sorted(set(reads))} while holding the payload"
            )

    assert not offenders, (
        "a setting shapes the bytes the server hashes: "
        + "; ".join(offenders)
        + ". Every value these contribute to a `Structure`, a `CalculationKey` or a tool's "
        "`arguments` is hashed on the other side, so a deployment could re-key its own "
        "calculations — silently, because a miss is not an error."
    )


def _status_error(status: int) -> httpx.HTTPStatusError:
    """An `httpx.HTTPStatusError` carrying `status`, shaped as the transport really raises it."""
    request = httpx.Request("POST", settings.calc_server_url)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url",
        request=request,
        response=httpx.Response(status, request=request),
    )


def test_a_refused_credential_is_not_an_outage() -> None:
    """A 401 is a refusal, not an outage.

    `CalcServerError` is retryable, so misclassifying a bad bearer would spend every activity's
    retry budget on an identical refusal.
    """
    assert mcp_session.auth_rejection(_status_error(401)) == 401
    assert mcp_session.auth_rejection(_status_error(403)) == 403


def test_the_rejection_is_found_inside_the_task_groups_exception_group() -> None:
    """The HTTP status is found inside the task group's `ExceptionGroup`, not by exception type.

    `streamablehttp_client` runs its transport in an anyio task group, so the 401 arrives nested
    under `__cause__`.
    """
    nested = ExceptionGroup("unhandled errors in a TaskGroup", [_status_error(401)])
    wrapper = RuntimeError("connect failed")
    wrapper.__cause__ = nested

    assert mcp_session.auth_rejection(wrapper) == 401


def test_a_server_that_is_genuinely_down_stays_an_outage() -> None:
    """Only 401 and 403 are refusals; a 500 or 502 stays a retryable outage."""
    assert mcp_session.auth_rejection(_status_error(500)) is None
    assert mcp_session.auth_rejection(_status_error(502)) is None
    assert mcp_session.auth_rejection(ConnectionRefusedError("no listener")) is None


def test_the_refusal_lands_in_the_non_retryable_hierarchy() -> None:
    """What `durable/publish.py` actually matches on, asserted rather than assumed."""
    assert issubclass(CalcToolError, ChemclawError)
    assert not issubclass(CalcToolError, SubsystemUnavailableError)


def test_the_two_epochs_compose_rather_than_having_to_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two `CALCULATION_EPOCH` constants compose rather than having to match.

    The server folds its epoch into `params_hash` and `remote_key` folds this one on top, so a bump
    on either side moves the key. Both halves are checked: this side's bump changes the key, and the
    server's digest survives inside the composed one.
    """
    fake = _FakeSession(_KEY, {})
    _session(monkeypatch, fake)

    async def _key_now() -> str:
        from chemclaw.connectors.calc.remote import calc_session

        async with calc_session() as session:
            keyed = await remote_key(session, "predict_solubility", {"smiles": "c1ccccc1"})
        assert keyed is not None
        return keyed.key.params_hash

    served = _KEY["params_hash"]
    at_epoch = asyncio.run(_key_now())
    monkeypatch.setattr("chemclaw.connectors.calc.remote.CALCULATION_EPOCH", "an-unmerged-bump")
    bumped = asyncio.run(_key_now())

    # This side alone moved, and the address moved with it: the server answered both round trips
    # from the same `_KEY`, so nothing about its own epoch changed between them.
    assert fake.key_calls == 2
    assert bumped != at_epoch
    # ...and the server's own digest is carried whole, so its epoch reaches the address too: it is
    # already inside `served`, and `served` is inside both keys above.
    assert at_epoch == stable_hash({"epoch": CALCULATION_EPOCH, "remote_params": served})
    assert bumped == stable_hash({"epoch": "an-unmerged-bump", "remote_params": served})


def _in_flight() -> float:
    """What Prometheus would read for `chemclaw_calc_requests_in_flight` right now.

    Read off the exposition, because the claim is about what a scrape sees.
    """
    for line in METRICS.render().splitlines():
        if line.startswith("chemclaw_calc_requests_in_flight "):
            return float(line.split()[1])
    raise AssertionError("chemclaw_calc_requests_in_flight is not on the exposition")


def test_a_held_calculation_session_is_visible_to_a_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held calculation session is visible on the in-flight gauge.

    Per-process caps cannot see fleet-wide demand on the shared calc pod; only this gauge can, so it
    must be bound in every dispatching process and read current state.
    """
    seen: list[float] = []

    @asynccontextmanager
    async def _open(*args: Any, **kwargs: Any) -> Any:
        seen.append(_in_flight())
        yield _FakeSession(_KEY, {})

    monkeypatch.setattr(remote, "open_session", _open)

    async def _run() -> None:
        async with remote.calc_session():
            pass

    assert _in_flight() == 0.0
    asyncio.run(_run())
    assert seen == [1.0], "a session held against the calculation backend was invisible to a scrape"
    assert _in_flight() == 0.0


async def test_a_failed_open_does_not_leak_a_permanent_unit_of_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gauge falls on every exit path, including a failed open.

    Otherwise an outage climbs the gauge by one per attempt and the saturation alert fires on an
    idle pod.
    """

    @asynccontextmanager
    async def _refused(*args: Any, **kwargs: Any) -> Any:
        raise McpConnectFailed("nothing is listening")
        yield  # pragma: no cover - unreachable, present so this is an async generator

    monkeypatch.setattr(remote, "open_session", _refused)

    for _ in range(3):
        with pytest.raises(CalcServerError):
            async with remote.calc_session():
                pass  # pragma: no cover - the open never yields

    assert _in_flight() == 0.0


def test_every_server_s_full_pod_is_recognised_by_its_format() -> None:
    """The fleet's one at-capacity format is recognised at the head of the message.

    An echoed token anywhere else still does not count.
    """
    assert mcp_session.at_capacity("[rxnpredict-at-capacity] 0 of 2 slots free")
    assert mcp_session.at_capacity("Error executing tool predict: [pyexec-at-capacity] full")
    assert mcp_session.at_capacity(f"{mcp_session.SERVER_AT_CAPACITY} 0 of 4 slots free")
    assert not mcp_session.at_capacity("no parameters for '[calc-at-capacity]'")
    assert not mcp_session.at_capacity("Unknown tool: [calc-at-capacity]")
    assert not mcp_session.at_capacity("[Calc-at-capacity] upper case is not the format")


async def test_a_time_budget_stop_is_named_and_stays_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A time-budget stop is named (`CalcTimeBudgetError`) and stays a non-retryable refusal.

    A retry would run the same work against the same clock. The marker is the calc server's literal,
    transcribed; only the head of the message is matched.
    """

    class _Stopped(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "calculation_key":
                return _Result({"key": _KEY})
            return _Result(
                "Error executing tool predict_pka: [calc-time-budget] a geometry optimization "
                "exceeded this server's inline budget of 780s (spent 781.2s; stopped after 11 "
                "gradient evaluations past the input geometry).",
                is_error=True,
            )

    _session(monkeypatch, _Stopped(_KEY, {}))

    with pytest.raises(CalcTimeBudgetError) as stopped:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    assert "inline budget" in str(stopped.value), "the server's own sentence reaches the chemist"
    assert issubclass(CalcTimeBudgetError, CalcToolError)
    assert not issubclass(CalcTimeBudgetError, SubsystemUnavailableError)
    assert CalcTimeBudgetError.__name__ in _BAD_DATA_TYPES
    assert mcp_session.SERVER_TIME_BUDGET == "[calc-time-budget]"


async def test_a_time_budget_marker_quoted_back_is_not_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A solvent named "[calc-time-budget]" is a bad input, not a busy pod's clock."""

    class _Echo(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "calculation_key":
                return _Result({"key": _KEY})
            return _Result(
                "Error executing tool predict_pka: GFN2-xTB's ALPB solvation model has no "
                "parameters for '[calc-time-budget]'.",
                is_error=True,
            )

    _session(monkeypatch, _Echo(_KEY, {}))

    with pytest.raises(CalcToolError) as refused:
        await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
    assert not isinstance(refused.value, CalcTimeBudgetError)
