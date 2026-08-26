"""Every channel `ChemclawState` declares survives a round trip through a **compiled graph**.

**Why this file exists.** Three defects in one week shared a single cause: a middleware was tested
by
calling its hook directly, the hook returned the right dict, and the channel it wrote did not exist
on the graph. LangGraph does not raise on a write to an undeclared channel — it drops it silently —
so the unit test passed, the suite was green, and the feature was inert.

- `agent/loop_cap.py` records that it fired in `loop_capped`; the first version of its own test
  called the hook and asserted the returned dict, and the docstring it left behind says what that
  proved: nothing. "Only a compiled graph proves the decision is connected to anything."
- `challenge_attempts` was deleted from `ChemclawState` by a merge while the challenge panel's gate
  went on reading and writing it. `attempts` stayed 0 for ever, so its bound never fired and the
  revision loop ran to the recursion limit — discarding the whole turn. That gate's own test did not
  notice, because it handed the hook a state dict it built itself, including the key. The panel and
  the field are both gone now (`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`, and
  the field followed it here) — the incident is kept because it is the clearest instance of what
  this file exists to prevent, and because the field outliving its feature by one merge is the same
  defect wearing the other face.
- The same shape is available to every future field, which is why this is a file rather than a test.

So the assertion here is deliberately *not* about behaviour. It is: **for each channel this
repository declares, a node that writes it must be able to read it back.** That is the one property
a
direct hook call can never establish, and it is cheap enough to hold for every field automatically —
`_declared_channels()` derives the list, so a field added tomorrow is covered without anyone
remembering to add a case.

**A second property joined it, from the other failure mode a channel has.** A channel can also be
declared, present, written — and *refuse* the write, which LangGraph does loudly rather than
silently: bare `UntrackedValue` raises `InvalidUpdateError` when one superstep delivers two values
for its key. That is not hypothetical for anything `ChemclawState` declares, because
`SubAgentMiddleware` returns each helper's whole state as an update and two `task` calls in one
assistant message are one superstep — measured, it killed the turn after both helpers had spent
their tokens. So every declared channel is also driven with two concurrent writers, for the same
reason the first case is derived rather than listed: the next field added here will be written from
inside a helper too, and nobody will remember.
"""

from typing import Any, cast, get_type_hints

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import after_model, before_model
from langchain.agents.middleware.todo import PlanningState
from langgraph.graph import END, START, StateGraph

from chemclaw.agent.state import ChemclawState
from tests.fakes_langgraph import ScriptedChatModel

# The channels this repository adds on top of upstream's. Derived rather than listed, so a new field
# is covered the day it is declared — the failure above was a field nobody remembered.
_UPSTREAM = set(get_type_hints(PlanningState, include_extras=True))
_PROBE_VALUE: dict[str, Any] = {"bool": True, "int": 7}


def _declared_channels() -> list[tuple[str, Any]]:
    """`(name, probe value)` for every channel `ChemclawState` declares beyond upstream's."""
    channels = []
    for name, annotation in get_type_hints(ChemclawState, include_extras=True).items():
        if name in _UPSTREAM:
            continue
        text = repr(annotation)
        kind = "bool" if "bool" in text else "int"
        channels.append((name, _PROBE_VALUE[kind]))
    return channels


def test_the_derivation_finds_the_channels_this_repository_declares() -> None:
    """The guard on the guard: an empty list would make every case below vacuous.

    If `ChemclawState` were emptied — or if `get_type_hints` stopped reporting its own fields, which
    is exactly what `agent/checkpointer.py::_first_party_channels` had to work around — the
    parametrised test would silently run zero cases and report green.
    """
    names = {name for name, _ in _declared_channels()}
    assert names, "no first-party channels found; every case below would be vacuous"
    # Named explicitly, because these two are the ones that have actually been lost.
    assert {"loop_capped"} <= names, (
        f"a channel this repository relies on is no longer declared: {sorted(names)}"
    )


@pytest.mark.parametrize(("channel", "value"), _declared_channels())
def test_a_declared_channel_survives_a_write_from_a_node(channel: str, value: Any) -> None:
    """A hook writing this channel can read it back off the finished run.

    Driven through `create_agent` with `state_schema=ChemclawState` — the same construction
    `build_langgraph_agent` uses — because the whole point is that the *graph* has the channel. A
    write to an undeclared channel is dropped without error, so the failure this catches looks
    exactly like a feature quietly doing nothing.
    """

    @before_model
    def _write(state: Any, runtime: Any) -> dict[str, Any]:
        return {channel: value}

    graph = create_agent(
        model=ScriptedChatModel(["done"]),
        tools=[],
        state_schema=ChemclawState,
        middleware=[_write],
    )
    final = graph.invoke(
        cast(Any, {"messages": [("user", "go")]}), cast(Any, {"recursion_limit": 20})
    )

    assert channel in final, (
        f"`{channel}` is declared on ChemclawState but the graph dropped a node's write to it — "
        "the channel is missing from the compiled state schema"
    )
    assert final[channel] == value


@pytest.mark.parametrize(("channel", "value"), _declared_channels())
def test_a_declared_channel_takes_two_writers_in_one_superstep(channel: str, value: Any) -> None:
    """Two nodes writing this channel together must produce a value, not `InvalidUpdateError`.

    The fan-out is hand-built rather than driven through `task`, because the property belongs to the
    *channel* and this parametrisation has to hold for every field the class declares — including
    ones no subagent writes yet. `tests/test_subagents.py` drives the real `task` fan-out that made
    this concrete; this is the general form, so a field added tomorrow cannot reintroduce it.

    What is asserted is only that the graph completed and the channel holds something. *What* two
    concurrent writers should mean is a per-field question with a per-field answer
    (`agent/state.TurnTotal` sums the advances, `TurnFlag` ors), and asserting one of those here
    would make this file the place a future field's semantics get decided by whichever probe value
    the derivation picked.
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
    """The mechanism itself, pinned — because every bug above depends on it being silent.

    If LangGraph ever starts raising on an undeclared write, this test fails and the whole file
    becomes unnecessary: the engine would be reporting the defect itself, which is better than a
    test doing it. That is worth being told about rather than discovering years later.
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
    """The parametrised cases build their own graph; this pins that the real one agrees.

    Without it the file could pass for ever against a schema `build_langgraph_agent` does not
    actually use — the same class of mistake as asserting against a hand-built shape the engine
    never emits.
    """
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
    """A redeclared upstream channel must not quietly lose `PrivateStateAttr`.

    `ReloadingSkillsMiddleware` redeclares `skills_metadata` to change its *channel* to
    `UntrackedValue`, and the first version of that dropped upstream's `PrivateStateAttr` with it.
    The marker is not cosmetic: without it the field enters the graph's **input** schema, so a
    caller can supply a skills listing and upstream's `if "skills_metadata" in state` short-circuit
    accepts it — replacing the role-narrowed listing from the invocation payload. It also enters the
    output schema, and `SubAgentMiddleware` stops stripping it, so a specialist inherits the
    supervisor's listing instead of re-narrowing to its own profile — which is invariant 4 of
    `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`.
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
