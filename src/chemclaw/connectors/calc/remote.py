"""The calculation client: ask the physics server for a key, then for the answer.

The engines moved to `Chemclaw3-mcp`'s `servers/calc`
(`D-2026-08-16-the-physics-leaves-the-cache-stays`). What stays here is the D-011 cache, the
calibration ledger and the orchestration — so every calculator in this repository is now
*lookup-then-maybe-compute* across a wire instead of in a process.

**Why two calls and not one.** `science.calc.store.cached_compute` needs the `CalculationKey`
*before* the compute, because the key is what it looks up. A result that carries its own key is
necessary but not sufficient: on a hit there is no result to read one off. So the server exposes
`calculation_key`, which derives the identity without running anything, and the sequence is
key → lookup → compute-only-on-a-miss. A hit costs one cheap round trip instead of an SCF.

**Nothing here derives a `calc_version`, and that is the point rather than an implementation
detail.** The version is half the cache key *and* the primary key of the calibration ledger
(`predictions`, unique on `(calc_type, calc_version, input_hash)`, exact-match by D-139). It is
built from `xtb --version`, distribution versions and seven calibration settings — none of which
this process can see any more. Worse, `binary_version()` answered the literal string `"absent"`
rather than raising when a binary was missing, so a client deriving its own would produce a
*well-formed* version matching zero rows: `calculator_trust("pka")` would report a confident
`UNCALIBRATED`, every historical residual would become unreachable, and nothing would look broken.
`tests/test_calc_remote.py` asserts no derivation survives in this package.

**A session per call, not one per process.** `connectors.identity` is explicit that the MCP
transport's tasks inherit the context of whoever opened the connection, so a shared session
misattributes concurrent callers to each other. The cost is a connect per calculation, which is
noise against an SCF and measurable against a cache hit — `calculation_key` is the only call on
the hit path, so that is the one worth watching.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.science.calc.store import (
    CalculationKey,
    ResultPayload,
    ResultStore,
    cached_compute,
)


class CalcServerError(SubsystemUnavailableError):
    """The calculation server could not be reached, so the calculation never began.

    **This was one error for both failures, and wiring the client into a Temporal activity is what
    made that wrong.** The original reasoning — "the caller's options are identical in every case:
    a calculation did not happen" — holds for a tool, which surfaces either one to a chemist. It is
    false for a durable job, where the two are opposites: an unreachable server is fixed by exactly
    one thing, a retry, and a refused *request* is fixed by exactly one thing that is not a retry.
    Conflating them means either burning `activity_max_attempts` on an unparameterised solvent, or
    giving up on a pod restart. So the transport failure is a `SubsystemUnavailableError` — the
    retryable hierarchy, deliberately absent from `durable/publish.py`'s non-retryable list — and a
    refusal is `CalcToolError` below.

    The message is written for the **chemist**, because `agent/tool_authz.py` hands it to the model
    verbatim: it says the calculators are unavailable and that this is an outage rather than a
    problem with what was asked. The address and the driver text ride on `__cause__`, for the log
    and the operator.
    """


class CalcToolError(ChemclawError):
    """The calculation server was reached and refused, or answered something unusable.

    Bad data by the same test as every other `ChemclawError`: an unparameterised solvent, an atom
    index past the molecule, a SMILES the predictor's domain excludes. The identical call fails
    identically on the next attempt, so it is registered non-retryable in
    `durable/publish.py::_BAD_DATA_TYPES` and a durable job fails fast instead of paying for the
    same refusal three more times.
    """


@asynccontextmanager
async def calc_session() -> AsyncIterator[ClientSession]:
    """Open one MCP session to the calculation server, with our bearer attached.

    The credential is an `httpx.Auth` rather than a per-call header for the reason
    `connectors.identity` records: MCP's per-call header callback is not applied to the
    `initialize()` that opens the connection, so a credential passed that way 401s at connect.
    """
    token = _token()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        async with streamablehttp_client(
            settings.calc_server_url,
            headers=headers,
            timeout=timedelta(seconds=settings.calc_server_timeout_seconds),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    except (CalcServerError, CalcToolError):
        raise
    except Exception as exc:
        raise CalcServerError(
            "the calculation service is not answering, so no calculation was run. This is an "
            "outage rather than a problem with what was asked; the same request will work once "
            "it is back."
        ) from exc


def _token() -> str | None:
    """The bearer from the configured environment variable, or `None` if unset.

    Unset is not an error here, and the reason is that the server decides: a development server
    started without a credential accepts an unauthenticated call, and forcing one would make this
    module refuse a request the server would have served. A server that *does* enforce one answers
    401, which surfaces as a `CalcServerError` — an outage from this side, with the status and the
    address on `__cause__` for the operator who can fix it.
    """
    import os

    return os.environ.get(settings.calc_server_token_env) or None


async def _call(session: ClientSession, tool: str, arguments: dict[str, Any]) -> Any:
    """Invoke one tool and return its decoded payload, or raise the failure the caller can act on.

    A refused tool call is a `CalcToolError` — the server was reached and said no, which no retry
    changes — and it carries the server's own message, because that message is the whole content of
    the refusal (which solvent is unparameterised, which atom index is out of range).
    """
    result = await session.call_tool(tool, arguments)
    if result.isError:
        raise CalcToolError(f"{tool} failed: {_text(result.content)}")
    text = _text(result.content)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CalcToolError(f"{tool} returned no JSON: {text[:200]}") from exc


def _text(content: Any) -> str:
    """The text of an MCP content list, joined — the shape every tool here answers in."""
    return "".join(getattr(block, "text", "") for block in content)


async def remote_key(
    session: ClientSession, tool: str, arguments: dict[str, Any]
) -> CalculationKey | None:
    """The `CalculationKey` this tool would stamp on its result, without computing anything.

    `None` when the server reports the calculation has no derivable key — `predict_logd` is the
    one such tool, because it never had a cache row of its own: its expensive half is a *cached*
    pKa and the rest is a Crippen sum. A caller that gets `None` computes without looking up,
    which is what happened before the split too.

    The key comes back as its four parts rather than as the flat `type@version:input:params`
    string, and the reason is measured rather than stylistic: a real `calc_version` contains both
    delimiters — `esol-delaney@2004` carries the `@`, `cal-0.28733:-29.3116` carries the `:` — so a
    client splitting the flat form would build a key that misses forever.
    """
    identity = await _call(session, "calculation_key", {"tool": tool, "arguments": arguments})
    key = identity.get("key")
    if key is None:
        return None
    try:
        return CalculationKey(
            calc_type=key["calc_type"],
            calc_version=key["calc_version"],
            input_hash=key["input_hash"],
            params_hash=key["params_hash"],
        )
    except (KeyError, TypeError) as exc:
        raise CalcToolError(f"calculation_key returned an unusable key for {tool}: {key}") from exc


async def remote_compute(
    session: ClientSession, tool: str, arguments: dict[str, Any]
) -> ResultPayload:
    """Run one calculation on the server and return its payload as the cache stores it."""
    payload = await _call(session, tool, arguments)
    if not isinstance(payload, dict):
        raise CalcToolError(f"{tool} returned {type(payload).__name__}, not an object")
    return payload


async def remote_call(tool: str, arguments: dict[str, Any]) -> ResultPayload:
    """One round trip to a tool that has **no cache row**, in its own session.

    Two tools on the server are like this and both are geometry rather than physics:
    `embed_structure` (ETKDG plus a force-field cleanup) and `combine_structures` (centre two
    monomers and offset one along x). `calculation_key` refuses them by name — they are not compute
    tools — so routing them through `cached_remote` would spend a round trip learning that, then
    call them anyway.

    They are on the server rather than here because their output is an *input to a key*: a geometry
    embedded by a different RDKit build is a different `structure_id`, and every cached relaxation
    and Hessian downstream of it would miss. Deriving it locally would put the two repositories'
    RDKit versions into an agreement nothing checks.
    """
    async with calc_session() as session:
        return await remote_compute(session, tool, arguments)


async def remote_version(tool: str, arguments: dict[str, Any]) -> str:
    """The `calc_version` this tool would stamp on a result, without computing one.

    The one way this repository is allowed to learn a calculator's current version, and the reason
    it needs one at all is the calibration ledger: `predictions` is keyed exactly on
    `(calc_type, calc_version, input_hash)` with no version pooling (D-139), so `calculator_trust`
    has to ask "what version would answer this question *now*" before it can score anything against
    it. A result carries its own version, but a trust report has no result — that is the question.

    `arguments` are needed because `calculation_key` derives an identity and an identity is of
    something; the version it returns does not depend on the molecule, only on the programs and the
    calibration behind the calculator. `settings.calc_version_probe_smiles` is what that argument
    is, named in configuration rather than inlined so it is one visible fact rather than a literal
    repeated at each call site.
    """
    async with calc_session() as session:
        identity = await _call(session, "calculation_key", {"tool": tool, "arguments": arguments})
    version = identity.get("calc_version")
    if not isinstance(version, str) or not version:
        raise CalcToolError(f"calculation_key returned no calc_version for {tool}: {identity}")
    return version


async def cached_remote(
    store: ResultStore, tool: str, arguments: dict[str, Any]
) -> tuple[ResultPayload, bool]:
    """One calculation: look it up by the server's own key, compute remotely only on a miss.

    This is what the twelve `run_cached_*` wrappers become, and the whole point of leaving the
    cache behind. D-011's rule is that a *persisted* result is never recomputed, and it survives
    the split unchanged — what changed is only that the miss path crosses a wire.

    One session for both calls rather than one each, which is the only sharing that is safe: both
    belong to the same caller, so there is no concurrent-caller attribution to lose
    (`connectors.identity`). On a hit the compute call is never made, so a hit costs one
    `calculation_key` round trip.

    A tool with no derivable key (`predict_logd`) computes every time and stores nothing, exactly
    as it did in-process — its cost was always the pKa underneath it, and that pKa is cached on the
    server's own key like everything else.
    """
    async with calc_session() as session:
        key = await remote_key(session, tool, arguments)
        if key is None:
            return await remote_compute(session, tool, arguments), False

        async def _compute() -> ResultPayload:
            return await remote_compute(session, tool, arguments)

        return await cached_compute(store, key, _compute)
