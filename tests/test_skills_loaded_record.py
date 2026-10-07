"""The `skills_loaded` record, driven from a skill body to a persisted row.

A backend delivers a skill body, the signal rides the turn's stream, the ledger folds it in, and
the row carries the names. `tests/test_distiller.py` drives the predicate that reads them.
"""

import asyncio
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.local_skills import LOCAL_SKILLS_ROOT, save_local_skill
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import scratchpad_backend
from chemclaw.agent.skill_access import SkillNarrowing
from chemclaw.core import turn_signals
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.turn_signals import SkillLoadedSignal

_BODY = "---\nname: cold-quench\ndescription: how to quench this class cold\n---\n\nQuench cold.\n"


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every signal the turn would have published, captured at `_emit`, the one publish point."""
    published: list[Any] = []
    monkeypatch.setattr(turn_signals, "_emit", published.append)
    return published


def _read(store: Any, actor: str, path: str) -> Any:
    """Read one path through the backend a turn for `actor` is given."""
    tokens = set_current_identity(actor, frozenset())
    try:
        from chemclaw.agent.langgraph_agent import skills_backend

        backend = scratchpad_backend(
            skills_backend(AgentProfile(name="default"), []),
            store,
            permits=SkillNarrowing.permissive(),
        )
        return backend.read(path)
    finally:
        reset_current_identity(tokens)


def test_reading_a_personal_skill_announces_it_by_name(emitted: list[Any]) -> None:
    """The tier the guard most needs, since it is the one the agent can propose into."""
    store = InMemoryStore()
    asyncio.run(save_local_skill(store, "a-chemist", "cold-quench", _BODY))

    assert _read(store, "a-chemist", f"{LOCAL_SKILLS_ROOT}cold-quench/SKILL.md").error is None

    loads = [signal for signal in emitted if isinstance(signal, SkillLoadedSignal)]
    assert [signal.skill for signal in loads] == ["cold-quench"]


def test_reading_a_shared_skill_announces_it_too(emitted: list[Any]) -> None:
    """Both tiers, because a reviewed skill shapes a turn exactly as a personal one does."""
    store = InMemoryStore()

    assert _read(store, "a-chemist", "/skills/protocol-generation/SKILL.md").error is None

    loads = [signal for signal in emitted if isinstance(signal, SkillLoadedSignal)]
    assert [signal.skill for signal in loads] == ["protocol-generation"]


def test_a_read_that_delivered_nothing_announces_nothing(emitted: list[Any]) -> None:
    """The same two conditions the counter is taken on, so the array and the counter agree.

    A failed read delivered no body, so no skill shaped that turn — and an array that said
    otherwise would discount evidence the guard should have counted.
    """
    store = InMemoryStore()

    assert _read(store, "a-chemist", f"{LOCAL_SKILLS_ROOT}not-a-skill/SKILL.md").error is not None
    assert _read(store, "a-chemist", "/skills/README.md").error is None

    assert [signal for signal in emitted if isinstance(signal, SkillLoadedSignal)] == [], (
        "a failed read, or a document beside the tree rather than inside a skill, was counted as a "
        "skill shaping this turn"
    )


def test_the_ledger_folds_both_tiers_into_one_set() -> None:
    """The row carries skill names deduplicated, sorted, with both tiers in one set."""
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _TurnLedger

    ledger = _TurnLedger(correlation_id="c-1", usage=TurnUsage())
    for signal in (
        SkillLoadedSignal(skill="cold-quench"),
        SkillLoadedSignal(skill="protocol-generation"),
        SkillLoadedSignal(skill="cold-quench"),
    ):
        ledger.note_signal(signal)

    assert sorted(ledger.skills_loaded) == ["cold-quench", "protocol-generation"]


def test_a_revision_round_still_records_what_it_loaded() -> None:
    """A revision round still records the skills it loaded.

    Later graph runs of a turn suppress only job launches; a skill read during revision must still
    be recorded, or the distiller would count that session as independent evidence.
    """
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _TurnLedger
    from chemclaw.core.turn_signals import JobSignal

    ledger = _TurnLedger(correlation_id="c-1", usage=TurnUsage())
    ledger.note_signal_without_job_chaining(JobSignal(job_id="job-that-must-not-chain", kind="xtb"))
    ledger.note_signal_without_job_chaining(SkillLoadedSignal(skill="cold-quench"))

    assert ledger.started_jobs == [], "a resumed run chained its own job into the turn's wait"
    assert sorted(ledger.skills_loaded) == ["cold-quench"]


def test_no_graph_run_of_a_turn_suppresses_the_whole_signal_union() -> None:
    """No graph run of a turn suppresses the whole signal union with a blanket lambda."""
    from pathlib import Path

    source = Path("src/chemclaw/api/runner.py").read_text()

    assert "on_signal=lambda" not in source, (
        "a second graph run suppressed every signal to suppress one; use "
        "`_TurnLedger.note_signal_without_job_chaining`"
    )


async def test_the_row_really_carries_what_the_ledger_folded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted row really carries what the ledger folded.

    Drives `_book_turn_spend` over a real ledger into the real Postgres sink and reads it back
    through the distiller's own query.
    """
    from chemclaw.agent import turn_cost
    from chemclaw.agent.session import TurnSession
    from chemclaw.agent.skill_fingerprint import skill_fingerprint
    from chemclaw.agent.turn_cost_store import PostgresTurnCostSink
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _book_turn_spend, _TurnLedger
    from chemclaw.cli.distill import _skills_by_session
    from chemclaw.core.config import settings
    from tests.pg import migrated_db_or_skip

    await migrated_db_or_skip()
    # The shipped chooser answers `NullTurnCostSink` off `session_store`, which this suite runs on
    # `memory`; the subject here is the columns, so the sink is named rather than configured.
    monkeypatch.setattr(turn_cost, "default_turn_cost_sink", PostgresTurnCostSink)
    monkeypatch.setattr(settings, "session_store", "postgres")

    session_id = "skills-loaded-row-probe"
    ledger = _TurnLedger(correlation_id="skills-loaded-row-1", usage=TurnUsage())
    ledger.note_signal(SkillLoadedSignal(skill="cold-quench"))
    ledger.note_signal(SkillLoadedSignal(skill="protocol-generation"))

    _book_turn_spend(
        ledger,
        session=TurnSession(session_id=session_id),
        actor="skills-loaded-row-actor",
        profile="default",
        budget=None,
    )
    # `record_turn_cost` writes on its own task, deliberately (it runs under a `finally` where an
    # await would re-raise a pending cancellation), so the pending set is what to wait on.
    await asyncio.gather(*turn_cost._PENDING)

    loaded = await _skills_by_session()

    assert loaded.get(session_id) == frozenset(
        {skill_fingerprint("cold-quench"), skill_fingerprint("protocol-generation")}
    ), "the ledger's skills did not reach the row the self-confirmation guard reads"
