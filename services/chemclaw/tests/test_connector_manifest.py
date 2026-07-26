"""The connector manifest is a contract, not a config file — every way to get it wrong fails loudly.

Replaces `test_mcp_server_spec.py`, whose subject (`McpServerSpec`) the connector seam absorbed.
The properties worth keeping are the same ones: the transport union dispatches, the agent-facing
allow-list is transport-independent, and a field that does not belong to the chosen variant is a
config error rather than a silent drop. The properties that are *new* are the manifest's own:
a bundle must contribute something reachable, a job must declare its arguments exactly one way,
and a name may not be claimed twice.

Pure validation — no subprocess, no network, no filesystem.
"""

import pytest
from pydantic import ValidationError

from connectors.manifest import (
    BearerAuth,
    ConnectorManifest,
    HttpEndpoint,
    JobSpec,
    NoAuth,
    StdioEndpoint,
)

_HTTP = {"transport": "http", "url": "http://127.0.0.1:9/mcp", "tools": ["search"]}
_JOB = {
    "name": "run_thing",
    "workflow": "ThingWorkflow",
    "task_queue": "connector-thing",
    "summary": "Run the thing.",
}


def _manifest(**overrides: object) -> ConnectorManifest:
    """Build a valid manifest with `overrides` applied — the shared happy-path fixture."""
    payload: dict[str, object] = {"name": "thing", "description": "does a thing", "endpoint": _HTTP}
    payload.update(overrides)
    return ConnectorManifest.model_validate(payload)


def test_transport_tag_selects_its_variant() -> None:
    """`transport` picks the shape: a stdio entry needs no url, an http one no command."""
    http = _manifest()
    assert isinstance(http.endpoint, HttpEndpoint)
    stdio = _manifest(endpoint={"transport": "stdio", "command": "python", "args": ["-m", "x"]})
    assert isinstance(stdio.endpoint, StdioEndpoint)


def test_the_allow_list_lives_on_the_endpoint_for_both_transports() -> None:
    """The read/compute-only boundary is a property of the endpoint, so it cannot exist without one.

    Nesting `tools` under `endpoint` is what makes "an allow-list with nothing to serve it"
    unrepresentable rather than something a validator has to catch after the fact.
    """
    stdio = _manifest(
        endpoint={"transport": "stdio", "command": "python", "tools": ["similar_molecules"]}
    )
    assert stdio.endpoint is not None and stdio.endpoint.tools == ["similar_molecules"]
    with pytest.raises(ValidationError):
        # `tools` at the top level is not a field at all — the shape refuses the mistake.
        _manifest(tools=["search"])


def test_a_field_foreign_to_the_chosen_transport_is_rejected() -> None:
    """`extra="forbid"`: a stdio field on an http endpoint is a config error, not a silent drop."""
    with pytest.raises(ValidationError):
        _manifest(endpoint={"transport": "http", "url": "http://x/mcp", "command": "python"})


def test_an_unknown_transport_is_rejected() -> None:
    """An unknown tag fails loud rather than falling back to a default variant."""
    with pytest.raises(ValidationError):
        _manifest(endpoint={"transport": "carrier-pigeon", "url": "http://x/mcp"})


def test_auth_defaults_to_none_and_bearer_names_an_env_var() -> None:
    """A credential is never written into a manifest — the bearer variant names the variable."""
    assert isinstance(_manifest().endpoint.auth, NoAuth)  # type: ignore[union-attr]
    bearer = _manifest(
        endpoint={**_HTTP, "auth": {"mode": "bearer", "token_env": "CHEMCLAW_X_TOKEN"}}
    )
    auth = bearer.endpoint.auth  # type: ignore[union-attr]
    assert isinstance(auth, BearerAuth) and auth.token_env == "CHEMCLAW_X_TOKEN"


def test_a_bundle_must_contribute_something_reachable() -> None:
    """Neither an endpoint nor a job means nothing could ever reach it (the `SourceSpec` rule)."""
    with pytest.raises(ValidationError, match="must declare an endpoint, a job, or both"):
        ConnectorManifest.model_validate({"name": "empty", "description": "nothing"})
    # A jobs-only connector is legitimate: durable capability needs no MCP endpoint at all.
    assert (
        ConnectorManifest.model_validate(
            {"name": "jobs-only", "description": "durable only", "jobs": [_JOB]}
        ).endpoint
        is None
    )


def test_a_job_declares_its_arguments_exactly_one_way() -> None:
    """Inline params and a model reference are alternatives; declaring both is ambiguous."""
    inline = JobSpec.model_validate(
        {**_JOB, "params": [{"name": "smiles", "type": "string", "description": "the molecule"}]}
    )
    assert inline.params[0].name == "smiles"
    referenced = JobSpec.model_validate({**_JOB, "params_model": "bo.problem:CampaignSpec"})
    assert referenced.params_model == "bo.problem:CampaignSpec"
    with pytest.raises(ValidationError, match="declares both"):
        JobSpec.model_validate(
            {
                **_JOB,
                "params": [{"name": "a", "type": "string", "description": "a"}],
                "params_model": "bo.problem:CampaignSpec",
            }
        )


def test_a_param_must_be_documented_and_typed_from_the_closed_set() -> None:
    """The description becomes the schema's — an undocumented argument gets filled wrongly."""
    with pytest.raises(ValidationError):
        JobSpec.model_validate({**_JOB, "params": [{"name": "a", "type": "string"}]})
    with pytest.raises(ValidationError):
        JobSpec.model_validate(
            {**_JOB, "params": [{"name": "a", "type": "blob", "description": "x"}]}
        )


def test_duplicate_names_are_rejected_at_every_level() -> None:
    """A name is an identity — for a param inside a job, and for a job inside a connector."""
    with pytest.raises(ValidationError, match="duplicate parameter"):
        JobSpec.model_validate(
            {
                **_JOB,
                "params": [
                    {"name": "a", "type": "string", "description": "one"},
                    {"name": "a", "type": "integer", "description": "two"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="duplicate job name"):
        _manifest(jobs=[_JOB, _JOB])


def test_a_job_name_must_be_a_valid_tool_name() -> None:
    """The job name *is* the advertised tool name and the authorization key, so it is constrained.

    A name with a hyphen or a capital would be a tool the model calls by one spelling while
    `tool_role_gates` is written for another — the drift class the pattern exists to prevent.
    """
    with pytest.raises(ValidationError):
        JobSpec.model_validate({**_JOB, "name": "Run-Thing"})
