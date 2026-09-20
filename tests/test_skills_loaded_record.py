"""The producer the guard reads, driven from a skill body to a persisted row.

**A guard reading a column nobody writes is a claim that a control exists**, which this repository
has deleted twice — `map_to_hpc_identity`, and `audit_events.agent`, empty on every row that trail
ever wrote while three docstrings said in the present tense that it named the agent. So the chain
is driven rather than assumed: a backend delivers a skill body, the signal rides the turn's own
stream, the ledger folds it in, and the row carries the names.

`tests/test_distiller.py` drives the predicate. This drives the thing it reads.
"""

import asyncio
from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from chemclaw.agent.local_skills import LOCAL_SKILLS_ROOT, save_local_skill
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.scratchpad import scratchpad_backend
from chemclaw.core import turn_signals
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.turn_signals import SkillLoadedSignal

_BODY = "---\nname: cold-quench\ndescription: how to quench this class cold\n---\n\nQuench cold.\n"


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every signal the turn would have published, captured off the one publish point.

    Patched at `_emit` rather than at LangGraph's writer, because what this test is about is
    whether the *backends* announce a load at all — and a graph-shaped fixture would put the thing
    under test behind two layers that can each swallow it silently.
    """
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
            permits=lambda _name: True,
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
    """What the row carries: names, deduplicated, sorted, both tiers together.

    A duplicate would make "how many turns loaded this" wrong in the direction that admits
    self-confirming evidence, and a per-tier split would make the guard ask two questions where the
    rule asks one.
    """
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
    """The second and third graph runs of a turn drop job launches and keep everything else.

    `_resume_on_job_results` and `_revise_answer` both passed `on_signal=lambda _signal: None`, a
    blanket over the whole union written for one member of it: a resume that fed its own job ids
    back into `started_jobs` would chain durable jobs inside a single request. Nothing about a
    second run makes a *skill* read untrue, and `answer_review_max_rounds` ships at 2 — so a skill
    read only during a revision round left this row empty, and
    `agent/distiller.py::independent_sessions` then counted that session as independent evidence for
    proposing the skill that was acting in it. The guard failed open, which is the one direction it
    exists to close.
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
    """An absence test, because the defect above is spelled as a plausible-looking lambda.

    A blanket `lambda _signal: None` reads as "this run announces nothing", which is never what any
    caller here means — every one of them means "this run must not add to what the turn waits for".
    Whoever writes the next second-run helper gets the named callback or this fails.
    """
    from pathlib import Path

    source = Path("src/chemclaw/api/runner.py").read_text()

    assert "on_signal=lambda" not in source, (
        "a second graph run suppressed every signal to suppress one; use "
        "`_TurnLedger.note_signal_without_job_chaining`"
    )


async def test_the_row_really_carries_what_the_ledger_folded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last hop, which this file's docstring claimed and no assertion reached.

    Everything above stops at the in-process `_TurnLedger`, so two independent one-line mutations —
    dropping `"skills_loaded"` from `turn_cost_store._COLUMNS`, and booking `skills_loaded=[]`
    instead of the ledger's set — left 467 tests passing. The column is the entire subject of
    `D-2026-09-18-a-guard-with-nothing-to-read-is-not-a-guard`, and a guard reading a column nobody
    writes is the `map_to_hpc_identity` shape that ADR invokes, one layer past where the tests
    stopped.

    So this drives the shipped producer — `_book_turn_spend` over a real ledger into the real
    Postgres sink — and reads it back through the distiller's own query. Both ends, because a test
    that wrote the row itself would prove the schema and not the wiring.
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
