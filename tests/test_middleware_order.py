"""The compiled middleware sequence, pinned.

The `create_deep_agent` assembly can fail silently in three ways:

- `_apply_custom_middleware` splices by `.name`: a matching name replaces upstream's entry in
  place, a new name lands after the last core member. That is the difference between our
  `FilesystemMiddleware` withholding `execute`/`delete` and upstream's offering them beside it.
- The governance wrappers must stay inside every middleware that registers a tool; their position
  follows upstream's splice rule, which is not promised.
- Two skills middlewares: a cached listing could shadow the role-narrowed one.

The order is asserted at construction and its effect by running a tool through the compiled graph;
both halves are needed. The helper behind `task` is covered by `tests/test_subagents.py`.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.audit import AuditEvent, AuditSink
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from tests.fakes import scripted

# The sequence as it compiles, outermost first. `create_agent` nests `wrap_tool_call` in list order,
# so position is nesting depth. Recorded rather than derived so a change must be deliberate. Entries
# 0-3 are upstream's core stack; ours land after the last core member. Positions worth explaining:
#
# - `FilesystemMiddleware` is ours, occupying upstream's slot by sharing its name, which withholds
#   `execute` and `delete`.
# - The `wrap_tool_call` wrappers sit inside it and inside `SubAgentMiddleware`, so scratchpad
#   writes
#   and `task` spawns cross the audit row and authorization gate like any tool call.
# - `AnthropicPromptCachingMiddleware` is upstream's, appended unconditionally by
#   `deepagents.graph`;
#   with `unsupported_model_behavior="ignore"` and a `ChatOpenAI` client it no-ops.
# - `enforce_loop_cap` is on every build, harness or not; it is a model-call hook, not a tool gate.
# - `enforce_spend_cap` (`before_model`) and `MeterTurnSpend` (`wrap_model_call`) are split because
#   only the response carries the bill, while an `after_model` counter can be skipped by a jump.
_EXPECTED_ORDER = (
    "FilesystemMiddleware",
    "SubAgentMiddleware",
    "SummarizationMiddleware",
    "PatchToolCallsMiddleware",
    # A capability middleware, not a gate: it owns the `todos` channel and contributes
    # `write_todos`. The plan gate must nest inside it, because `enforce_plan_approval` reads
    # `request.state["todos"]`.
    "ScopedTodoListMiddleware",
    # A `before_model` hook, ahead of the caps: a turn that lost its session makes no further model
    # call, so nothing downstream is counted or billed for it.
    "HoldClaimBeforeModel",
    "enforce_loop_cap",
    "enforce_spend_cap",
    # The other half of `enforce_loop_cap`: that hook authorises one call per graph past the cap
    # and marks it, and this turns the marked call into an answer (tools off, a system note, any
    # tool call dropped). Adjacent for readability only — it reads a state mark, not a neighbour.
    "AnswerAtTheCap",
    "MeterTurnSpend",
    # A `before_agent` hook, so its position carries no nesting argument: it runs once, before the
    # first model call, and removes `files` entries past `agent_scratch_retention_days`.
    "expire_stale_scratch",
    "ReloadingSkillsMiddleware",
    "surface_authorization_denials",
    "surface_domain_errors",
    # Outermost of what rewrites a result, so the handle line lies outside the envelope and the
    # defang.
    "stamp_result_handles",
    # Inside both converters and outside the trail: a refusal this system composed must not be
    # wrapped in the evidence envelope, and the announcer and audit trail must read what the tool
    # returned, not what the model is shown.
    "frame_connector_results",
    # Inside the framing and outside the trail: the envelope wraps an already-bounded payload, and
    # `audit_events.detail` records what the tool returned (`agent/tool_result_size.py`).
    "bound_tool_results",
    "announce_tool_failures",
    "audit_tool_calls",
    "enforce_tool_authz",
    "refuse_writes_on_dry_run",
    "refuse_repeated_calls",
    # Innermost of the deciding gates: a mis-serialised call promoted by `PromoteInvalidToolCalls`
    # reaches this chain, so the announcer, trail, authorization gate and guards all see it before
    # it is refused, and it still raises before the tool body. The two harness entries below nest
    # inside it; `tests/test_invalid_tool_calls.py` states the relation for both arrangements.
    "refuse_unparsed_arguments",
    # Inside the guard: a promoted call never reaches the plan gate, since unparsed arguments are
    # not a request to decide about. Below every other gate because `side_effecting_call(name,
    # args)` reads arguments settled by everything above.
    "enforce_plan_approval",
    "stamp_plan_link",
    # Innermost of every tool gate: the ownership check sits as close to the effect as the chain
    # allows, so a call a gate refused never costs a claim lookup.
    "refuse_when_claim_lost",
    # Above the compaction group, so the preferences it appends to the system message are charged
    # as prefix by `MeasureRequestPrefix` rather than missed by it (`agent/preferences.py`).
    "StandingPreferences",
    # The artefact listing, on the same terms: request-only state, above the compaction group so
    # it is charged as prefix (`agent/exhibit_notes.py`).
    "ExhibitListing",
    # Outermost of the compaction group: a `ContextEdit` sees a message list and a counter, never
    # the request, so the prefix it must budget against can only be published by a middleware above
    # the editor (`agent/context_budget.py`).
    "MeasureRequestPrefix",
    "OffLoopContextEditing",
    "RecordContextCompaction",
    # The two model-call observers, closest to the provider call and below the compaction group, so
    # compaction's own token counting is not folded into endpoint latency. The promotion is outside
    # the recorder because it reads the response the recorder timed and makes no provider call.
    "PromoteInvalidToolCalls",
    "RecordModelCalls",
    "AnthropicPromptCachingMiddleware",
)


def _middleware_names(**kwargs: Any) -> list[str]:
    """Build an agent and report the middleware `create_agent` was finally handed, in order.

    Patched inside `deepagents.graph`, because upstream splices our list into its own stack by
    `.name` and a compiled graph exposes nodes, not middleware. The `task` helper compiles through
    `langchain.agents.create_agent` directly, so it does not pass this spy and one build yields one
    list.
    """
    from langchain.agents import create_agent as real

    captured: list[str] = []

    def spy(*args: Any, **call_kwargs: Any) -> Any:
        for entry in call_kwargs.get("middleware", ()):
            name = getattr(entry, "name", None) or getattr(entry, "__name__", None)
            captured.append(name or type(entry).__name__)
        return real(*args, **call_kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("deepagents.graph.create_agent", spy)
        build_langgraph_agent(
            model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])), **kwargs
        )
    return captured


def test_the_middleware_sequence_is_the_recorded_one() -> None:
    """The middleware sequence is the recorded one.

    A change is not necessarily wrong but must be deliberate: it catches a middleware arriving
    beside the one it should replace, or a governance wrapper outside what it should wrap.
    """
    assert tuple(_middleware_names()) == _EXPECTED_ORDER


def test_every_governance_wrapper_sits_inside_the_capability_middleware() -> None:
    """Every governance wrapper sits inside the middleware that registers tools.

    A relation rather than a list, so it survives deliberate reordering: a filesystem write or
    `task` call must cross the audit row and authorization gate.
    """
    names = _middleware_names()
    registrars = [
        i for i, n in enumerate(names) if n in {"FilesystemMiddleware", "SubAgentMiddleware"}
    ]
    assert registrars, "no tool-registering middleware found — has the composition changed?"
    for gate in ("audit_tool_calls", "enforce_tool_authz", "refuse_writes_on_dry_run"):
        assert names.index(gate) > max(registrars), (
            f"{gate} is outside the middleware that registers tools, so the tools they add would "
            "execute without crossing it"
        )


def test_the_skills_middleware_appears_exactly_once() -> None:
    """The skills middleware appears exactly once.

    Upstream caches its listing across turns while ours is narrowed by the caller's role; two in one
    chain would shadow the narrowed listing with a stale one.
    """
    names = _middleware_names()
    skills = [n for n in names if "Skills" in n]
    assert skills == ["ReloadingSkillsMiddleware"], (
        f"expected exactly one skills middleware, found {skills}. Upstream's caches its listing "
        "across turns; the subclass re-narrows it per caller."
    )


def test_the_filesystem_middleware_is_the_one_that_withholds_the_shell() -> None:
    """Exactly one `FilesystemMiddleware`, and it is the narrowed one that withholds the shell.

    Two would mean ours landed beside upstream's, whose eight verbs include `execute` (a shell) and
    `delete`.
    """
    from chemclaw.agent.scratchpad import scratchpad_tools

    names = _middleware_names()
    assert names.count("FilesystemMiddleware") == 1
    graph = build_langgraph_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    )
    bound = set(graph.nodes["tools"].bound.tools_by_name)
    assert not {"execute", "delete"} & bound, (
        "upstream's filesystem middleware is registering its full verb set: this deployment "
        "withholds the shell and the delete verb, and the narrowing is carried by replacing that "
        "middleware by name"
    )
    assert set(scratchpad_tools()) <= bound


class _Recording(AuditSink):
    """An audit sink that keeps what it was given, so a test can read the trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def test_a_filesystem_write_crosses_the_audit_trail() -> None:
    """A filesystem write crosses the audit trail.

    `write_file` is registered by upstream middleware, so the chain wrapping our tools does not
    imply it wraps upstream's; an unaudited scratchpad write would be a durable side effect with no
    record.
    """
    sink = _Recording()

    graph = build_langgraph_agent(
        model=scripted("write_file", {"file_path": "/scratch/notes.md", "content": "hello"}),
        profile=AgentProfile(name="default"),
        audit_sink=sink,
    )
    asyncio.run(graph.ainvoke({"messages": [("user", "write a note")]}, {"recursion_limit": 25}))
    written = [event for event in sink.events if event.tool == "write_file"]
    assert written, (
        "a scratchpad write reached no audit row: the governance chain does not wrap the tools "
        f"upstream middleware registers. Recorded: {[e.tool for e in sink.events]}"
    )
