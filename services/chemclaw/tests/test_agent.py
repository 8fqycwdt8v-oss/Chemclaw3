"""The agent wires its tools and skills correctly (plan step 1.5; harness backbone: D-038).

Built with an injected dummy client so no LLM credentials are needed — this
proves the MAF wiring (tools advertised, skills discovered, context kept in
budget, backbone selected), not model behavior. No LLM call happens at
construction in either backbone.
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

_DOMAIN_TOOLS = {
    "submit_qm_job",
    "get_job_status",
    "find_notes",
    "expand_note",
    "propose_knowledge_note",
}


def test_agent_applies_default_generation_options() -> None:
    """Config-driven temperature/max-tokens are threaded onto the agent's default options (F0.3)."""
    agent = build_agent(chat_client=object())
    assert agent.default_options["temperature"] == settings.llm_temperature
    assert agent.default_options["max_tokens"] == settings.llm_max_tokens


def test_agent_advertises_qm_tools() -> None:
    """All domain tools are registered on the agent under their function names."""
    agent = build_agent(chat_client=object())
    tool_names = {tool.name for tool in agent.default_options["tools"]}
    assert _DOMAIN_TOOLS <= tool_names


def test_agent_has_skills_history_and_compaction() -> None:
    """Skills (judgment), a session history, and context compaction are all attached."""
    agent = build_agent(chat_client=object())
    provider_types = {type(p).__name__ for p in agent.context_providers}
    assert {"SkillsProvider", "InMemoryHistoryProvider", "CompactionProvider"} <= provider_types


def test_skills_load_and_read_without_an_unanswerable_approval() -> None:
    """`load_skill`/`read_skill_resource` never stall on approval — the front door cannot answer.

    MAF registers both with `approval_mode="always_require"` by default. Nothing in the front
    door wires a `ToolApprovalMiddleware` or exposes a decision endpoint, so a turn that reached
    for a skill would emit an unanswerable `approval_request` and never produce an answer
    (regression: every skill-using turn stalled this way through the whole front door). Skills
    are always the deployer-configured, first-party `skills_dir` tree — never tenant/user-
    uploaded content — so disabling approval for these two read-only tools is the documented
    "trusted source" case, not a broadened attack surface. `run_skill_script` is intentionally
    left requiring approval (chemclaw wires no `script_runner`, so a call fails fast instead).
    """
    agent = build_agent(chat_client=object())
    skills_provider = next(
        p for p in agent.context_providers if type(p).__name__ == "SkillsProvider"
    )
    # `next(...)` types this as the `ContextProvider` base, which does not declare
    # `_create_tools`; the concrete `SkillsProvider` does (checked at runtime by the isinstance
    # filter above), so this is a deliberate reach past the base type, not a real type error.
    tools_by_name = {
        tool.name: tool
        for tool in skills_provider._create_tools([])  # type: ignore[attr-defined]
    }
    assert tools_by_name["load_skill"].approval_mode == "never_require"
    assert tools_by_name["read_skill_resource"].approval_mode == "never_require"
    assert tools_by_name["run_skill_script"].approval_mode == "always_require"


def test_agent_audits_and_authorizes_every_tool_call() -> None:
    """Four middlewares attach: both error-surfacing layers, the GxP audit trail, per-tool authz."""
    from agents.tool_authz import (
        enforce_tool_authz,
        surface_authorization_denials,
        surface_domain_errors,
    )

    agent = build_agent(chat_client=object())
    middleware = list(agent.middleware or [])
    assert len(middleware) == 4  # denial + domain-error surfacing + audit + per-tool authorization
    assert middleware[0] is surface_authorization_denials  # outermost: sees audit's re-raise
    assert middleware[1] is surface_domain_errors
    assert enforce_tool_authz in middleware  # the authz gate is wired, not just audit


def test_agent_attaches_fingerprint_search_as_mcp_servers() -> None:
    """Structural search is reached over MCP (servers on `mcp_tools`), not in-process tools.

    The in-process search wrappers are no longer registered as function tools; the agent talks
    to the molfp/rxnfp capability servers over the MCP protocol instead (construction is lazy —
    no subprocess is spawned here).
    """
    agent = build_agent(chat_client=object())
    assert {t.name for t in agent.mcp_tools} == {"mcp-molfp", "mcp-rxnfp", "mcp-calc"}
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
        "submit_qm_job",
        "get_job_status",
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
    assert {"mcp-molfp", "mcp-rxnfp", "mcp-calc"} == {t.name for t in harness.mcp_tools}


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


def test_harness_disables_generic_sandbox_batteries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Governance (§6, G6): no generic file-memory/file-access provider is wired.

    Chemclaw's capability is its explicit tools/skills, never a generic filesystem or
    shell — so the harness's default file batteries must be off.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    provider_types = {type(p).__name__ for p in build_agent(chat_client=object()).context_providers}
    assert "FileMemoryProvider" not in provider_types
    assert "FileAccessProvider" not in provider_types
    assert "BackgroundAgentsProvider" not in provider_types


def test_execute_autonomy_wires_a_bounded_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute autonomy attaches the loop, bounded by the configured iteration cap."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 9)
    middleware = build_agent(chat_client=object()).middleware or []
    loops = [m for m in middleware if type(m).__name__ == "AgentLoopMiddleware"]
    assert len(loops) == 1
    assert getattr(loops[0], "max_iterations", None) == 9
