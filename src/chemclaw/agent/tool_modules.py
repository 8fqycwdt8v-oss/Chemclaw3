"""Importing this module registers every in-process capability tool. That is its whole purpose.

**Why it exists as a module rather than as a block inside one consumer.** The registry
(`chemclaw.core.tool_registry`) is populated by import side effect — a `@tool` decorator runs when
the module defining it is imported — which is the right design and has one sharp edge: *every*
consumer of `registered_tools()` must first import the tool modules, and nothing makes it.

That edge drew blood. The import block lived inside `agent/chemclaw_agent.py`, so
`build_langgraph_agent` was correct and `api/mcp_face.py` — a second consumer written later, which
imports `authz`, `settings` and the registry but not the agent — served **zero tools** in
production: `advertised_tools()` returned `[]`, the pod logged "serving 0 read-only tool(s)",
answered `tools/list` with an empty array, and passed its readiness probe. Five tests covered it and
all five passed, because `tests/test_mcp_face.py` imports `chemclaw_agent` itself to populate the
registry — so the suite tested a registry the shipped process never had.

One module both consumers import is what makes that unrepeatable: the seeding has a name, the
dependency is explicit at each call site, and `tests/test_tool_modules.py` asserts in a *fresh
interpreter* that the production entrypoint alone advertises tools — the only form of that test
that can fail, since any test importing the agent first would pass either way.
"""

from chemclaw.agent import attachments as _attachments  # noqa: F401
from chemclaw.agent import commitment_tools as _commitment_tools  # noqa: F401
from chemclaw.agent import dialogue_tools as _dialogue_tools  # noqa: F401
from chemclaw.agent import durable_tools as _durable_tools  # noqa: F401
from chemclaw.agent import evidence_tools as _evidence_tools  # noqa: F401
from chemclaw.agent import graph_tools as _graph_tools  # noqa: F401
from chemclaw.agent import memory_tools as _memory_tools  # noqa: F401
from chemclaw.agent import operations_tools as _operations_tools  # noqa: F401
from chemclaw.agent import pending_tools as _pending_tools  # noqa: F401
from chemclaw.agent import preferences as _preferences  # noqa: F401
from chemclaw.agent import protocol_design_tools as _protocol_design_tools  # noqa: F401
from chemclaw.agent import protocol_tools as _protocol_tools  # noqa: F401
from chemclaw.agent import research_tools as _research_tools  # noqa: F401
from chemclaw.agent import subscriptions as _subscriptions  # noqa: F401
