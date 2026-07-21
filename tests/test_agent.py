"""The agent wires its tools and skills correctly (plan step 1.5).

Built with an injected dummy client so no LLM credentials are needed — this
proves the MAF wiring (tools advertised, skills discovered, context kept in
budget), not model behavior.
"""

import asyncio

import pytest
from agent_framework import CharacterEstimatorTokenizer, Message, SlidingWindowStrategy
from agent_framework._compaction import (
    TokenBudgetComposedStrategy,
    ToolResultCompactionStrategy,
    apply_compaction,
    included_token_count,
)

from agents.chemclaw_agent import _build_compaction, _default_chat_client, build_agent
from chemclaw.config import settings


def test_default_client_preflights_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building the default client without ANTHROPIC_API_KEY fails with a clear message."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _default_chat_client()


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
    """A single GxP tool-audit middleware is attached (built per-conversation)."""
    agent = build_agent(chat_client=object())
    middleware = agent.middleware
    assert middleware is not None
    assert len(list(middleware)) == 1  # exactly the one audit middleware, over all tools


def test_agent_attaches_fingerprint_search_as_mcp_servers() -> None:
    """Structural search is reached over MCP (servers on `mcp_tools`), not in-process tools.

    The in-process search wrappers are no longer registered as function tools; the agent talks
    to the molfp/rxnfp capability servers over the MCP protocol instead (construction is lazy —
    no subprocess is spawned here).
    """
    agent = build_agent(chat_client=object())
    assert {t.name for t in agent.mcp_tools} == {"mcp-molfp", "mcp-rxnfp"}
    function_tool_names = {f.name for f in agent.default_options["tools"]}
    assert {"find_similar_reactions", "find_similar_molecules"} & function_tool_names == set()


def test_instructions_only_name_available_tools() -> None:
    """Every tool the instructions tell the model to call actually exists (no name drift).

    Regression guard for the `find_similar_reactions` vs `similar_reactions` class of bug: the
    agent's advertised surface is the registered function tools plus the allowed MCP tools, and
    the instructions must not promise a tool outside that set.
    """
    agent = build_agent(chat_client=object())
    available = {f.name for f in agent.default_options["tools"]}
    for spec in settings.mcp_servers:
        available |= set(spec.allowed_tools or [])

    # The tool names the instructions direct the model to use.
    referenced = {
        "gather_evidence",
        "expand_note",
        "find_notes",
        "similar_reactions",
        "similar_molecules",
        "substructure_matches",
        "compute_xtb_energy",
        "predict_pka",
        "predict_solubility",
        "submit_qm_job",
        "get_qm_job_status",
        "suggest_next_experiment",
        "propose_knowledge_note",
        "record_confirmed_answer",
    }
    missing = {name for name in referenced if name not in available}
    assert missing == set(), f"instructions reference unavailable tools: {missing}"
    # And each referenced name must actually appear in the instruction text.
    from agents.chemclaw_agent import _INSTRUCTIONS

    assert all(name in _INSTRUCTIONS for name in referenced)


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
