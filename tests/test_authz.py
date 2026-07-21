"""Tool-authorization policy at the in-process tool boundary (Phase 6, plan step 6.1).

Proves the gate the authz middleware enforces: ungated tools run for anyone, a gated tool runs
only for a caller holding one of its roles, and an anonymous caller is denied any gated tool.
The middleware is a thin wrapper that calls `authorize` before the tool, so testing the pure
policy is testing the behavior.
"""

import pytest

from agents.authz import ToolNotAuthorizedError, authorize
from chemclaw.identity import Principal

_GATES = {"submit_qm_job": ["compute"]}


def test_ungated_tool_is_allowed_for_anyone() -> None:
    """A tool with no gate runs for an anonymous caller and regardless of roles."""
    authorize(None, "compute_xtb_energy", _GATES)  # no raise
    authorize(Principal(oid="u", roles=frozenset({"lab"})), "compute_xtb_energy", _GATES)


def test_gated_tool_allowed_with_a_required_role() -> None:
    """A caller holding one of the gate's roles may call the gated tool."""
    authorize(Principal(oid="u", roles=frozenset({"compute"})), "submit_qm_job", _GATES)


def test_gated_tool_denied_without_the_role() -> None:
    """A caller lacking the role is denied, and the error names the tool and needed role."""
    caller = Principal(oid="u", roles=frozenset({"lab"}))
    with pytest.raises(ToolNotAuthorizedError) as excinfo:
        authorize(caller, "submit_qm_job", _GATES)
    assert excinfo.value.tool == "submit_qm_job"
    assert "compute" in str(excinfo.value)


def test_gated_tool_denied_for_anonymous_caller() -> None:
    """No principal means no roles — a gated tool is denied (fail-closed)."""
    with pytest.raises(ToolNotAuthorizedError):
        authorize(None, "submit_qm_job", _GATES)
