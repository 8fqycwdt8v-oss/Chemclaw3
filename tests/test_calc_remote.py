"""The calculation client: the cache still decides, and nothing here derives a `calc_version`.

`connectors/calc/remote.py` is what the twelve `run_cached_*` wrappers become once the physics
lives in `Chemclaw3-mcp` (`D-2026-08-16-the-physics-leaves-the-cache-stays`). Two properties carry
the whole design, and both are asserted here rather than believed:

1. **D-011 survives the wire.** A persisted result is never recomputed — the miss path just got
   longer. Driven against a fake session that counts calls, because "was it recomputed?" is a
   question about call counts and nothing else.
2. **No `calc_version` is derived in this repository.** That one is a *static* check over the
   source, not a behavioural one, because the failure it guards is silent: `binary_version()`
   answered the literal string `"absent"` rather than raising when a binary was missing, so a
   locally-derived version would be well-formed, match zero ledger rows, and make
   `calculator_trust` report a confident `UNCALIBRATED`. A test that called something would pass
   while the defect sat one import away.

The live server is deliberately not required: what this file proves needs no physics at all. The
key contract *against* a running server was measured once and recorded in the ADR — every identity
byte-identical across the two repositories — because that check needs a server and this suite must
not.
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
    CalcServerError,
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
from chemclaw.science.calc.store import CALCULATION_EPOCH, InMemoryStore

# A version carrying *both* key delimiters, which is not a contrived string: `esol-delaney@2004`
# is the solubility model's real name and `cal-0.28733:-29.3116` is the pKa calibration pair, both
# read off the running server. A client that split the flat `type@version:input:params` form would
# reassemble a different key from either one.
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
    """The `CallToolResult` shape the client reads: `isError` plus text content."""

    def __init__(self, payload: dict[str, Any], is_error: bool = False) -> None:
        import json

        self.isError = is_error
        self.content = [_Text(json.dumps(payload))]


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


def test_a_persisted_result_is_never_recomputed_across_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-011 is the reason the cache stayed behind; the split must not weaken it.

    The assertion is the *compute* count, not the elapsed time: a remote call that happened is a
    remote call, however fast. `key_calls` is checked too, because the design deliberately pays one
    cheap round trip on a hit — if that ever became zero the client would be deriving keys locally,
    which is the thing this module exists to prevent.
    """
    fake = _FakeSession(_KEY, {"log_s_mol_per_l": -2.1268648})
    _session(monkeypatch, fake)

    async def _run() -> None:
        store = InMemoryStore()
        first, cached_first = await cached_remote(
            store, "predict_solubility", {"smiles": "c1ccccc1"}
        )
        second, cached_second = await cached_remote(
            store, "predict_solubility", {"smiles": "c1ccccc1"}
        )

        assert (cached_first, cached_second) == (False, True)
        assert fake.compute_calls == 1, "a persisted result was recomputed"
        assert fake.key_calls == 2, "the hit path must still ask the server for the key"
        assert first == second

    asyncio.run(_run())


def test_a_version_carrying_both_delimiters_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key crosses as four fields, so a version containing `@` and `:` survives it.

    This is why `remote_key` reads an object rather than splitting the flat form. With
    `esol-delaney@2004/…/cal-0.28733:-29.3116`, a split on `@` takes the type as
    `solubility@esol-delaney` and a split on `:` takes the input hash as `-29.3116` — either one a
    key that matches nothing, forever, with no error anywhere.
    """
    fake = _FakeSession(_KEY, {})
    _session(monkeypatch, fake)

    async def _run() -> None:
        from chemclaw.connectors.calc.remote import calc_session

        async with calc_session() as session:
            identity = await remote_key(session, "predict_solubility", {"smiles": "c1ccccc1"})
        assert identity is not None
        # `remote_key` answers with the key *and* the geometry the calculation runs on
        # (D-2026-08-21) — the server reports both from one round trip and this client used to
        # read only the first. A molecule-keyed calculator is about a compound and not about any
        # particular geometry of it, so this one reports none, which is the honest value.
        assert identity.structure_id == ""
        key = identity.key
        assert key.calc_version == _AWKWARD_VERSION
        assert key.calc_type == "solubility"
        # Three of the four parts are the server's verbatim; `params_hash` is deliberately not.
        # `CALCULATION_EPOCH` is folded into it here because `CalculationKey.build` — the only
        # place that ever folded it in — has no `calc` caller left since the physics moved, so a
        # bump invalidated the DFT rows and nothing else while three documents prescribed it as the
        # remedy for a changed payload meaning.
        assert key.as_str().startswith(f"solubility@{_AWKWARD_VERSION}:07010a68dabf6858:")
        assert key.params_hash != "a075a6029c28d314", (
            "the epoch is not in the key: bumping CALCULATION_EPOCH would invalidate nothing"
        )
        assert key.params_hash == stable_hash(
            {"epoch": CALCULATION_EPOCH, "remote_params": "a075a6029c28d314"}
        )

    asyncio.run(_run())


def test_a_tool_the_server_will_not_key_is_refused_rather_than_quietly_recomputed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unkeyable tool reaching the cache is a miswiring, and it now says so.

    This used to fall through and compute every time, on the reasoning that `predict_logd` has no
    cache row of its own — which is true, and was still the wrong branch. `predict_logd` is composed
    *client-side* from a cached remote pKa plus a local Crippen sum, so it never arrives here at
    all: measured against the running server, every one of the eleven tools production passes to
    `cached_remote` returns a key, and the single tool the server refuses to key is the one that
    never comes. So the fallthrough was unreachable, and an unreachable fallthrough is not a safety
    net — it is where a future miswiring lands silently and recomputes an expensive calculation on
    every call, forever.

    The refusal is a `CalcToolError` because that is what it is: the server was reached and said
    this has no identity. Non-retryable, so a durable job fails fast and names the tool instead of
    paying for the same answer three more times.
    """
    fake = _FakeSession(None, {"logd": 0.65})
    _session(monkeypatch, fake)

    async def _run() -> None:
        with pytest.raises(CalcToolError, match="no derivable cache key") as refused:
            await cached_remote(InMemoryStore(), "predict_logd", {"smiles": "c1ccncc1"})
        # It names the tool and what to do instead, because the reader is whoever miswired it.
        assert "predict_logd" in str(refused.value)
        assert "remote_call" in str(refused.value)
        assert fake.compute_calls == 0, "a tool with no key must not be computed anyway"

    asyncio.run(_run())


def test_a_refused_call_and_an_unreachable_server_are_different_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one distinction a durable job acts on, and the reason it is not one error any more.

    They were one, on the reasoning that "the caller's options are identical in every case: a
    calculation did not happen". That holds for a tool, which surfaces either to a chemist. It is
    false for a Temporal activity, where the two are opposites: an unreachable server is fixed by
    exactly one thing — a retry — and a refused request is fixed by exactly one thing that is not a
    retry. Conflated, the durable jobs either burn `activity_max_attempts` on an unparameterised
    solvent or give up on a pod restart.

    So the classification is asserted on the *hierarchies*, which is what
    `durable/publish.py` matches on: `CalcToolError` is a `ChemclawError` (registered non-retryable,
    checked by `tests/test_publish.py`'s completeness walk) and `CalcServerError` is a
    `SubsystemUnavailableError` (deliberately absent from that list).
    """

    class _Failing(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return _Result({"detail": "unparameterised solvent"}, is_error=True)

    _session(monkeypatch, _Failing(_KEY, {}))

    async def _run() -> None:
        with pytest.raises(CalcToolError, match="calculation_key failed") as refused:
            await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
        # The server's own message is the whole content of a refusal — which solvent, which index.
        assert "unparameterised solvent" in str(refused.value)

    asyncio.run(_run())
    assert issubclass(CalcToolError, ChemclawError)
    assert issubclass(CalcServerError, SubsystemUnavailableError)
    assert not issubclass(CalcServerError, ChemclawError)


def test_the_servers_internal_error_is_an_outage_not_bad_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An infrastructure fault on the calc server must stay retryable, though it arrives as isError.

    This is the door the split above did not cover. FastMCP turns *every* exception in a tool body
    into `isError=True`, and `Chemclaw3-mcp`'s `mcp_server_kit.app._sanitize_tool_errors` replaces
    anything that is not a deliberate `ValueError` with the literal "an internal error occurred" —
    which is the path `Chemclaw3-mcp:servers/calc/src/chemclaw_mcp_calc/engine/xtb_cli.py` takes
    *by design*, since `CliError` is a `RuntimeError`.

    So an xtb subprocess timeout, a non-zero exit, a full scratch directory and an OOM all looked
    exactly like an unparameterised solvent, and `CalcToolError` is registered non-retryable: the
    single most likely fault on that server failed an expensive durable job on attempt 1 with
    `activity_max_attempts` untouched.

    The refusal case in the test above still classifies as bad data, which is what makes this a
    distinction rather than a blanket loosening.
    """

    class _Broken(_FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return _Result({"detail": "an internal error occurred"}, is_error=True)

    _session(monkeypatch, _Broken(_KEY, {}))

    async def _run() -> None:
        with pytest.raises(CalcServerError) as outage:
            await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
        assert "may work on a retry" in str(outage.value)

    asyncio.run(_run())


def test_a_black_holed_server_fails_to_connect_in_seconds_not_quarter_hours() -> None:
    """The connect bound is the connectors' 5 s, not the 900 s a calculation is allowed to take.

    `streamablehttp_client(timeout=…)` composes one `httpx.Timeout` for connect, write and pool
    alike, so `calc_server_timeout_seconds` — necessarily large, these are the calculations — also
    became the time a deleted pod had to accept a TCP connection. Measured before the factory:
    `connect 900.0`. A durable activity stalled fifteen minutes per attempt while its heartbeat
    reported it healthy.

    The read leg is asserted too, because it must stay long: shortening it is the measured hang
    `calc_session` documents, where the client swallows a read timeout and the caller waits forever.
    """
    composed = httpx.Timeout(settings.calc_server_timeout_seconds, read=905.0)
    factory = mcp_session.short_connect_client(settings.calc_server_timeout_seconds)
    client = factory(headers=None, timeout=composed, auth=None)

    assert client.timeout.connect == registry._CONNECT_TIMEOUT_SECONDS
    assert client.timeout.read == 905.0


class _Wire:
    """`streamablehttp_client` — an async CM yielding the `(read, write, _)` triple.

    Deliberately a *separate* fake from the session below. Conflating the two is not a hypothetical
    slip: it fails at the tuple unpack, the connection guard catches that, and every test built on
    it then passes for the wrong reason — the code under test is never reached at all.
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
def test_a_failure_inside_the_session_body_is_not_relabelled_as_an_outage(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException, expected: type[BaseException]
) -> None:
    """`calc_session` guards the *connection*, and nothing else — measured, not assumed.

    `@asynccontextmanager` re-injects whatever the caller's block raises back into the generator at
    its `yield`, so a guard wrapping the yield catches the caller's own exceptions too. That was
    live: `cached_remote` runs `store.get`/`store.put` inside the block, so a Postgres outage was
    reported to the chemist as "the calculation service is not answering" — the wrong subsystem
    entirely — and a `ChemclawError` from the store came back out as `CalcServerError`, which is
    *retryable*. A durable job then burned `activity_max_attempts` on data no retry could fix,
    which is precisely the inversion the two error classes exist to prevent.
    """
    _real_session(monkeypatch, _Transport())

    async def _run() -> None:
        with pytest.raises(expected) as caught:
            await cached_remote(_RaisingStore(raised), "predict_solubility", {"smiles": "c1ccccc1"})
        assert not isinstance(caught.value, CalcServerError)

    asyncio.run(_run())


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
def test_a_protocol_error_is_classified_by_who_is_at_fault(
    monkeypatch: pytest.MonkeyPatch, code: int, expected: type[Exception], retryable: bool
) -> None:
    """An `McpError` is two opposite failures wearing one type, told apart by its code.

    `session.call_tool` raises `McpError` identically for a request the server rejected and for a
    server that broke mid-call. Classified as one, either the durable jobs retry an unparameterised
    solvent to exhaustion or they give up on a pod restart. The code is the only thing that
    separates them, so it is what `_call` reads.
    """
    _real_session(monkeypatch, _Transport(McpError(ErrorData(code=code, message="refused"))))

    async def _run() -> None:
        with pytest.raises(expected) as caught:
            await cached_remote(InMemoryStore(), "predict_pka", {"smiles": "CC(=O)O"})
        # `ChemclawError` is the non-retryable hierarchy `durable/publish.py` matches on.
        assert isinstance(caught.value, ChemclawError) is not retryable

    asyncio.run(_run())


# Every name whose value is or contains a `calc_version`. A local definition of any of these is the
# defect: it would be derived from binaries and settings this process no longer has.
_DERIVATION_NAMES = frozenset(
    {"calc_version", "_calc_version", "backend_version", "binary_version"}
)
_SRC = Path(__file__).resolve().parent.parent / "src" / "chemclaw"
_SEARCHED = (
    pytest.param(_SRC / "connectors" / "calc", id="connectors"),
    # `science/calc` carried nine derivations while the in-process engines sat beside the client —
    # `xtb_spec`, `pka`, `solubility`, `descriptors`, `complexes` and the two `binary_version`s — so
    # this parameter was `xfail(strict=True)` and its marker was the migration's own finish line.
    # The engines are gone; the marker went with them, which is exactly what `strict=True` was for.
    pytest.param(_SRC / "science" / "calc", id="science"),
)


@pytest.mark.parametrize("root", _SEARCHED)
def test_no_module_here_derives_a_calc_version(root: Path) -> None:
    """The one rule the whole transport rests on, checked statically because the failure is silent.

    A locally-derived version does not raise and does not look wrong. `binary_version()` answered
    `"absent"` rather than raising when the binary was missing, so a pod without xtb would build a
    well-formed string, match **zero** rows in a ledger keyed exactly on `(calc_type, calc_version,
    input_hash)`, and `calculator_trust` would report `UNCALIBRATED` — the state D-139 built that
    machinery to distinguish, reached by a route it never anticipated, with every historical
    residual unreachable at the same time.

    Definitions only, never references: reading a version off a result is the correct thing to do
    and must stay legal. The check is therefore on `def <name>`, which is what deriving one looks
    like.
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
    """The function every other test in this file patches away, and what hid inside it.

    `calc_session` is monkeypatched wholesale by every test above — reasonably, since none of them
    wants a socket — with the consequence that the function is *never executed as written*. Three
    defects lived there behind that.

    The one this pins is the timeout pair. `connectors/registry.py` records the measurement:
    `ClientSession(read_timeout_seconds=...)` is the bound that *raises* (`McpError`), while httpx's
    read timeout is caught by `mcp.client.streamable_http` at debug level with no reconnect — so
    when the invisible one fires first the answer is lost silently and the caller waits forever.
    This function had `read_timeout_seconds` unset, which upstream documents as waiting forever, and
    `timeout=` sets connect/write/pool only — so the only live bound was `sse_read_timeout`'s
    un-overridden **300 s** default, not the 900 s `calc_server_timeout_seconds` names. A CREST
    search past five minutes never returned while `durable/heartbeat` kept heartbeating, so Temporal
    saw a healthy activity and the job burned its full four hours.

    Asserted on the arguments actually handed to the transport and the session, because the values
    are the whole finding — a test that only checked "a session was opened" would have passed
    throughout.
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

# Every module that derives the bytes an identity is made of, and where therefore *no* setting may
# be read at all. Three rather than one, because the read that re-keys a deployment does not have to
# happen in the model: `store.CalculationKey.build` assembles the key and folds in
# `CALCULATION_EPOCH`, and `core/ids.stable_hash` is the digest under both. A knob in any of them
# has the identical consequence, and a single-file check said nothing about two of them.
_IDENTITY_MODULES = (
    Path("science") / "calc" / "models.py",
    Path("science") / "calc" / "store.py",
    Path("core") / "ids.py",
)

# The client is not on that list, because it legitimately reads settings: the server URL, the
# bearer's env var and two timeouts. None of those is a byte on the wire — a socket bound is not
# an argument.
# What *is* on the wire is the `arguments` mapping, so the rule for this module is scoped to the
# functions that hold one: a payload-carrying function may not read a setting, because anything it
# reads can be folded into what the server hashes. Derived from the signature rather than from a
# hand-kept list of function names, so a new payload path is covered the day it is written.
_PAYLOAD_CLIENT = Path("connectors") / "calc" / "remote.py"
_PAYLOAD_PARAMETER = "arguments"


def _settings_aliases(tree: ast.Module) -> set[str]:
    """Every local dotted name bound to the one settings object in this module.

    The whole point of the alias walk: `from chemclaw.core.config import settings as cfg` binds the
    same object under a name a check keyed on the literal `settings` cannot see, and `import
    chemclaw.core.config` reaches it through a dotted prefix instead. Both are ordinary Python and
    neither is unusual enough to notice in review.
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

    An accessor is covered as well as a name: an attribute taken off the result of a call whose
    function is named `…settings` (`get_settings().x`, `Settings().x`) is the same read through one
    more door, and a check that only knew names would be blind to it the day someone adds one.
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
    """The sibling rule to the version check, and it failed the same way: silently.

    `Structure` normalization used to round coordinates to `settings.xtb_geometry_decimals`, an
    ordinary ENV-overridable field. Those bytes are what crosses the wire, so it was a local knob
    shaping half of a *remote* cache key: an operator who changed it made every relaxation, Hessian,
    scan point and CREST search miss forever and diverge from every other deployment, with nothing
    raising — a miss is not an error, it is a recomputation.

    Checked statically over the modules that *are* the wire contract, because the failure has no
    observable symptom other than a bill.

    **An audit defeated the first version of this while it stayed green**, twice over, and both
    defeats are the same mistake: it checked a spelling instead of a meaning.

    - It matched `ast.Attribute` on a bare `ast.Name` called `settings`, so
      `from chemclaw.core.config import settings as cfg` — one alias, the same object, the same
      consequence — read the knob straight back into `_GEOMETRY_DECIMALS` and passed. Fixed by
      resolving the import: whatever local name the module bound that object to is the name checked
      (`_settings_aliases`), including a dotted `import chemclaw.core.config` and a `…settings()`
      accessor.
    - It read one file, and the identity is not assembled in one file. `store.CalculationKey.build`
      and `core/ids.stable_hash` shape the same bytes, and the client builds the `arguments` the
      server hashes. All four are checked now — the client by the rule its own settings reads
      require (see `_PAYLOAD_CLIENT`), which is a scope rather than an exemption.
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
    """A 401 is a refusal, and calling it an outage costs a durable job its whole retry budget.

    Measured against a live `calc` server before this was classified: a bad bearer produced
    `CalcServerError("the calculation service is not answering ... the same request will work once
    it is back")`, of which every clause is false. The service answered — with 401. It *is* a
    problem with what was asked. And it will never work once "it is back", because it never left.

    The cost is not the wording. `CalcServerError` is `SubsystemUnavailableError`, which is
    retryable by construction, so every calculator activity spent `activity_max_attempts` — each
    one behind a heartbeat-detection window — being refused identically. That is precisely the
    inversion `CalcToolError` exists to prevent, surviving at the one boundary that still collapsed
    it.
    """
    assert mcp_session.auth_rejection(_status_error(401)) == 401
    assert mcp_session.auth_rejection(_status_error(403)) == 403


def test_the_rejection_is_found_inside_the_task_groups_exception_group() -> None:
    """The status is nested, so neither `except HTTPStatusError` nor one `__cause__` hop sees it.

    `streamablehttp_client` runs its transport in an anyio task group, so the real shape at this
    boundary is `ExceptionGroup(... [HTTPStatusError(401)])` reached through `__cause__`. This is
    the assertion that would have failed had the fix caught the exception by type, which was the
    obvious implementation and the wrong one.
    """
    nested = ExceptionGroup("unhandled errors in a TaskGroup", [_status_error(401)])
    wrapper = RuntimeError("connect failed")
    wrapper.__cause__ = nested

    assert mcp_session.auth_rejection(wrapper) == 401


def test_a_server_that_is_genuinely_down_stays_an_outage() -> None:
    """The negative half: only 401 and 403 are refusals.

    A 500 or a 502 is the server failing and may well pass on retry, so it must keep the retryable
    classification. A fix that turned every HTTP status into bad data would trade one wrong answer
    for another.
    """
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
    """Two constants named `CALCULATION_EPOCH`, one address, and no equality between them.

    `Chemclaw3-mcp`'s `servers/calc` folds *its* epoch into the `params_hash` it returns, and
    `remote_key` folds *this* one in on top. That composition is the whole relationship: the stored
    address moves when either side bumps, and neither side can see the other's value — so requiring
    them to be equal is a coupling the code does not have, and one that would fail on a legitimate
    one-sided bump.

    Asserted rather than argued, because the two numbers *look* like they must match and a reader
    who assumes it will "fix" one of them. Both halves are checked here: this side's bump changes
    the key while the server's answer is byte-identical, and the server's own digest survives
    verbatim inside the composed one, which is what makes its bump reach the key too.
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

    Read off the exposition rather than the module's counter, because the claim is about what a
    scrape sees: a counter that is right and a gauge that is not bound are the same outage from the
    alert's point of view.
    """
    for line in METRICS.render().splitlines():
        if line.startswith("chemclaw_calc_requests_in_flight "):
            return float(line.split()[1])
    raise AssertionError("chemclaw_calc_requests_in_flight is not on the exposition")


def test_a_held_calculation_session_is_visible_to_a_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live half of the calculation backend's admission budget (BS-07).

    `worker_max_concurrent_activities` caps one worker *process* and `servers/calc` is one shared
    pod, so the fleet's real demand is that cap times the replica count — and the `calc` bundle's
    own MCP server pods dispatch there too, from a tool call, with no per-process cap at all.
    `Settings` refuses a bad *product* at startup; only this gauge can see the sum, so it has to be
    bound in every process that dispatches and it has to read *current* state.
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


def test_a_failed_open_does_not_leak_a_permanent_unit_of_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gauge must fall on every exit path, and an outage is the one that repeats.

    A backend that is down fails every open, so a count that only decremented on success would
    climb by one per attempt and the saturation alert would fire on an idle pod — the classic way a
    load signal becomes noise nobody acts on.
    """

    @asynccontextmanager
    async def _refused(*args: Any, **kwargs: Any) -> Any:
        raise McpConnectFailed("nothing is listening")
        yield  # pragma: no cover - unreachable, present so this is an async generator

    monkeypatch.setattr(remote, "open_session", _refused)

    async def _run() -> None:
        for _ in range(3):
            with pytest.raises(CalcServerError):
                async with remote.calc_session():
                    pass  # pragma: no cover - the open never yields

    asyncio.run(_run())
    assert _in_flight() == 0.0
