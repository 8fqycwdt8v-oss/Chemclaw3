"""A session bricked by a stranded `tool_result` heals itself, loudly.

D-145 built `unmatched_result_ids` as an *assertion* and deliberately did not wire it into the read
repair: healing silently would mask a regression in `droppable_rows` — the one primitive every
deleter of conversation rows goes through — rather than surface it. The cost it left standing is
that any session split by the old age-based retention was unusable forever, with no recovery short
of a database edit.

The heal ships now *with* the alarm D-145 wanted instead of without it: a counter and a WARNING
naming the session. These tests cover both, because a heal without the count is the thing that was
argued against, not a smaller version of the thing that was asked for.

The pure-logic cases run everywhere; the round trip needs Postgres and skips offline.
"""

import asyncio
from typing import Any

import pytest
from agent_framework import Content, Message

from chemclaw.agent.message_pairing import strip_orphans, unmatched_result_ids
from chemclaw.agent.session_store import PostgresHistoryProvider, _report_stranded_results
from tests.pg import migrated_db_or_skip


def _call(call_id: str) -> Any:
    """One `function_call` content item."""
    return Content.from_function_call(call_id=call_id, name="screen_hazards", arguments={})


def _result(call_id: str) -> Any:
    """One `function_result` content item."""
    return Content.from_function_result(call_id=call_id, result="no rule matched")


def test_a_stranded_result_is_stripped_and_its_partner_is_not() -> None:
    """The primitive: one function over both halves, and it must not over-reach.

    A message carrying an intact pair and a stranded result keeps the pair. Stripping by type
    alone — or passing one id set to both branches — would take the healthy result with it, which
    turns a repairable session into a rewritten one.
    """
    message = Message(role="tool", contents=[_result("kept"), _result("orphan")])
    repaired = strip_orphans(message, frozenset(), {"orphan"})
    assert repaired is not None
    assert [c.call_id for c in repaired.contents] == ["kept"]


def test_a_message_that_is_only_a_stranded_result_is_dropped_whole() -> None:
    """An empty message is itself a malformed block, so nothing is left to keep."""
    message = Message(role="tool", contents=[_result("orphan")])
    assert strip_orphans(message, frozenset(), {"orphan"}) is None


def test_stripping_nothing_returns_the_same_object() -> None:
    """The identity contract the storage layer writes back on — asserted for the new argument too.

    `get_messages` uses `repaired is not message` to decide which rows to `UPDATE`. A version that
    rebuilt every message would rewrite the whole session on every read of a session with a single
    orphan in it.
    """
    message = Message(role="tool", contents=[_result("c1")])
    assert strip_orphans(message, {"c1"}, frozenset()) is message


def test_the_heal_is_counted_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The condition D-145 attached to healing at all: it must not be silent.

    Every deleter of conversation rows goes through `droppable_rows`, so this counter should sit at
    zero forever. A non-zero rate means one of them stopped going through the guard — which is the
    regression a silent heal would have hidden, permanently, because nothing else looks.

    The counter is read through the real `Metrics` object rather than a stand-in, so this also
    pins that the metric name is one the registry accepts.
    """
    from chemclaw.core.metrics import Metrics

    metrics = Metrics()
    monkeypatch.setattr(
        "chemclaw.agent.session_store.record_metric", lambda update: update(metrics)
    )
    _report_stranded_results("sess-x", {"c1", "c2"})

    assert metrics.value("chemclaw_history_stranded_results_total") == 2.0


def test_a_stranded_result_is_repaired_on_read() -> None:
    """The finding, end to end: a session the old retention split becomes usable again.

    The mirror of `test_an_orphaned_tool_call_is_repaired_on_read`, which has always passed while
    this exact case bricked a session permanently. The API rejects a `tool_result` with no
    `tool_use` exactly as hard as the converse, so before this the only recovery was editing the
    database.

    Asserted through a *fresh* provider as well, so this proves the repair was written back rather
    than filtered for one read — a filter would leave the poison pill in the table for anything
    reading the rows another way.
    """

    async def _run() -> tuple[set[str], set[str]]:
        await migrated_db_or_skip()
        provider = PostgresHistoryProvider()
        session_id = "sess-stranded-result"
        await provider.rollback_to(session_id, 0)
        await provider.save_messages(
            session_id,
            [
                Message(role="user", contents=["screen sodium azide"]),
                # The shape the old age-based retention left: the assistant row carrying the call
                # aged out, the tool row answering it did not.
                Message(role="tool", contents=[_result("c-gone")]),
                Message(role="assistant", contents=[Content.from_text("It is shock-sensitive.")]),
            ],
        )

        loaded = await provider.get_messages(session_id)
        assert any("shock-sensitive" in m.text for m in loaded), "the repair ate healthy history"

        fresh = await PostgresHistoryProvider().get_messages(session_id)
        return unmatched_result_ids(loaded), unmatched_result_ids(fresh)

    from_read, from_storage = asyncio.run(_run())
    assert from_read == set(), "the caller still sees the stranded result"
    assert from_storage == set(), "the repair was filtered for one read rather than written back"


def test_a_healthy_pair_survives_the_wider_repair() -> None:
    """The bound: healing both halves must not touch a session that is intact.

    The read repair now computes two orphan sets instead of one, so the way it fails is by finding
    an orphan that is not there — and it would rewrite every row of a healthy conversation.
    """

    async def _run() -> list[Message]:
        await migrated_db_or_skip()
        provider = PostgresHistoryProvider()
        session_id = "sess-stranded-healthy"
        await provider.rollback_to(session_id, 0)
        await provider.save_messages(
            session_id,
            [
                Message(role="user", contents=["screen sodium azide"]),
                Message(role="assistant", contents=[Content.from_text("Checking."), _call("c1")]),
                Message(role="tool", contents=[_result("c1")]),
            ],
        )
        return await provider.get_messages(session_id)

    loaded = asyncio.run(_run())
    assert len(loaded) == 3, "the repair dropped a row of an intact conversation"
    assert any(c.type == "function_call" for m in loaded for c in m.contents)
    assert any(c.type == "function_result" for m in loaded for c in m.contents)
