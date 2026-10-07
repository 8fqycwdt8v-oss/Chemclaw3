"""Per-tool authorization as tool-call middleware.

Where `chemclaw.agent.audit` records every tool call, this gates it: `enforce_tool_authz` asks
`chemclaw.agent.authz.authorize_tool` whether the turn's user may invoke the tool, uniformly for
every tool rather than per tool. The decisions are framework-free functions here and in
`chemclaw.agent.authz`; the `wrap_tool_call` wrappers at the end are only wiring. Safe to attach
unconditionally (`authorize_tool` is a no-op unless `entra_required`). Attach them inside the audit
middleware so a denied attempt is still recorded.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from chemclaw.agent.audit import refusal_reason, returned_failure
from chemclaw.agent.authz import (
    AuthorizationError,
    authorize_tool,
    changes_the_conversation,
    side_effecting_call,
    side_effecting_tools,
)
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK, defang
from chemclaw.agent.refusal_route import routed, sentence_of
from chemclaw.agent.tool_result_size import bounded_for_batch
from chemclaw.connectors.transport import transport_failure
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.core.turn_flags import is_dry_run
from chemclaw.core.turn_signals import record_tool_failure

logger = logging.getLogger(__name__)

# How much of a failure message reaches the trace. Long enough for a chemist to recognise the
# problem, short enough that an unexpected exception's text cannot flood the stream.
_FAILURE_CHARS = 300


class DryRunRefusal(AuthorizationError):
    """A side-effecting tool was called on a turn the caller marked `dry_run`.

    An `AuthorizationError`, so the audit middleware records it and `surface_authorization_denials`
    relays it verbatim; its own class so "you lack a role" and "you asked me not to do this" stay
    distinguishable.
    """


class UndeclaredWriteRefusal(AuthorizationError):
    """A side-effecting tool was called by a narrowed agent that was never given it.

    It enforces nothing (the tool is not bound, so it cannot run) but it changes what the model and
    the audit trail read. Without it, `ToolNode` answers with `status="error"` (the retry flag) and
    "not a valid tool, try one of [...]", inviting a retry and enumerating the agent's whole
    remaining inventory into the transcript and the audit `detail`.

    An `AuthorizationError`, so it is recorded and relayed verbatim as a refusal, with its own name
    to distinguish "never given this tool" from "your account may not use it".
    """


# --- the decisions, framework-free ---------------------------------------------------------------
#
# Each decision below is one sentence of policy, kept apart from the framework plumbing, which is
# what changes when a library does.


def dry_run_refusal(name: str, arguments: Mapping[str, Any]) -> DryRunRefusal | None:
    """The refusal a side-effecting call earns on a dry-run turn, or `None` to let it through.

    Takes the arguments too, because `write_file` under `/memories/` is durable and under
    `/scratch/` is not (see `authz.side_effecting_call`, and `authz.changes_the_conversation` for
    the handoff this gate refuses).
    """
    if not is_dry_run():
        return None
    if side_effecting_call(name, arguments):
        sentence = (
            f"DRY RUN — {name} changes stored data or starts work, so it was not called. "
            "Nothing was started; re-ask without dry-run to do it."
        )
        path = (
            "every read-only tool still runs — look up what the real call would need, and "
            "say what you would have done"
        )
    elif changes_the_conversation(name):
        # Its own wording: a handoff changes no stored data; its harm on a dry run is moving this
        # and later turns to another agent.
        sentence = (
            f"DRY RUN — {name} would move this conversation, and every later turn, to another "
            "agent, so it was not taken. The conversation stays with you; re-ask without dry-run "
            "to hand over."
        )
        path = (
            "answer the chemist yourself with what you have, and name the agent you would have "
            "handed to and what you wanted from it"
        )
    else:
        return None
    return DryRunRefusal(
        routed(
            sentence,
            code="dry_run",
            boundary="this turn's dry-run flag",
            who_can_act="the chemist, by asking again without dry run",
            sanctioned_path=path,
        )
    )


def undeclared_write_refusal(name: str, held: frozenset[str]) -> UndeclaredWriteRefusal | None:
    """The refusal a withheld tool earns from an agent narrowed away from it, or `None`.

    `held` is the profile's resolved `tool_names`. Only a name outside it that this deployment
    withholds on purpose is worded as a refusal; any other unknown name is a hallucination or stale
    name, and calling it "refused" would be false.

    Two withholding reasons are read from the sets that own them: `side_effecting_tools()` and
    `SPEAKS_TO_THE_CHEMIST` (e.g. `ask_clarifying_question`, a read that must not be reclassified as
    side-effecting, which would pull it into the plan and dry-run gates).
    """
    # Deferred import: `agent/subagents.py` reaches this chain through the agent builder.
    from chemclaw.agent.subagents import SPEAKS_TO_THE_CHEMIST

    if name in held:
        return None
    if name in side_effecting_tools():
        return UndeclaredWriteRefusal(
            routed(
                f"{name} changes stored data or starts work, and this agent was not given it, so "
                "it was not called. Nothing was started; say what you could not do and continue "
                "with what you can.",
                code="tool_withheld_write",
                boundary="the tools this agent was built with",
                who_can_act="an agent holding the full set; this one was narrowed on purpose",
                sanctioned_path=(
                    "continue with the tools you do hold, and name this one in what you report back"
                ),
            )
        )
    if name in SPEAKS_TO_THE_CHEMIST:
        # A second sentence: this tool changes nothing, so "changes stored data or starts work"
        # would misstate the reason.
        return UndeclaredWriteRefusal(
            routed(
                f"{name} reaches the chemist directly, and this agent was not given it, so it was "
                "not called. Nothing was asked; answer from what you have, or say in your own "
                "answer what you would have needed to ask.",
                code="tool_withheld_reaches_the_chemist",
                boundary="the tools this agent was built with",
                who_can_act="whatever this agent reports back to, which does reach the chemist",
                sanctioned_path=(
                    "answer from what you have, naming in that answer what you would have asked"
                ),
            )
        )
    return None


def denial_result(exc: AuthorizationError) -> str:
    """What the model is told when a call was refused — the message verbatim, never swallowed.

    Unmarked here: `_refusal_message` defangs what it is handed, which would escape this system's
    own mark, so marking is `_refusal_message(marked=True)`'s job after the defang.
    """
    return f"Refused: {exc}"


def domain_error_result(exc: BaseException) -> str:
    """What the model is told when a tool raised one of the two deliberately-safe error types."""
    return f"Error: {exc}"


def failure_detail(exc: BaseException) -> str:
    """What the *chemist's* transcript is told a tool raised, bounded so it cannot flood.

    `sentence_of` drops a refusal's routing footer, which is written for the model and would
    otherwise push the sentence past `_FAILURE_CHARS`. Text without a footer is unchanged.
    """
    return f"{type(exc).__name__}: {sentence_of(str(exc))}"[:_FAILURE_CHARS]


def returned_failure_detail(message: ToolMessage) -> str:
    """The same sentence for a tool that *returned* its failure instead of raising one.

    Separate from `failure_detail` because a returned failure has no exception class, only the
    server's own words. `message.text` rather than `message.content`, which for MCP is a list of
    content blocks. Bounded by `_FAILURE_CHARS`, since remote errors can be arbitrarily long.
    """
    return message.text[:_FAILURE_CHARS]


def answered_failure(message: ToolMessage) -> ToolMessage:
    """The same returned failure, minus the flag a provider reads as "retry this".

    In-process and job tools fail by raising, and the converters answer with `_refusal_message`,
    which deliberately avoids `status="error"` (Anthropic's `is_error`, an invitation to retry). An
    MCP tool never raises: the adapter returns `ToolMessage(status="error")`. This clears that flag
    so connector refusals read the same way. The server's words are kept verbatim; they are already
    sanitized by `connectors/server.py`.

    Only what the model reads changes: the audit trail and `announce_tool_failures` run inside this
    converter and have already recorded the failure, which is why the flag is not cleared lower, at
    the MCP seam. `api/graph_stream.py` reads the same `status`, so a connector failure now reaches
    the trace as a `tool_result` beside the `tool_failed`, as raised failures already did.
    """
    return message.model_copy(update={"status": "success"})


def transport_error_result(name: str, exc: BaseException) -> str:
    """What the model is told when the *wire* to a connector failed, not the tool behind it.

    Unlike `unexpected_error_result`, this permits a retry: tool-level failures are returned, so a
    raised one is a timeout, reset or dead session, and the repeat guard bounds retries. Only the
    exception's type is named; its text can carry internal addresses.
    """
    return (
        f"Error: the connector serving {name} did not answer ({type(exc).__name__}) — the call "
        "failed in transport, not in the tool, and no result is known. One retry may succeed; if "
        "it fails again, continue without it and say what is missing."
    )


def unexpected_error_result() -> str:
    """What the model is told when a tool raised something outside the two safe families.

    Says nothing about the exception, whose text can carry a DSN, a path or a row of data. The
    chemist's transcript still gets the type and message through `announce_tool_failures`.
    """
    return (
        "Error: that tool failed unexpectedly and returned nothing. Do not retry it with the same "
        "arguments; say what you were unable to do, and continue with what you can."
    )


# --- the wiring ----------------------------------------------------------------------------------
#
# A gate stops a call by returning a `ToolMessage` instead of calling `handler`, built by the shared
# `_refusal_message`.


def _refusal_message(request: Any, text: str, *, marked: bool = False) -> ToolMessage:
    """A tool result the model reads as this call's answer, carrying the id it must reply to.

    `tool_call_id` is required: a `tool_use` with no matching `tool_result` is rejected by the
    provider. `name` is filled in to match what `ToolNode` sets on every result.

    Deliberately not `status="error"`, which reaches Anthropic as `is_error` and invites a retry; a
    refusal is the call's answer.

    Defanged and bounded here, because a tool that fails by raising bypasses the inner framing and
    bounding middlewares. Defanged, not framed: framing would tell the model to treat this system's
    own refusal as untrusted evidence, while interpolated text still must not carry a live
    delimiter. Order: defang (which can grow the text), then the mark (`marked=True`, for access
    decisions, appended after defanging so it is not escaped), then `bounded_for_batch`, which keeps
    head and tail so a long refusal still ends in the mark.
    """
    marker = f" {SYSTEM_SPEECH_MARK}" if marked else ""
    return ToolMessage(
        content=bounded_for_batch(request, defang(text) + marker),
        tool_call_id=request.tool_call["id"],
        name=str(request.tool_call["name"]),
    )


@wrap_tool_call
async def enforce_tool_authz(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Block a tool call the turn's user is not authorized for, else run it unchanged."""
    authorize_tool(request.tool_call["name"])
    return await handler(request)


def refuse_undeclared_writes(held: frozenset[str]) -> Any:
    """Middleware wording an undeclared write's refusal for a profile narrowed to `held`.

    A factory because the answer depends on which agent this is. Attached by
    `langgraph_agent.tool_governance_middleware` only when a profile narrows. It runs even for a
    tool the graph does not hold: `ToolNode` passes `tool=None` and defers validation precisely so
    interceptors can short-circuit unregistered names.
    """

    @wrap_tool_call(name="refuse_undeclared_writes")
    async def _refuse(request: Any, handler: Callable[[Any], Any]) -> Any:
        """Refuse a side-effecting tool this profile was narrowed away from."""
        refusal = undeclared_write_refusal(request.tool_call["name"], held)
        if refusal is not None:
            raise refusal
        return await handler(request)

    # Named explicitly, because `wrap_tool_call` uses the function's name as the middleware's, and
    # traces should name the rule.
    return _refuse


@wrap_tool_call
async def refuse_writes_on_dry_run(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse any side-effecting tool while the turn is a dry run (`refuse_writes_on_dry_run`)."""
    refusal = dry_run_refusal(request.tool_call["name"], request.tool_call.get("args") or {})
    if refusal is not None:
        raise refusal
    return await handler(request)


@wrap_tool_call
async def surface_authorization_denials(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Hand a denial to the model verbatim (`surface_authorization_denials`).

    Keeps a deliberately worded refusal from being reported as a tool error the model might retry.
    Attached outside the audit middleware, so the denial is recorded first.
    """
    try:
        return await handler(request)
    except AuthorizationError as exc:
        return _refusal_message(request, denial_result(exc), marked=True)


@wrap_tool_call
async def surface_domain_errors(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Turn any tool exception into a result the model can read, rather than ending the turn.

    The two safe families keep their own words; everything else becomes a contentless
    `unexpected_error_result` (its text is not vetted for the model), while the transcript still
    carries type and message. Otherwise an arbitrary exception would escape `ToolNode` and kill the
    whole turn. `BaseException` is not caught: `CancelledError` carries disconnects and the turn
    deadline.

    A tool can also fail without raising (connectors return `ToolMessage(status="error")`), so this
    converter also inspects what came back (`answered_failure`): how a failure was signalled is the
    transport's property, not something the model's answer should depend on.
    """
    try:
        result = await handler(request)
    except (ChemclawError, SubsystemUnavailableError) as exc:
        return _refusal_message(request, domain_error_result(exc))
    except AuthorizationError:
        # Left for `surface_authorization_denials`, which sits outside this one and words a refusal
        # differently from a fault. Catching it here would make every denial read as a crash.
        raise
    except Exception as exc:
        # Transport failures first: a raised connector error is the wire failing (tool-level errors
        # are returned), so the model may retry rather than being told not to.
        if transport_failure(exc):
            logger.warning(
                "tool %s failed in transport (%s: %s)",
                request.tool_call["name"],
                type(exc).__name__,
                exc,
            )
            return _refusal_message(request, transport_error_result(request.tool_call["name"], exc))
        logger.exception("tool %s raised an unhandled error", request.tool_call["name"])
        return _refusal_message(request, unexpected_error_result())
    failed = returned_failure(result)
    return result if failed is None else answered_failure(failed)


@wrap_tool_call
async def announce_tool_failures(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Tell the chemist's stream a tool failed, then let it continue (`announce_tool_failures`).

    Outermost of the governance middleware and inside both converters, so it sees both a tool body
    raising and a gate's refusal before either is converted to a result. A connector failure is
    returned rather than raised, so the returned message is checked too (`returned_failure` is
    `None` for raised failures, so nothing is reported twice). The raising path classifies refusals
    via `refusal_reason`, from the exception class rather than its text.
    """
    try:
        result = await handler(request)
    except Exception as exc:
        record_tool_failure(
            request.tool_call["name"],
            failure_detail(exc),
            str(request.tool_call.get("id") or ""),
            refusal_reason(exc),
        )
        raise
    failed = returned_failure(result)
    if failed is not None:
        record_tool_failure(
            request.tool_call["name"],
            returned_failure_detail(failed),
            str(request.tool_call.get("id") or ""),
        )
    return result
