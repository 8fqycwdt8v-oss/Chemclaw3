"""A tool call the framework refuses still reaches the audit trail.

AUDIT-2, from the 50-user load run: `agent_framework._tools._auto_invoke_function` returns early on
two paths — a tool name that is in no map, and arguments that fail schema validation — and both
return **before** the function-middleware pipeline. So "the model asked for `find_notes` with
arguments it could not satisfy" left no row at all.

Authorization not running there is harmless: nothing executed. The *audit* gap is not, for a trail
whose stated purpose is to answer what the agent attempted — and a malformed call is the shape a
prompt injection takes when it half-works.

These drive MAF's real dispatcher rather than a stand-in, because the finding *is* about MAF's
control flow: a test over our own wrapper would prove the wrapper works and say nothing about where
upstream returns.
"""

import asyncio
from typing import Any

import pytest
from agent_framework import tool as maf_tool

from chemclaw.agent.audit import (
    REJECTED_OUTCOME,
    AuditEvent,
    install_rejected_call_audit,
    make_audit_middleware,
)


class _RecordingSink:
    """Collects the events written to it — the audit trail as a list."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Append one event."""
        self.events.append(event)


@maf_tool
def add_numbers(first: int, second: int) -> int:
    """Add two integers — a tool with a schema strict enough to reject a bad call."""
    return first + second


def _install(sink: _RecordingSink) -> Any:
    """Install the dispatch wrapper with `sink` standing in for the deployment's audit trail.

    The wrapper resolves its sink per call through `default_audit_sink()` rather than binding one,
    because the patch is process-global and permanent while an agent is not — so a test injects the
    trail the same way a deployment configures it, by being the one sink there is.
    """
    import chemclaw.agent.audit as audit_module

    original_default = audit_module.default_audit_sink
    audit_module.default_audit_sink = lambda: sink
    restore_patch = install_rejected_call_audit()

    def _undo() -> None:
        restore_patch()
        audit_module.default_audit_sink = original_default

    return _undo


def _dispatch(name: str, arguments: str, sink: _RecordingSink) -> Any:
    """Ask MAF to invoke `name` with raw JSON `arguments`, through the real dispatcher."""
    from agent_framework import _tools as maf_tools
    from agent_framework._middleware import FunctionMiddlewarePipeline
    from agent_framework._types import Content

    audit = make_audit_middleware(correlation_id="cid-1", actor="chemist-1", sink=sink)
    pipeline = FunctionMiddlewarePipeline(audit)
    call = Content.from_function_call(call_id="call-1", name=name, arguments=arguments)

    restore = _install(sink)
    try:
        return asyncio.run(
            maf_tools._auto_invoke_function(  # noqa: SLF001 - the dispatcher under test
                call,
                config={},
                tool_map={"add_numbers": add_numbers},
                middleware_pipeline=pipeline,
            )
        )
    finally:
        restore()


def test_a_call_with_unsatisfiable_arguments_is_audited() -> None:
    """The finding, in the shape the load run found it.

    The model names a real tool and sends arguments its schema refuses. MAF composes an error
    result and returns before any middleware, so nothing recorded the attempt.
    """
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity

    sink = _RecordingSink()
    # The turn's real identity, exactly as the front door stamps it — which is where the row's
    # actor comes from now. Binding one at install time was the first version's defect: the patch
    # is process-global and permanent, so the first agent built would have owned every later
    # agent's rejections.
    token = set_current_identity("chemist-1", frozenset({"process-chemist"}))
    try:
        _dispatch("add_numbers", '{"first": "not-a-number", "second": 2}', sink)
    finally:
        reset_current_identity(token)

    assert len(sink.events) == 1, "a refused call left no row in the trail"
    event = sink.events[0]
    assert event.tool == "add_numbers"
    assert event.outcome == REJECTED_OUTCOME
    assert event.actor == "chemist-1", "the row does not name the chemist whose turn it was"


def test_the_recorded_arguments_are_what_the_model_actually_sent() -> None:
    """What was rejected is the only interesting thing about a rejected call.

    The validated form does not exist — that is what "rejected" means — so recording the raw
    arguments is the difference between a trail that says an attempt happened and one that says
    what it was.
    """
    sink = _RecordingSink()
    _dispatch("add_numbers", '{"first": "not-a-number", "second": 2}', sink)

    assert "not-a-number" in sink.events[0].arguments


def test_a_call_naming_no_tool_at_all_is_audited() -> None:
    """The second early return, which the backlog row does not name.

    A tool name in no map returns even earlier than the argument check. It is the more interesting
    of the two for a trail: "the agent tried to call something that does not exist" is a fact about
    the model's behaviour, and it was invisible.
    """
    sink = _RecordingSink()
    _dispatch("delete_everything", "{}", sink)

    assert len(sink.events) == 1
    assert sink.events[0].tool == "delete_everything"
    assert sink.events[0].outcome == REJECTED_OUTCOME


def test_a_successful_call_is_recorded_once_by_the_middleware() -> None:
    """The other direction: no double-recording, and the outcome is the middleware's, not ours.

    Without this, a wrapper that recorded unconditionally would pass every test above while
    doubling every row in the trail — and both rows would look correct, which is the worst shape
    for a record nobody re-reads.
    """
    sink = _RecordingSink()
    result = _dispatch("add_numbers", '{"first": 2, "second": 3}', sink)

    assert len(sink.events) == 1, "the call was recorded twice"
    assert sink.events[0].outcome != REJECTED_OUTCOME
    assert "5" in str(result.result)


def test_a_tool_that_raises_is_recorded_once_and_not_as_rejected() -> None:
    """A tool body that fails *did* run, so the middleware owns that row.

    This is the case a string-matching wrapper would get wrong: MAF composes an error result with
    an exception for a raising tool exactly as it does for a refused one. The marker asks a
    different question — did anything audit this — so the two stay apart.
    """

    @maf_tool
    def always_fails(value: int) -> int:
        """Raise, so the middleware sees a failure it must own."""
        raise ValueError("no")

    from agent_framework import _tools as maf_tools
    from agent_framework._middleware import FunctionMiddlewarePipeline
    from agent_framework._types import Content

    sink = _RecordingSink()
    audit = make_audit_middleware(correlation_id="cid-1", actor="chemist-1", sink=sink)
    restore = _install(sink)
    try:
        asyncio.run(
            maf_tools._auto_invoke_function(  # noqa: SLF001 - the dispatcher under test
                Content.from_function_call(
                    call_id="c", name="always_fails", arguments='{"value":1}'
                ),
                config={},
                tool_map={"always_fails": always_fails},
                middleware_pipeline=FunctionMiddlewarePipeline(audit),
            )
        )
    finally:
        restore()

    assert len(sink.events) == 1
    assert sink.events[0].outcome != REJECTED_OUTCOME, "a tool that ran was recorded as rejected"


def test_installing_twice_does_not_double_record() -> None:
    """`build_agent` runs once per profile per process, and a second patch would wrap the first.

    The doubling would be invisible in the trail, because both rows would be correct.
    """
    from agent_framework import _tools as maf_tools

    sink = _RecordingSink()
    first = _install(sink)
    patched = maf_tools._auto_invoke_function  # noqa: SLF001 - identity is the assertion
    second = install_rejected_call_audit()
    try:
        assert maf_tools._auto_invoke_function is patched  # noqa: SLF001
    finally:
        second()
        first()


def test_the_patch_is_reversible() -> None:
    """Install then restore leaves the process exactly as it was found.

    Asserted as a *round trip* rather than as "the dispatcher changed", because whether it changes
    depends on something outside this test: `build_agent` installs the patch permanently, so any
    other test in the session that builds an agent gets there first and the idempotency guard
    correctly makes this a no-op. The first version asserted the change and passed alone while
    failing beside `test_agent.py` — a test whose result depended on which files ran before it.

    The round trip is the property that actually matters, and it holds in both states.
    """
    from agent_framework import _tools as maf_tools

    before = maf_tools._auto_invoke_function  # noqa: SLF001 - identity is the assertion
    restore = install_rejected_call_audit()
    restore()
    assert maf_tools._auto_invoke_function is before  # noqa: SLF001


def test_parallel_calls_do_not_see_each_others_marker() -> None:
    """The concurrency property the whole design rests on, asserted rather than reasoned about.

    `asyncio.gather` copies the context per task, so a marker set by the middleware in one call is
    invisible to a sibling. If that were false, one successful call would suppress the audit of a
    refused one running beside it.
    """
    sink = _RecordingSink()

    async def _both() -> None:
        from agent_framework import _tools as maf_tools
        from agent_framework._middleware import FunctionMiddlewarePipeline
        from agent_framework._types import Content

        audit = make_audit_middleware(correlation_id="cid-1", actor="a", sink=sink)
        pipeline = FunctionMiddlewarePipeline(audit)

        async def call(arguments: str, call_id: str) -> Any:
            return await maf_tools._auto_invoke_function(  # noqa: SLF001
                Content.from_function_call(
                    call_id=call_id, name="add_numbers", arguments=arguments
                ),
                config={},
                tool_map={"add_numbers": add_numbers},
                middleware_pipeline=pipeline,
            )

        await asyncio.gather(call('{"first":1,"second":2}', "ok"), call('{"first":"x"}', "bad"))

    restore = _install(sink)
    try:
        asyncio.run(_both())
    finally:
        restore()

    outcomes = sorted(event.outcome for event in sink.events)
    assert len(sink.events) == 2, f"expected one row per call, got {outcomes}"
    assert REJECTED_OUTCOME in outcomes, "the refused call was suppressed by its successful sibling"


@pytest.mark.parametrize("arguments", ["{not json", '{"first": 1}'])
def test_an_unparseable_argument_blob_still_records_something(arguments: str) -> None:
    """The trail must not fail where the call already did.

    Reading a malformed call's arguments can itself fail — that is the definition of this path — so
    the reader is defensive and records what it can. An exception raised *inside* the audit of a
    refusal would replace a recorded attempt with a crash, in the one place that must add none.
    """
    sink = _RecordingSink()
    _dispatch("add_numbers", arguments, sink)

    assert len(sink.events) == 1
    assert sink.events[0].outcome == REJECTED_OUTCOME
    assert sink.events[0].arguments
