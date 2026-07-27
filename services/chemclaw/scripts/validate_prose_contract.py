"""Check that the agent's prose only names capability the agent actually has (gap IDEA-7).

Two of this codebase's real defects were the same shape — prose promising something the code could
not do, invisible to `mypy`, `pytest`, and `make skill-validate` (which only checks frontmatter):

- `skills/experiment-design/SKILL.md` told the agent to "reach for" `BoCampaignWorkflow`, which no
  tool exposed, so the instruction pointed at an uninvocable capability (gap RCH-2).
- The agent instructions advertised answers about "purity, impurities" while the canonical reaction
  schema carried no such field (gap KNW-2).

Both are cheap to catch mechanically, and this is the check that does it. It is also the
*deterministic half* of the deferred agent-behavior eval (AG-13): AG-13 waits on a live LLM to
observe tool **selection**, but whether a named tool exists at all needs no model.

Two rules, both deliberately narrow so the check stays true rather than noisy:

1. Every `name(`-style call mentioned in a skill or in the agent instructions must be a registered
   agent tool, a tool an enabled connector advertises, or an explicitly allowlisted helper.
2. A skill must not direct the agent at a `*Workflow` class. The agent cannot invoke a workflow;
   it can only call a tool. Naming one is always either a dangling pointer or a missing tool.

Run via `make prose-validate`; gated in CI beside `kg-validate` and `skill-validate`.
"""

import re
import sys
from pathlib import Path

from agents.chemclaw_agent import _INSTRUCTIONS, _capability_tools
from chemclaw.config import settings
from connectors.registry import connector_tool_names
from connectors.registry import skills_dirs as connector_skills_dirs

# Symbols a skill may legitimately name in call form that are not agent tools: library/graph
# internals a skill explains conceptually. Kept explicit and short — adding one is a review
# decision, which is the friction this check exists to create.
_ALLOWED_NON_TOOLS = frozenset(
    {
        "neighborhood",  # kg.graph traversal primitive, explained conceptually by the query skill
    }
)

_CALL = re.compile(r"`([a-z_][a-z0-9_]*)\(")
_WORKFLOW = re.compile(r"`([A-Za-z][A-Za-z0-9]*Workflow)`")


def _registered_tool_names() -> set[str]:
    """Every tool the agent can actually call: in-process functions plus every connector tool."""
    names: set[str] = set()
    for tool in _capability_tools():
        name = getattr(tool, "__name__", None)
        if name:
            names.add(name)
    # A connector's endpoint tools are named by the manifest's allow-list, not by a Python symbol
    # this process holds, so they are collected from the manifests — the same place `build_agent`
    # reads them. `connector_tool_names` also covers the generated job launchers, which *are*
    # registry tools, so the union is idempotent rather than double-counted.
    names.update(connector_tool_names())
    return names


def _prose_sources() -> dict[str, str]:
    """The agent-facing prose to check: every SKILL.md plus the built-in instructions."""
    sources = {"agents/chemclaw_agent.py::_INSTRUCTIONS": _INSTRUCTIONS}
    for skills_dir in [*settings.skills_dirs, *connector_skills_dirs()]:
        for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
            sources[str(path)] = path.read_text()
    return sources


def check_prose_contract() -> list[str]:
    """Return one problem string per violation; empty means the prose matches the tool surface."""
    tools = _registered_tool_names()
    problems: list[str] = []
    for origin, text in _prose_sources().items():
        for name in sorted(set(_CALL.findall(text))):
            if name not in tools and name not in _ALLOWED_NON_TOOLS:
                problems.append(
                    f"{origin}: names `{name}(...)` but no such agent tool is registered"
                )
        for workflow_name in sorted(set(_WORKFLOW.findall(text))):
            problems.append(
                f"{origin}: directs the agent at `{workflow_name}`, which it cannot invoke — "
                "name the tool that starts it instead"
            )
    return problems


def main() -> int:
    """CLI: report every prose/tool mismatch; non-zero exit fails the CI gate."""
    problems = check_prose_contract()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} prose/tool mismatch(es)", file=sys.stderr)
        return 1
    print("prose contract OK: every named tool exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
