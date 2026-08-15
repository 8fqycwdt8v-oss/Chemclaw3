"""The compiled middleware sequence, pinned — the instrument the `create_deep_agent` swap needs.

**Why this exists before the swap rather than after it.** Every other change in this workstream
fails loudly: delete a module and the prose gate reddens, add a filesystem tool and the cache-floor
ratchet reddens, break the audit ordering and the MCP tests redden. `create_deep_agent` is the one
remaining change whose failure modes are *silent*, and three of them are:

- **`_apply_custom_middleware` splices by `.name`.** Upstream's is `"SkillsMiddleware"`;
  `ReloadingSkillsMiddleware` reports its own class name, so it would be *appended beside*
  upstream's rather than replacing it. Two skills middlewares, one of which caches a role-narrowed
  listing — and the symptom is a chemist occasionally offered a skill their role no longer holds.
- **`create_deep_agent` inserts a general-purpose subagent by default.** It holds every tool the
  parent holds and none of this repository's middleware, because `create_sub_agent` builds a bare
  `SubAgent` from *only* `spec["middleware"]`. That is a `task` tool with no audit trail, no
  per-tool authorization, no dry-run gate and no plan gate;
  `D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks` recorded that "nothing
  would fail while it did".
- **It returns a `RunnableBinding`,** not a `CompiledStateGraph` — it ends
  `.with_config({"recursion_limit": 9_999, …})` — so `aget_state` is absent and the baked ceiling
  sits outside `turn_config`'s.

None of those turns a test red on its own. This file is what makes them reviewable: the order is
asserted at construction, and the *effect* of the order is asserted by running a tool through the
compiled graph. Both halves are needed. Order alone is the shape-without-effect failure
`tasks/lessons.md` rule 27 names; effect alone would not notice a second skills middleware.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.audit import AuditEvent, AuditSink
from chemclaw.agent.profiles import AgentProfile
from tests.fakes import scripted

# The sequence as it compiles today, outermost first. `create_agent` nests `wrap_tool_call` in list
# order, so position here *is* nesting depth: entry 0 sees a tool call before entry 1 does.
#
# Recorded rather than derived, and the point is that changing it must be deliberate. Two positions
# carry an argument that is not obvious from the name:
#
# - `FilesystemMiddleware` precedes `SkillsMiddleware` because the skills prompt tells the model to
#   read a `SKILL.md` with `read_file`, and the filesystem middleware is what registers it.
# - the seven `wrap_tool_call` wrappers sit *inside* both, so a filesystem write and a skill read
#   cross the audit row and the authorization gate exactly like any other tool call.
_EXPECTED_ORDER = (
    "FilesystemMiddleware",
    "ReloadingSkillsMiddleware",
    "surface_authorization_denials",
    "surface_domain_errors",
    "announce_tool_failures",
    "audit_tool_calls",
    "enforce_tool_authz",
    "refuse_writes_on_dry_run",
    "refuse_repeated_calls",
    "AnthropicPromptCachingMiddleware",
    "ContextEditingMiddleware",
    "RecordContextCompaction",
)


def _middleware_names(**kwargs: Any) -> list[str]:
    """Build an agent and report the middleware `create_agent` was handed, in order.

    Captured at the call rather than read off the compiled graph, because a `CompiledStateGraph`
    exposes its nodes and not the middleware that produced them — so the list is only observable
    where it is passed.
    """
    # The real function is taken from `langchain.agents`, not from `langgraph_agent`, and patched
    # by dotted name. Both are for mypy rather than taste: `langgraph_agent` *imports*
    # `create_agent` rather than defining it, so reading it back off that module is an
    # `attr-defined` error under
    # `--strict`. It is the same object either way — which is exactly why taking it from the source
    # is not a workaround but the more honest reference.
    from langchain.agents import create_agent as real

    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    captured: list[str] = []

    def spy(*args: Any, **call_kwargs: Any) -> Any:
        for entry in call_kwargs.get("middleware", ()):
            name = getattr(entry, "name", None) or getattr(entry, "__name__", None)
            captured.append(name or type(entry).__name__)
        return real(*args, **call_kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("chemclaw.agent.langgraph_agent.create_agent", spy)
        build_langgraph_agent(
            model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])), **kwargs
        )
    return captured


def test_the_middleware_sequence_is_the_recorded_one() -> None:
    """The order is load-bearing, so it is a value a reviewer adjudicates rather than a side effect.

    A change here is not necessarily wrong — it is necessarily *deliberate*. The failure this
    catches is the one that has no other symptom: a middleware that arrives beside the one it was
    meant to replace, or a governance wrapper that ends up outside the thing it was meant to wrap.
    """
    assert tuple(_middleware_names()) == _EXPECTED_ORDER


def test_every_governance_wrapper_sits_inside_the_capability_middleware() -> None:
    """The property the order exists for, stated independently of the exact sequence.

    Written as a relation rather than a list so it survives a deliberate reordering: whatever else
    moves, a tool call must cross the audit row and the authorization gate, and the middleware that
    *registers* tools must be outside them — otherwise a filesystem write or a `task` call would
    execute through a chain that never saw it.
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
    """The `.name` splice hazard, asserted directly.

    `ReloadingSkillsMiddleware` exists because upstream caches its listing across turns while this
    repository's listing is narrowed by the caller's role. Two of them in one chain means the
    narrowed one is shadowed by a cached one, and the only symptom is a stale skill offer.
    """
    names = _middleware_names()
    skills = [n for n in names if "Skills" in n]
    assert skills == ["ReloadingSkillsMiddleware"], (
        f"expected exactly one skills middleware, found {skills}. Upstream's caches its listing "
        "across turns; the subclass re-narrows it per caller."
    )


def test_no_subagent_middleware_is_attached_today() -> None:
    """Pinned as an *absence*, so adding one is a decision this file records.

    The specialist team was deleted in D-2026-08-15 and nothing spawns a helper today. When
    subagents return they must be `CompiledSubAgent`s built through `build_langgraph_agent` — a
    bare `SubAgent` dict gets only `spec["middleware"]`, which is none of the above. This assertion
    is what makes that arrival visible instead of ambient.
    """
    assert not [n for n in _middleware_names() if "SubAgent" in n]


class _Recording(AuditSink):
    """An audit sink that keeps what it was given, so a test can read the trail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def test_a_filesystem_write_crosses_the_audit_trail() -> None:
    """The effect the ordering exists for, on the tools the scratchpad added.

    This is the half that a list cannot give: `write_file` is registered by upstream middleware, not
    by this repository, so "the chain wraps our tools" is not the same claim as "the chain wraps the
    tools upstream added". A scratchpad write that never reached the audit row would be a durable
    side effect with no record that it happened.
    """
    sink = _Recording()
    from chemclaw.agent.langgraph_agent import build_langgraph_agent

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
