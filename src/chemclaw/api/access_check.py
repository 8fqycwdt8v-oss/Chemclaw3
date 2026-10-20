"""Startup check that the access policy names only things this deployment has.

A typo in the policy grants nothing, which reads as a role that mysteriously cannot work; refusing
at boot makes it a deployment error. The tool set is every tool any discovered bundle declares
(enabled or not), so a policy may name a bundle that is switched off here.
"""

from chemclaw.agent.profiles import registered_profile_names
from chemclaw.connectors import registry as connector_registry
from chemclaw.core.access_policy import AccessPolicyError, active_policy
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import registered_tool_names
from chemclaw.templates.registry import template_tool_names


def refuse_an_unknown_access_policy() -> None:
    """Fail startup when the configured policy names an unknown tool, connector, source or profile.

    Raises:
        AccessPolicyError: the file is unusable, or lists every unknown reference.
    """
    # Deferred: the ingest package is not an `api` dependency at import time.
    from chemclaw.ingest.sources import registry as source_registry

    policy = active_policy()
    if policy is None:
        return
    problems = policy.unknown_references(
        tools=[
            *registered_tool_names(),
            *connector_registry.declared_connector_tool_names(),
            *template_tool_names(declared=True),
        ],
        connectors=connector_registry.discovered().keys(),
        sources=source_registry.discovered().keys(),
        profiles=registered_profile_names(),
    )
    if problems:
        raise AccessPolicyError(
            f"{settings.access_policy_file} names things this deployment does not have: "
            + "; ".join(problems)
        )
