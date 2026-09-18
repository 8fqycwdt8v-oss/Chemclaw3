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

        backend = scratchpad_backend(skills_backend(AgentProfile(name="default"), []), store)
        return backend.read(path)
    finally:
        reset_current_identity(tokens)


def test_reading_a_personal_skill_announces_it_by_name(emitted: list[Any]) -> None:
    """The tier the guard most needs, since it is the one the agent can propose into."""
    store = InMemoryStore()
    asyncio.run(save_local_skill(store, "a-chemist", "cold-quench", _BODY))

    assert _read(store, "a-chemist", f"{LOCAL_SKILLS_ROOT}cold-quench/SKILL.md").error is None

    loads = [signal for signal in emitted if isinstance(signal, SkillLoadedSignal)]
    assert [(signal.skill, signal.tier) for signal in loads] == [("cold-quench", "mine")]


def test_reading_a_shared_skill_announces_it_too(emitted: list[Any]) -> None:
    """Both tiers, because a reviewed skill shapes a turn exactly as a personal one does."""
    store = InMemoryStore()

    assert _read(store, "a-chemist", "/skills/protocol-generation/SKILL.md").error is None

    loads = [signal for signal in emitted if isinstance(signal, SkillLoadedSignal)]
    assert [(signal.skill, signal.tier) for signal in loads] == [("protocol-generation", "shared")]


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
        SkillLoadedSignal(skill="cold-quench", tier="mine"),
        SkillLoadedSignal(skill="protocol-generation", tier="shared"),
        SkillLoadedSignal(skill="cold-quench", tier="mine"),
    ):
        ledger.note_signal(signal)

    assert sorted(ledger.skills_loaded) == ["cold-quench", "protocol-generation"]
