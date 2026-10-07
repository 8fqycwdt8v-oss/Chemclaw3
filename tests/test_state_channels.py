"""Every channel `ChemclawState` declares survives a round trip through a compiled graph.

LangGraph silently drops a write to an undeclared channel, so a hook tested directly can pass
while the feature is inert. For every declared channel, derived from the class, a node that
writes it must be able to read it back. Each channel is also driven with two writers in one
superstep, since helpers return whole states and a bare `UntrackedValue` raises
`InvalidUpdateError` on concurrent writes.
"""

from typing import Any, cast, get_type_hints

import pytest
from deepagents.middleware._state import private_state_field_names
from langchain.agents import create_agent
from langchain.agents.middleware import after_model, before_model
from langchain.agents.middleware.todo import PlanningState
from langgraph.graph import END, START, StateGraph

from chemclaw.agent.state import ChemclawState
from tests.fakes_langgraph import ScriptedChatModel

# The channels this repository adds on top of upstream's. Derived rather than listed, so a new field
# is covered the day it is declared — the failure above was a field nobody remembered.
_UPSTREAM = set(get_type_hints(PlanningState, include_extras=True))
_PROBE_VALUE: dict[str, Any] = {"bool": True, "int": 7, "str": "a-peer"}

# Channels declared `PrivateStateAttr`: kept out of a run's output and out of what crosses the
# subagent boundary, so they are read back from inside the run rather than off its result. Found by
# the function deepagents strips them by, so the two cannot disagree about which they are.
_PRIVATE = private_state_field_names(ChemclawState)


def _declared_channels() -> list[tuple[str, Any]]:
    """`(name, probe value)` for every channel `ChemclawState` declares beyond upstream's.

    The probe is derived from the annotation's type, and an unrecognised type raises, so a new
    field is covered by a value of its own type.
    """
    channels = []
    for name, annotation in get_type_hints(ChemclawState, include_extras=True).items():
        if name in _UPSTREAM:
            continue
        text = repr(annotation)
        kind = next((k for k in ("bool", "str", "int") if f"{k}," in text or f"[{k}]" in text), "")
        if kind not in _PROBE_VALUE:
            raise AssertionError(
                f"{name!r} is annotated {text!r}, whose type this derivation does not recognise. "
                "Add its probe value to `_PROBE_VALUE` and its name to the `kind` tuple — a field "
                "probed with a value of the wrong type is visited rather than covered."
            )
        channels.append((name, _PROBE_VALUE[kind]))
    return channels


def test_the_derivation_finds_the_channels_this_repository_declares() -> None:
    """The derivation finds the declared channels, or every parametrised case would be vacuous."""
    names = {name for name, _ in _declared_channels()}
    assert names, "no first-party channels found; every case below would be vacuous"
    # Named explicitly, because these two are the ones that have actually been lost.
    assert {"loop_capped"} <= names, (
        f"a channel this repository relies on is no longer declared: {sorted(names)}"
    )


@pytest.mark.parametrize(("channel", "value"), _declared_channels())
def test_a_declared_channel_survives_a_write_from_a_node(channel: str, value: Any) -> None:
    """A hook writing this channel can read it back off the finished run.

    Driven through `create_agent(state_schema=ChemclawState)`, as `build_langgraph_agent` builds it.
    """

    @before_model
    def _write(state: Any, runtime: Any) -> dict[str, Any]:
        return {channel: value}

    seen: dict[str, Any] = {}

    @after_model
    def _read(state: Any, runtime: Any) -> None:
        # A `PrivateStateAttr` channel is omitted from the run's *output* by design, so the finished
        # run cannot show it; a later node reading the live state is where it has to be found.
        if channel in state:
            seen[channel] = state[channel]

    graph = create_agent(
        model=ScriptedChatModel(["done"]),
        tools=[],
        state_schema=ChemclawState,
        middleware=[_write, _read],
    )
    final = graph.invoke(
        cast(Any, {"messages": [("user", "go")]}), cast(Any, {"recursion_limit": 20})
    )
    if channel in _PRIVATE:
        assert seen.get(channel) == value, (
            f"`{channel}` is declared on ChemclawState but a node's write to it never reached the "
            "next node — the channel is missing from the compiled state schema"
        )
        assert channel not in final, f"`{channel}` is private and leaked into the run's output"
        return

    assert channel in final, (
        f"`{channel}` is declared on ChemclawState but the graph dropped a node's write to it — "
        "the channel is missing from the compiled state schema"
    )
    assert final[channel] == value


@pytest.mark.parametrize(("channel", "value"), _declared_channels())
def test_a_declared_channel_takes_two_writers_in_one_superstep(channel: str, value: Any) -> None:
    """Two nodes writing this channel in one superstep produce a value, not `InvalidUpdateError`.

    Hand-built so it covers every field; `tests/test_subagents.py` drives the real `task` fan-out.
    Only completion is asserted; what two writers mean is each field's own reducer's decision.
    """
    builder = StateGraph(ChemclawState)
    builder.add_node("fan", lambda state: {})
    builder.add_node("left", lambda state: {channel: value})
    builder.add_node("right", lambda state: {channel: value})
    builder.add_edge(START, "fan")
    builder.add_edge("fan", "left")
    builder.add_edge("fan", "right")
    builder.add_edge("left", END)
    builder.add_edge("right", END)
    graph = builder.compile()

    final = graph.invoke(cast(Any, {"messages": []}))

    assert channel in final, (
        f"`{channel}` did not survive two writers in one superstep — a channel that refuses a "
        "concurrent update loses the whole turn the moment two helpers finish together"
    )


def test_a_write_to_an_undeclared_channel_is_dropped_without_error() -> None:
    """A write to an undeclared channel is dropped without error.

    If LangGraph starts raising here, this fails and the file becomes unnecessary.
    """

    @after_model
    def _write_unknown(state: Any, runtime: Any) -> dict[str, Any]:
        return {"chemclaw_no_such_channel": 1}

    graph = create_agent(
        model=ScriptedChatModel(["done"]),
        tools=[],
        state_schema=ChemclawState,
        middleware=[_write_unknown],
    )
    final = graph.invoke(
        cast(Any, {"messages": [("user", "go")]}), cast(Any, {"recursion_limit": 20})
    )

    assert "chemclaw_no_such_channel" not in final, (
        "LangGraph now surfaces writes to undeclared channels — check whether it raises, and if so "
        "this file's premise (silent drops) no longer holds"
    )


def test_the_state_schema_is_what_the_real_builder_compiles() -> None:
    """The real builder compiles with the same state schema the parametrised cases use."""
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    graph = build_langgraph_agent(
        ScriptedChatModel(["done"]), audit_sink=NullAuditSink(), connectors=[]
    )
    compiled = set(graph.channels)
    for name, _ in _declared_channels():
        assert name in compiled, (
            f"`{name}` is declared on ChemclawState but is not a channel on the graph "
            "build_langgraph_agent compiles"
        )


def test_an_agent_middleware_subclass_keeps_upstream_s_schema_markers() -> None:
    """A redeclared upstream channel keeps `PrivateStateAttr`.

    Without it `skills_metadata` enters the input schema, so a caller could replace the narrowed
    listing, and the output schema, so a helper would inherit its caller's listing instead of
    re-narrowing.
    """
    from deepagents.middleware.skills import SkillsState

    from chemclaw.agent.langgraph_agent import ReloadingSkillsState

    upstream = repr(get_type_hints(SkillsState, include_extras=True)["skills_metadata"])
    ours = repr(get_type_hints(ReloadingSkillsState, include_extras=True)["skills_metadata"])

    assert "UntrackedValue" in ours, "the reload mechanism is the UntrackedValue channel"
    assert "OmitFromSchema" in upstream, "upstream no longer marks it private; re-read this test"
    assert "OmitFromSchema" in ours, (
        "the redeclaration dropped upstream's PrivateStateAttr — skills_metadata is now in the "
        "graph's input and output schema, so a caller can replace the role-narrowed listing and a "
        "specialist inherits the supervisor's"
    )
