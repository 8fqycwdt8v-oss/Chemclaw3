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

import pytest

from chemclaw.connectors.calc.remote import (
    CalcServerError,
    CalcToolError,
    cached_remote,
    remote_key,
)
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.core.ids import stable_hash
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
    async def _fake_session() -> Any:
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
            key = await remote_key(session, "predict_solubility", {"smiles": "c1ccccc1"})
        assert key is not None
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


def test_a_tool_with_no_derivable_key_computes_every_time_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`predict_logd` never had a cache row; the split must not invent one for it.

    Its expensive half is a *cached* pKa on the server's own key, and the rest is a Crippen sum —
    so computing twice costs two cheap calls, not two calculations. Storing under a fabricated key
    would be worse than not storing: it would serve a logD computed at one pH for a request at
    another.
    """
    fake = _FakeSession(None, {"logd": 0.65})
    _session(monkeypatch, fake)

    async def _run() -> None:
        store = InMemoryStore()
        _, first = await cached_remote(store, "predict_logd", {"smiles": "c1ccncc1"})
        _, second = await cached_remote(store, "predict_logd", {"smiles": "c1ccncc1"})
        assert (first, second) == (False, False)
        assert fake.compute_calls == 2

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
        monkeypatch.setattr(remote_module, "streamablehttp_client", _transport)
        monkeypatch.setattr(remote_module, "ClientSession", _NullSession)

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
