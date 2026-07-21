"""The agent wires its tools and skills correctly (plan step 1.5).

Built with an injected dummy client so no LLM credentials are needed — this
proves the MAF wiring (tools advertised, skills discovered, context kept in
budget), not model behavior.
"""

import asyncio

from agent_framework import CharacterEstimatorTokenizer, Message, SlidingWindowStrategy
from agent_framework._compaction import (
    TokenBudgetComposedStrategy,
    ToolResultCompactionStrategy,
    apply_compaction,
    included_token_count,
)

from agents.audit import audit_tool_calls
from agents.chemclaw_agent import _build_compaction, build_agent


def test_agent_advertises_qm_tools() -> None:
    """Both QM tools are registered on the agent under their function names."""
    agent = build_agent(chat_client=object())
    tool_names = {tool.name for tool in agent.default_options["tools"]}
    assert {
        "compute_xtb_energy",
        "predict_solubility",
        "predict_pka",
        "submit_qm_job",
        "get_qm_job_status",
        "find_notes",
        "expand_note",
        "propose_knowledge_note",
    } <= tool_names


def test_agent_has_skills_history_and_compaction() -> None:
    """Skills (judgment), a session history, and context compaction are all attached."""
    agent = build_agent(chat_client=object())
    provider_types = {type(p).__name__ for p in agent.context_providers}
    assert {"SkillsProvider", "InMemoryHistoryProvider", "CompactionProvider"} <= provider_types


def test_agent_audits_every_tool_call() -> None:
    """The GxP tool-audit middleware is attached to the agent."""
    agent = build_agent(chat_client=object())
    middleware = agent.middleware
    assert middleware is not None
    assert audit_tool_calls in middleware


def test_compaction_reduces_context_over_budget() -> None:
    """The wired strategy trims a long thread to its token budget, keeping the newest turn."""
    tokenizer = CharacterEstimatorTokenizer()
    # A tiny explicit budget so the test is deterministic and independent of the config default.
    strategy = TokenBudgetComposedStrategy(
        token_budget=200,
        tokenizer=tokenizer,
        strategies=[
            ToolResultCompactionStrategy(keep_last_tool_call_groups=1),
            SlidingWindowStrategy(keep_last_groups=2),
        ],
    )
    marker = "the newest question"
    # Alternating roles so each turn is its own group, as in a real thread.
    messages = [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            contents=[f"turn {i} " + "filler " * 40],
        )
        for i in range(20)
    ]
    messages.append(Message(role="user", contents=[marker]))

    kept = asyncio.run(apply_compaction(messages, strategy=strategy, tokenizer=tokenizer))

    assert included_token_count(kept) <= 200  # brought within budget
    assert len(kept) < len(messages)  # actually dropped older turns
    assert any(marker in m.text for m in kept)  # newest turn preserved


def test_compaction_is_a_noop_under_budget() -> None:
    """Under budget, nothing is trimmed — compaction only fires when applicable."""
    tokenizer = CharacterEstimatorTokenizer()
    strategy = _build_compaction("in_memory").before_strategy
    assert strategy is not None
    messages = [Message(role="user", contents=["short question"])]

    kept = asyncio.run(apply_compaction(messages, strategy=strategy, tokenizer=tokenizer))

    assert len(kept) == 1
