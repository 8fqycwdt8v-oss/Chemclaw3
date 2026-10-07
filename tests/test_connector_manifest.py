"""The connector manifest is a contract, not a config file — every way to get it wrong fails loudly.

The transport union dispatches, the agent-facing allow-list is transport-independent, a field
foreign to the chosen variant is an error, a bundle must contribute something reachable, a job
declares its arguments exactly one way, and a name may not be claimed twice. Pure validation;
the one loader test checks that a failure names its file.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.connectors.manifest import (
    BearerAuth,
    ConnectorManifest,
    HttpEndpoint,
    JobSpec,
    NoAuth,
    StdioEndpoint,
)
from chemclaw.core.config import settings

# Every endpoint must classify each tool it serves (D-167), so the shared fixture does too.
_HTTP = {
    "transport": "http",
    "url": "http://127.0.0.1:9/mcp",
    "tools": ["search"],
    "read_only": ["search"],
}
_JOB = {
    "name": "run_thing",
    "workflow": "ThingWorkflow",
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
    stdio = _manifest(
        endpoint={
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "x"],
            "tools": ["search"],
            "read_only": ["search"],
        }
    )
    assert isinstance(stdio.endpoint, StdioEndpoint)


def test_the_allow_list_lives_on_the_endpoint_for_both_transports() -> None:
    """The read/compute-only boundary is a property of the endpoint, so it cannot exist without one.

    Nesting `tools` under `endpoint` is what makes "an allow-list with nothing to serve it"
    unrepresentable rather than something a validator has to catch after the fact.
    """
    stdio = _manifest(
        endpoint={
            "transport": "stdio",
            "command": "python",
            "tools": ["similar_molecules"],
            "read_only": ["similar_molecules"],
        }
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


def test_a_networked_endpoint_may_not_declare_no_credential() -> None:
    """A networked endpoint may not declare no credential.

    Otherwise an unauthenticated MCP call carries the turn's actor to a host outside our trust
    boundary. Loopback stays free (shipped dev defaults), and a bearer credential makes any host
    legal.
    """
    with pytest.raises(ValidationError, match="is not loopback"):
        _manifest(endpoint={**_HTTP, "url": "https://model.vendor.example/mcp"})
    # The same URL with a credential is fine; so is loopback with none, in either spelling.
    _manifest(
        endpoint={
            **_HTTP,
            "url": "https://model.vendor.example/mcp",
            "auth": {"mode": "bearer", "token_env": "CHEMCLAW_VENDOR_TOKEN"},
        }
    )
    _manifest(endpoint={**_HTTP, "url": "http://localhost:9/mcp"})
    _manifest(endpoint={**_HTTP, "url": "http://[::1]:9/mcp"})
    # An in-cluster Service name is *not* loopback, which is the point: a manifest may not ship
    # naming one. A deployment still moves any bundle there through CHEMCLAW_CONNECTOR_URLS, which
    # this rule deliberately does not police (that address is the operator's own infrastructure).
    with pytest.raises(ValidationError, match="is not loopback"):
        _manifest(endpoint={**_HTTP, "url": "http://chemclaw-connector-molfp:8080/mcp"})


def test_a_stdio_endpoint_needs_no_credential_at_all() -> None:
    """A stdio endpoint needs no credential at all.

    The rule is about a network hop; lifting it onto the shared `tools` surface would make the local
    stdio path undeclarable.
    """
    stdio = _manifest(
        endpoint={
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "thing"],
            "tools": ["search"],
            "read_only": ["search"],
        }
    )
    assert isinstance(stdio.endpoint, StdioEndpoint)
    assert not hasattr(stdio.endpoint, "auth"), "a stdio endpoint has no credential to declare"


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
    referenced = JobSpec.model_validate(
        {**_JOB, "params_model": "chemclaw.science.bo.problem:CampaignSpec"}
    )
    assert referenced.params_model == "chemclaw.science.bo.problem:CampaignSpec"
    with pytest.raises(ValidationError, match="declares both"):
        JobSpec.model_validate(
            {
                **_JOB,
                "params": [{"name": "a", "type": "string", "description": "a"}],
                "params_model": "chemclaw.science.bo.problem:CampaignSpec",
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


def test_a_typo_in_the_classification_is_refused_rather_than_ignored() -> None:
    """A typo in the classification is refused rather than ignored.

    A misspelled `state_changing` entry leaves the real tool ungated (fails open), so it is a
    load-time error, and the intended tool then shows as unclassified (D-167).
    """
    with pytest.raises(ValidationError, match="does not serve"):
        HttpEndpoint.model_validate(
            {
                "url": "http://127.0.0.1:8899/mcp",
                "tools": ["compute_xtb_energy"],
                "state_changing": ["compute_xtb_enrgy"],
                "read_only": ["compute_xtb_energy"],
            }
        )
    with pytest.raises(ValidationError, match="both state_changing and read_only"):
        HttpEndpoint.model_validate(
            {
                "url": "http://127.0.0.1:8899/mcp",
                "tools": ["compute_xtb_energy"],
                "state_changing": ["compute_xtb_energy"],
                "read_only": ["compute_xtb_energy"],
            }
        )


def test_a_tool_listed_twice_in_one_endpoint_is_refused_where_it_is_readable() -> None:
    """A tool listed twice in one endpoint is refused where it is readable.

    Otherwise the registry reports the bundle colliding with itself, which is unactionable; only the
    manifest can still see the repetition.
    """
    with pytest.raises(ValidationError, match="lists tool.*more than once"):
        HttpEndpoint.model_validate(
            {
                "url": "http://127.0.0.1:8899/mcp",
                "tools": ["resolve_compound", "resolve_compound"],
                "read_only": ["resolve_compound"],
            }
        )


def test_an_unclassified_tool_refuses_to_load() -> None:
    """An unclassified tool refuses to load.

    Defaulting to read would leave the harness gate to memory; defaulting to write would gate every
    lookup. Refusing cannot be wrong quietly.
    """
    with pytest.raises(ValidationError, match="does not say whether"):
        HttpEndpoint.model_validate(
            {"url": "http://127.0.0.1:8899/mcp", "tools": ["resolve_compound"]}
        )


def test_an_endpoint_that_declares_no_tools_refuses_to_load() -> None:
    """An endpoint that declares no tools refuses to load.

    An empty partition is trivially classified, yet the registry would read it as "no allow-list",
    bind the server's whole surface and treat every tool, writes included, as non-side-effecting for
    the plan and dry-run gates (D-167).
    """
    for endpoint in (HttpEndpoint, StdioEndpoint):
        payload = (
            {"url": "http://127.0.0.1:8899/mcp"}
            if endpoint is HttpEndpoint
            else {"command": "/bin/true"}
        )
        with pytest.raises(ValidationError, match="declares no tools"):
            endpoint.model_validate(payload)


def test_a_job_may_declare_its_own_ceiling_and_a_bad_number_is_refused() -> None:
    """A job may declare its own ceiling, and a bad number is refused.

    `None` means "the deployment's ceiling" to `child_execution_timeout`, so zero or negative values
    must be refused at load (`gt=0`) rather than coerced.
    """
    assert JobSpec.model_validate(_JOB).timeout_seconds is None
    assert JobSpec.model_validate({**_JOB, "timeout_seconds": 900}).timeout_seconds == 900.0
    for bad in (0, -1, "soon"):
        with pytest.raises(ValidationError):
            JobSpec.model_validate({**_JOB, "timeout_seconds": bad})


def test_a_job_cannot_both_wait_on_a_person_and_declare_what_it_costs() -> None:
    """A job cannot both wait on a person and declare what it costs.

    `timeout_seconds` states the run's cost; `awaits_answer` says it has no meaningful wall-clock
    bound. Honouring both would restore a compute ceiling over a person's wait.
    """
    assert JobSpec.model_validate(_JOB).awaits_answer is False
    assert JobSpec.model_validate({**_JOB, "awaits_answer": True}).awaits_answer is True
    with pytest.raises(ValidationError, match="no wall-clock ceiling to declare"):
        JobSpec.model_validate({**_JOB, "awaits_answer": True, "timeout_seconds": 900})


def test_a_manifest_cannot_take_the_operators_ceiling_off_its_own_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manifest cannot take the operator's ceiling off its own job.

    `awaits_answer` removes the ceiling, so claiming it requires the operator's allowlist
    (`require_funded_ceiling`). The check is in the shared pre-flight `prepare_job_launch`, which
    every launcher (generated tools and template job steps) goes through, so building a tool refuses
    nothing and a misconfiguration affects only the job being launched.
    """
    from chemclaw.connectors.jobs import (
        ConnectorJobError,
        prepare_job_launch,
        require_funded_ceiling,
    )

    job = JobSpec.model_validate({**_JOB, "awaits_answer": True})
    with pytest.raises(ConnectorJobError, match="because a manifest is data"):
        require_funded_ceiling("acme", job)
    # Through the pre-flight, which is what both launchers actually call.
    with pytest.raises(ConnectorJobError, match="because a manifest is data"):
        prepare_job_launch("acme", job, {})

    # The shipped bundle keeps the shape, which is what makes this a gate and not a removal.
    monkeypatch.setattr(settings, "connector_jobs_awaiting_answer", f"acme.{job.name}")
    require_funded_ceiling("acme", job)

    # And an ordinary job is untouched by the gate whatever it is set to.
    monkeypatch.setattr(settings, "connector_jobs_awaiting_answer", "")
    require_funded_ceiling("acme", JobSpec.model_validate(_JOB))

    # Building a tool is not where this is decided any more: a mis-set allowlist must not refuse
    # the launchers of every *other* bundle, which is what `job_tools()` rebuilding through a
    # raising `build_job_tool` did — reported to the chemist as `bad_tool_arguments`, per turn.
    from chemclaw.connectors.jobs import build_job_tool

    assert build_job_tool("acme", job) is not None


def test_a_bad_ceiling_in_a_real_manifest_names_the_file_it_is_in(tmp_path: Path) -> None:
    """A bad ceiling in a real manifest names the file it is in.

    Bundles are discovered by existing on `connectors_dir`, so `registry._load_manifest` wraps each
    validation failure with the file it read.
    """
    from chemclaw.connectors.registry import ConnectorError, _load_manifest

    bundle = tmp_path / "thing"
    bundle.mkdir()
    (bundle / "connector.yaml").write_text(
        "name: thing\n"
        "description: does a thing\n"
        "jobs:\n"
        "  - name: run_thing\n"
        "    workflow: ThingWorkflow\n"
        "    summary: Run the thing.\n"
        "    timeout_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConnectorError, match=r"connector\.yaml: invalid manifest"):
        _load_manifest(bundle)
