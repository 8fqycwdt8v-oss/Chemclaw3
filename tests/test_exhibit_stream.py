"""An artefact write is announced after its call's result, and a chemist's edit is told once.

The stream half drives a scripted turn through the real compiled graph and `graph_events`, because
the ordering is a property of how LangGraph delivers a tool body's custom signal (during the tools
node) against the node's own update (after it) — a unit test of the mapping function could not see
it. The note half drives `exhibit_turn_note` across three turns against the store the turn reads.
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.exhibit_notes import exhibit_turn_note
from chemclaw.agent.framing import ENVELOPE_TAG
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.api.events import ExhibitEvent
from chemclaw.api.graph_stream import graph_events
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.exhibits.models import ExhibitRef, parse_spec
from chemclaw.exhibits.store import default_exhibit_store
from tests.fakes_langgraph import ScriptedChatModel

_TABLE: dict[str, Any] = {
    "kind": "table",
    "columns": [
        {"key": "solvent", "label": "Solvent"},
        {"key": "y", "label": "Yield", "unit": "%"},
    ],
    "rows": [{"solvent": "THF", "y": 76}],
}


class _Usage:
    """The token ledger's shape; the stream feeds it and nothing here reads it."""

    def add(self, usage: Any) -> None:
        """Accept one update's usage."""


def _drive(script: list[Any], session: str) -> list[Any]:
    """One scripted turn in `session`, through the real graph and the real stream translator."""

    async def _run() -> list[Any]:
        token = set_current_session_id(session)
        identity = set_current_identity("oid-ana", frozenset())
        try:
            graph = build_langgraph_agent(ScriptedChatModel(script), audit_sink=NullAuditSink())
            return [
                event
                async for event in graph_events(
                    graph,
                    "make me a table",
                    config={"configurable": {"thread_id": session}},
                    trace=ToolCallTrace(),
                    on_signal=lambda signal: None,
                    usage=_Usage(),
                )
            ]
        finally:
            reset_current_identity(identity)
            reset_current_session_id(token)

    return asyncio.run(_run())


@pytest.fixture(autouse=True)
def _memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every artefact here lives in the process's in-memory store."""
    monkeypatch.setattr(settings, "session_store", "memory")


def test_the_exhibit_event_follows_the_result_of_the_call_that_wrote_it() -> None:
    """`tool_call` → `tool_result` → `exhibit`, as the contract orders them, then the answer.

    The signal is raised inside the tool body and so reaches the stream *before* the tools node's
    update; without the hold it would sit between the call and its result, which is where a
    `question` sits — right for a question, wrong for an announcement the pane opens on.
    """
    session = uuid4().hex
    events = _drive(
        [{"name": "create_exhibit", "args": {"title": "Screen", "spec": _TABLE}}, "done"], session
    )
    assert [event.type for event in events] == ["tool_call", "tool_result", "exhibit", "token"]
    exhibit = events[2]
    assert isinstance(exhibit, ExhibitEvent)
    assert (exhibit.op, exhibit.revision, exhibit.kind, exhibit.title) == (
        "created",
        1,
        "table",
        "Screen",
    )
    assert (exhibit.author_kind, exhibit.author) == ("agent", "oid-ana")
    stored = asyncio.run(default_exhibit_store().view(session, exhibit.exhibit_id))
    assert stored is not None


def test_a_refused_write_announces_nothing() -> None:
    """A spec the store refused produced no artefact, so no event says one exists."""
    events = _drive(
        [{"name": "create_exhibit", "args": {"title": "x", "spec": {"kind": "poster"}}}, "sorry"],
        uuid4().hex,
    )
    assert "exhibit" not in [event.type for event in events]


async def _agent_table(session: str) -> str:
    """An artefact the agent wrote at revision 1."""
    view = await default_exhibit_store().create(
        session, title="Screen", spec=parse_spec(_TABLE), author_kind="agent", author="oid-ana"
    )
    return view.exhibit_id


async def _chemist_edit(session: str, exhibit_id: str, parent: int, value: int) -> None:
    """A chemist's one-cell correction."""
    await default_exhibit_store().append(
        session,
        exhibit_id,
        spec=parse_spec({**_TABLE, "rows": [{"solvent": "THF", "y": value}]}),
        parent_revision=parent,
        author_kind="human",
        author="oid-ben",
    )


async def test_a_chemists_edit_is_announced_once_with_its_diff_and_then_not_again() -> None:
    """One notice per new human revision: framed, with the changed cell; silent on the next turn."""
    session = uuid4().hex
    xid = await _agent_table(session)
    quiet = await exhibit_turn_note(session)
    assert xid in quiet and "Since you last" not in quiet, "an unedited artefact is only listed"

    await _chemist_edit(session, xid, 1, 71)
    note = await exhibit_turn_note(session)
    assert note.count("revision 1 -> 2") == 1
    assert 'changed rows[0].y: "76" -> "71"' in note
    assert f"<{ENVELOPE_TAG}" in note, "a chemist's edit reaches the model framed as data"
    assert "Since you last" not in await exhibit_turn_note(session), "announced twice"

    await _chemist_edit(session, xid, 2, 70)
    assert "revision 2 -> 3" in await exhibit_turn_note(session)


async def test_no_notice_once_the_agent_has_revised_past_the_edit() -> None:
    """The agent's own revision on top of the chemist's moves the mark: nothing left to announce."""
    session = uuid4().hex
    xid = await _agent_table(session)
    await _chemist_edit(session, xid, 1, 71)
    await default_exhibit_store().append(
        session,
        xid,
        spec=parse_spec({**_TABLE, "rows": [{"solvent": "THF", "y": 71}]}),
        parent_revision=2,
        author_kind="agent",
        author="oid-ana",
    )
    note = await exhibit_turn_note(session)
    assert "Since you last" not in note and "revision 3, last by agent" in note


async def test_an_artefact_the_chemist_created_is_announced_as_theirs() -> None:
    """A pinned result or a table the chemist started has no agent revision to diff against."""
    session = uuid4().hex
    view = await default_exhibit_store().create(
        session,
        title="Pinned screen",
        spec=parse_spec({"kind": "result", "result_ref": "c" * 64, "tool": "screen_hazards"}),
        author_kind="human",
        author="oid-ben",
    )
    note = await exhibit_turn_note(session)
    assert f"{view.exhibit_id}" in note and "created by the chemist" in note


async def test_a_referenced_artefact_is_copied_in_and_nothing_is_said_without_artefacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`exhibit_refs` brings the referenced revision's spec; an empty session gets no note."""
    session = uuid4().hex
    assert await exhibit_turn_note(session) == ""
    xid = await _agent_table(session)
    await _chemist_edit(session, xid, 1, 71)
    note = await exhibit_turn_note(session, [ExhibitRef(exhibit_id=xid, revision=1)])
    assert "refers to these artefacts" in note and '"y": 76' in note
    monkeypatch.setattr(settings, "agent_exhibits_enabled", False)
    assert await exhibit_turn_note(session) == ""


async def test_the_note_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is appended to a `HumanMessage`, so its size is the configured bound, not the store's."""
    session = uuid4().hex
    big = {**_TABLE, "rows": [{"solvent": f"S{i}", "y": i} for i in range(400)]}
    view = await default_exhibit_store().create(
        session, title="Big", spec=parse_spec(big), author_kind="agent", author="oid-ana"
    )
    monkeypatch.setattr(settings, "exhibit_note_max_chars", 2_000)
    note = await exhibit_turn_note(session, [ExhibitRef(exhibit_id=view.exhibit_id)])
    assert len(note) <= 2_000 + 500, "the framing and the listing are the only overhead"
    assert "read_exhibit" in note
