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

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from mcp import ClientSession
from pydantic import BaseModel, ConfigDict

from chemclaw.connectors.identity import turn_identity_hook
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.core.ids import stable_hash
from chemclaw.core.mcp_session import (
    McpConnectFailed,
    McpCredentialRefused,
    McpRequestRefused,
    McpServerFault,
    invoke,
    open_session,
)
from chemclaw.core.metrics_bridge import degraded
from chemclaw.science.calc.store import (
    CALCULATION_EPOCH,
    CalculationKey,
    ResultPayload,
    ResultStore,
    cached_compute,
)

logger = logging.getLogger(__name__)

# Every calculation in this system now crosses this wire, and until this module imported `logging`
# at all — it did not — an outage of the calculation backend produced **no first-party signal of
# any kind**: not a log line, not a counter. The classification here is already exactly right
# (`CalcServerError` for an outage, `CalcToolError` for a refusal, split across
# `durable/publish.py`'s `_BAD_DATA_TYPES` so one is retried and the other is not), and neither
# half was observable — so a dead backend burned `activity_max_attempts` on every job with only
# the Temporal SDK's own WARNING to show for it.
#
# Only the **outage** paths are counted, and that is the distinction rather than an omission. A
# refusal is the server working: an unparameterised solvent or an atom index past the molecule is
# bad data, it reaches the chemist as a written sentence, and counting it under a degradation
# marker would put a user's typo on the same series as a down pod. One subsystem name for all
# three outage sites, because to an operator they are one thing — the calculation backend is not
# answering — and three labels would split one alert into three.
# Written out at each call site rather than shared through a constant, deliberately:
# `tests/test_degraded.py` reads these arguments out of the source to bound the
# metric's label value space, and it can only do that for a literal.


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


# The transport, the timeout ordering, the credential-rejection walk and the internal-error
# string all live in `core/mcp_session.py` now — this file worked them out against a live server
# and the reaction labeller became the second client that needs every one of them. What stays
# here is the part that genuinely differs: which of the two error classes a failure belongs in,
# and the wording a chemist reads.


@asynccontextmanager
async def calc_session(timeout_seconds: float | None = None) -> AsyncIterator[ClientSession]:
    """Open one MCP session to the calculation server, and name its failures for a chemist.

    Everything about *how* the session is opened — the connection-scoped bearer, the short connect
    bound behind a long read bound, the ordering that keeps the MCP session's timeout the one that
    trips — is `core.mcp_session.open_session`. What this adds is the classification: a refused
    credential is bad data (a 401 never comes back on its own, so a durable job must not spend
    `activity_max_attempts` being told the same thing), and an unreachable host is an outage.

    `timeout_seconds` overrides the default read bound for the one call class that outgrew it: a
    CREST search is minutes to hours where every other primitive here is seconds to minutes, and a
    client bound shorter than the server's own means the server finishes a calculation nobody is
    still waiting for. Left unset it is `calc_server_timeout_seconds`, which is right for
    everything that is not sampling.
    """
    try:
        async with open_session(
            settings.calc_server_url,
            token_env=settings.calc_server_token_env,
            timeout_seconds=timeout_seconds or settings.calc_server_timeout_seconds,
            # The same hook every other connector's client carries
            # (`connectors/registry.connector_http_client`), and deliberately not a second one: it
            # brings the W3C `traceparent`, the correlation id, the actor and the session, *and*
            # the origin-strip guard that removes them again if a redirect leaves the endpoint's
            # origin. Without it this connection sent `Authorization` alone — so the most expensive
            # work in the system was the one call nobody could trace or correlate.
            request_hook=turn_identity_hook(settings.calc_server_url),
        ) as session:
            yield session
    except McpCredentialRefused as exc:
        raise CalcToolError(
            f"the calculation service refused this client's credential "
            f"(HTTP {exc.status} from {settings.calc_server_url}). The service is running and "
            f"answering; it does not accept the bearer taken from "
            f"{settings.calc_server_token_env}. Set that variable to the value the server "
            f"verifies — retrying will not help."
        ) from exc
    except McpConnectFailed as exc:
        degraded(
            logger,
            "calc_server",
            "cannot reach the calculation server at %s; no calculation was run",
            settings.calc_server_url,
        )
        raise CalcServerError(
            "the calculation service is not answering, so no calculation was run. This is an "
            "outage rather than a problem with what was asked; the same request will work once "
            "it is back."
        ) from exc


async def _call(session: ClientSession, tool: str, arguments: dict[str, Any]) -> Any:
    """Invoke one tool and return its decoded payload, in this service's error vocabulary.

    `core.mcp_session.invoke` does the calling and draws the one distinction that matters — the
    server refused you, versus the server broke — and this maps the two onto the classes a durable
    activity's retry policy reads. A refusal carries the server's own message, because that message
    is the whole content of the refusal (which solvent is unparameterised, which atom index is out
    of range).
    """
    try:
        return await invoke(session, tool, arguments)
    except McpRequestRefused as exc:
        raise CalcToolError(str(exc)) from exc
    except McpServerFault as exc:
        if exc.internal:
            degraded(
                logger,
                "calc_server",
                "the calculation server raised an internal error running %s",
                tool,
            )
            raise CalcServerError(
                f"the calculation service hit an internal error running {tool}, so no result was "
                "produced. This is a fault on the calculation service rather than a problem with "
                "what was asked; the same request may work on a retry."
            ) from exc
        degraded(
            logger,
            "calc_server",
            "the calculation server stopped answering during %s",
            tool,
        )
        raise CalcServerError(
            f"the calculation service stopped answering during {tool}, so no result was produced. "
            "This is an outage rather than a problem with what was asked."
        ) from exc


class KeyedCalculation(BaseModel):
    """A calculation's identity, and the geometry it is about when it is about one.

    Two facts from one `calculation_key` round trip, because the server answers both and this
    client used to read only the first. `structure_id` is what makes "have we already relaxed this
    conformer?" answerable at all (D-2026-08-21): a `calculation_results` row's `input_hash` is a
    digest, so nothing about a stored row said which geometry it described — the server knows,
    and says so, and the answer was being dropped on the floor.

    Empty for a molecule-keyed calculation (pKa, solubility, descriptors), which is the honest
    value: those are about a compound and not about any particular geometry of it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: CalculationKey
    structure_id: str = ""


async def remote_key(
    session: ClientSession, tool: str, arguments: dict[str, Any]
) -> KeyedCalculation | None:
    """The `CalculationKey` this tool would stamp on its result, without computing anything.

    `None` when the server reports the calculation has no derivable key. Exactly one tool answers
    that way — the server's own `predict_logd`, which has no cache row because its expensive half
    is a *cached* pKa and the rest is a Crippen sum. This repository never calls it: it composes
    logD client-side from those same two parts, so in practice every tool that reaches here is
    keyed. `cached_remote` therefore treats a `None` as a miswiring and refuses, rather than
    computing uncached forever.

    The key comes back as its four parts rather than as the flat `type@version:input:params`
    string, and the reason is measured rather than stylistic: a real `calc_version` contains both
    delimiters — `esol-delaney@2004` carries the `@`, `cal-0.28733:-29.3116` carries the `:` — so a
    client splitting the flat form would build a key that misses forever.
    """
    identity = await _call(session, "calculation_key", {"tool": tool, "arguments": arguments})
    key = identity.get("key")
    if key is None:
        return None
    # **The epoch is folded in on this side, because nothing else does it any more.**
    # `CalculationKey.build` is where `CALCULATION_EPOCH` enters a key, and after the physics left
    # it had exactly one caller — the removed DFT bundle's cache. Every `calc` key now comes
    # back from the server as its four parts and is rebuilt field-by-field here, so bumping the
    # epoch invalidated DFT rows and nothing else, while `science/calc/store.py`, `science/calc`'s
    # `__init__` and `tests/test_calc_payload_schemas.py`'s own failure message all prescribed
    # bumping it as the remedy for a stored payload changing meaning. It would have appeared to
    # work.
    #
    # Folded into `params_hash` rather than into the type or the version, so it composes with the
    # server's own params digest the same way `build` composes it with a local one, and a bump
    # invalidates every `calc` row without touching what the server considers its identity.
    try:
        return KeyedCalculation(
            key=CalculationKey(
                calc_type=key["calc_type"],
                calc_version=key["calc_version"],
                input_hash=key["input_hash"],
                params_hash=stable_hash(
                    {"epoch": CALCULATION_EPOCH, "remote_params": key["params_hash"]}
                ),
            ),
            # The server's own answer, never re-derived here — the same rule this module states for
            # `calc_version`, and for the same reason: a locally-derived value would be well-formed
            # and would match nothing. Absent for a molecule-keyed calculation.
            structure_id=str(identity.get("structure_id") or ""),
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


# Every calculation key reached inside the current `collecting()` block, in first-seen order.
# A contextvar for the reasons `chemclaw.core.turn_signals` gives for its own buffer: it is
# task-local, so two concurrent activities on one worker cannot see each other's keys; it is empty
# off that path, so the tool surface and every direct caller are unaffected; and it is *mutated*
# rather than rebound, so it stays visible when the work runs in a task of its own.
_collected: ContextVar[list[str] | None] = ContextVar("chemclaw_calc_refs", default=None)


@contextmanager
def collecting() -> Iterator[list[str]]:
    """Collect the calculation keys reached inside this block, for a run to cite afterwards.

    **Why a collector rather than a return value.** A composite reaches between two and a few dozen
    cached primitives across four call layers, and none of them is the place that reports a result —
    threading a key list back through `relax`, `hessian`, `relax_to_minimum`, `_species_energy` and
    `reaction_energy` would put cache bookkeeping in the signature of every piece of chemistry here
    for one consumer at the top.

    The consumer is the durable job, which puts them on its envelope so a note drafted from the run
    can cite what it rested on (D-2026-08-21). `propose_knowledge_note` has advertised exactly that
    since D-133 — "get them from a job's result envelope" — against an envelope that carried none.

    De-duplicated, order preserved: a reaction relaxes a shared species once and the key is reached
    once per lookup, and a citation list that repeats a key says nothing extra.
    """
    keys: list[str] = []
    token = _collected.set(keys)
    try:
        yield keys
    finally:
        _collected.reset(token)


def _record(key: CalculationKey) -> None:
    """Note one key against the enclosing `collecting()` block, if there is one."""
    keys = _collected.get()
    if keys is None:
        return
    flat = key.as_str()
    if flat not in keys:
        keys.append(flat)


async def cached_remote(
    store: ResultStore,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> tuple[ResultPayload, bool]:
    """One calculation: look it up by the server's own key, compute remotely only on a miss.

    This is what the twelve `run_cached_*` wrappers become, and the whole point of leaving the
    cache behind. D-011's rule is that a *persisted* result is never recomputed, and it survives
    the split unchanged — what changed is only that the miss path crosses a wire.

    One session for both calls rather than one each, which is the only sharing that is safe: both
    belong to the same caller, so there is no concurrent-caller attribution to lose
    (`connectors.identity`). On a hit the compute call is never made, so a hit costs one
    `calculation_key` round trip.

    **A tool the server will not key is a caller error here, not a silent uncached compute.** This
    used to fall through to computing every time, on the reasoning that `predict_logd` had no cache
    row of its own. That reasoning was right and the branch was still wrong: `predict_logd` is
    composed *client-side* from a cached remote pKa plus a local Crippen sum, so it never reaches
    this function — measured, every one of the eleven tools production actually passes here returns
    a key, and the server refuses to key exactly one tool, which is the one that never arrives. A
    branch that cannot execute is not a safety net; it is a place for a future miswiring to land
    quietly and recompute forever.
    """
    async with calc_session(timeout_seconds) as session:
        keyed = await remote_key(session, tool, arguments)
        if keyed is None:
            raise CalcToolError(
                f"{tool} has no derivable cache key, so it cannot be routed through the cache. "
                "Either it is composed here from keyed primitives (as predict_logd is), or it "
                "should be called with remote_call."
            )

        # Recorded on hit and miss alike: what a run *rested on* is the same either way, and a
        # citation list that thinned out as the cache warmed would be the least useful version of
        # itself.
        _record(keyed.key)

        async def _compute() -> ResultPayload:
            return await remote_compute(session, tool, arguments)

        return await cached_compute(store, keyed.key, _compute, structure_id=keyed.structure_id)
