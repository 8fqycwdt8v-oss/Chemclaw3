"""The agent wires its tools and skills correctly (plan step 1.5).

Built with an injected dummy client so no LLM credentials are needed — this
proves the MAF wiring (tools advertised, skills discovered, context kept in
budget), not model behavior.
"""

import asyncio

import pytest
from agent_framework import (
    AgentModeProvider,
    CharacterEstimatorTokenizer,
    Message,
    SlidingWindowStrategy,
    TodoProvider,
)
from agent_framework._compaction import (
    TokenBudgetComposedStrategy,
    ToolResultCompactionStrategy,
    apply_compaction,
    included_token_count,
)

from agents.chemclaw_agent import _build_compaction, build_agent
from chemclaw.config import settings


def test_agent_applies_default_generation_options() -> None:
    """Config-driven temperature/max-tokens are threaded onto the agent's default options (F0.3)."""
    agent = build_agent(chat_client=object())
    assert agent.default_options["temperature"] == settings.llm_temperature
    assert agent.default_options["max_tokens"] == settings.llm_max_tokens


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


def _enable_harness(monkeypatch: pytest.MonkeyPatch, *, autonomy: str = "plan_only") -> None:
    """Turn on the harness path for a test (reverted automatically after)."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", autonomy)


def test_harness_agent_adds_todo_and_mode_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`harness_enabled` wires MAF todo + plan/execute mode atop history/skills/compaction."""
    _enable_harness(monkeypatch)
    agent = build_agent(chat_client=object())
    provider_types = {type(p).__name__ for p in agent.context_providers}
    assert {
        "TodoProvider",
        "AgentModeProvider",
        "InMemoryHistoryProvider",
        "SkillsProvider",
        "CompactionProvider",
    } <= provider_types


def test_harness_agent_keeps_full_capability_toolset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness must not drop Chemclaw's tools — it runs over the *same* capability set.

    Regression guard against a harness path that silently ships a reduced toolset: the harness
    agent advertises every classic function tool and attaches the same MCP capability servers.
    """
    classic = {t.name for t in build_agent(chat_client=object()).default_options["tools"]}
    _enable_harness(monkeypatch)
    harness = build_agent(chat_client=object())
    harness_tools = {t.name for t in harness.default_options["tools"]}
    assert classic <= harness_tools  # every classic capability tool is still present
    assert {"mcp-molfp", "mcp-rxnfp"} == {t.name for t in harness.mcp_tools}


@pytest.mark.parametrize(
    ("autonomy", "expected_mode"),
    [("plan_only", "plan"), ("execute", "execute")],
)
def test_harness_autonomy_sets_start_mode(
    monkeypatch: pytest.MonkeyPatch, autonomy: str, expected_mode: str
) -> None:
    """`plan_only` starts in plan mode (approval-first); `execute` starts looping in execute."""
    _enable_harness(monkeypatch, autonomy=autonomy)
    agent = build_agent(chat_client=object())
    mode = next(p for p in agent.context_providers if isinstance(p, AgentModeProvider))
    assert mode.default_mode == expected_mode


def test_harness_agent_still_audits_every_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single GxP audit middleware is attached on the harness path too, not just the classic."""
    _enable_harness(monkeypatch)
    agent = build_agent(chat_client=object())
    assert agent.middleware is not None
    assert any(True for _ in agent.middleware)  # at least the audit middleware is present


def test_classic_agent_has_no_harness_providers() -> None:
    """With the harness off (the default), no todo/mode providers are attached — the fallback."""
    agent = build_agent(chat_client=object())
    assert not any(
        isinstance(p, (TodoProvider, AgentModeProvider)) for p in agent.context_providers
    )


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
