"""Importing this module registers every in-process capability tool. That is its whole purpose.

The registry (`chemclaw.core.tool_registry`) is populated by import side effect, so every
consumer of `registered_tools()` must import the tool modules first. This module gives that
seeding a name both consumers (the agent and `api/mcp_face.py`) import explicitly;
`tests/test_tool_modules.py` checks in a fresh interpreter that the production entrypoint alone
advertises tools.
"""

from chemclaw.agent import analytical_tools as _analytical_tools  # noqa: F401
from chemclaw.agent import attachments as _attachments  # noqa: F401
from chemclaw.agent import commitment_tools as _commitment_tools  # noqa: F401
from chemclaw.agent import dialogue_tools as _dialogue_tools  # noqa: F401
from chemclaw.agent import durable_tools as _durable_tools  # noqa: F401
from chemclaw.agent import evidence_tools as _evidence_tools  # noqa: F401
from chemclaw.agent import exhibit_tools as _exhibit_tools  # noqa: F401
from chemclaw.agent import graph_tools as _graph_tools  # noqa: F401
from chemclaw.agent import memory_tools as _memory_tools  # noqa: F401
from chemclaw.agent import operations_tools as _operations_tools  # noqa: F401
from chemclaw.agent import pending_tools as _pending_tools  # noqa: F401
from chemclaw.agent import preferences as _preferences  # noqa: F401
from chemclaw.agent import proposal_tools as _proposal_tools  # noqa: F401
from chemclaw.agent import protocol_design_tools as _protocol_design_tools  # noqa: F401
from chemclaw.agent import protocol_tools as _protocol_tools  # noqa: F401
from chemclaw.agent import research_tools as _research_tools  # noqa: F401
from chemclaw.agent import subscriptions as _subscriptions  # noqa: F401
from chemclaw.agent import workflow_tools as _workflow_tools  # noqa: F401
