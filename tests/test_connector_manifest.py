"""The connector manifest is a contract, not a config file — every way to get it wrong fails loudly.

Replaces `test_mcp_server_spec.py`, whose subject (`McpServerSpec`) the connector seam absorbed.
The properties worth keeping are the same ones: the transport union dispatches, the agent-facing
allow-list is transport-independent, and a field that does not belong to the chosen variant is a
config error rather than a silent drop. The properties that are *new* are the manifest's own:
a bundle must contribute something reachable, a job must declare its arguments exactly one way,
and a name may not be claimed twice.

Pure validation — no subprocess and no network. One test at the end reaches the loader, and
says why: a number a bundle author got wrong is only actionable if the failure names the file it
is in, and that framing belongs to `registry._load_manifest` rather than to the model.
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
    """`auth: mode: none` is refused for a host that is reachable from the network.

    The rule `NoAuth`'s docstring claimed for a long time and nothing enforced. It only became
    reachable when a bundle could name somebody else's server (the chart's `connectors.<name>.url`),
    and the failure it prevents is the quiet one: an unauthenticated MCP call carrying the turn's
    actor and full role set to a host outside our trust boundary.

    Loopback stays free — every shipped bundle declares a loopback dev default and lets the
    deployment move it — and a bearer credential makes any host legal.
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
    """The rule above is about a *network* hop; a subprocess of our own pod has none.

    Worth pinning separately because the two variants share the `tools` surface and it would be
    easy to lift the check onto something they share — which would make the zero-infrastructure
    local path impossible to declare.
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
    """Every way of getting the classification wrong fails **open**, so none of them is tolerated.

    A misspelled `state_changing` entry matches no tool, leaves the real one ungated, and looks
    exactly like a correct manifest. So it is a load-time error — and the tool it was meant to name
    then shows up as unclassified, which is also an error (D-167).
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
    """The partition collapsed `tools` to a set, so a repeat validated here and failed later.

    `registry._declared_tool_names` walks the raw list, so `make connector-validate` and every
    agent build then reported the bundle colliding with *itself* — "connector 'x' declares tool 'a',
    which connector 'x' already provides as a tool", which is true and unactionable without reading
    the source to learn it means "you listed `a` twice". The manifest is the only place that can
    still see the repetition, so it is the place that names it.
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
    """Silence is not "read-only": a bundle has to say, because core cannot tell.

    Defaulting an omission to "read" would put the entire harness gate on a bundle author
    remembering, and defaulting it to "write" would gate every connector's lookups and make the
    approval-first posture unusable. Refusing to load is the only answer that cannot be wrong
    quietly, and it costs one line per tool, once.
    """
    with pytest.raises(ValidationError, match="does not say whether"):
        HttpEndpoint.model_validate(
            {"url": "http://127.0.0.1:8899/mcp", "tools": ["resolve_compound"]}
        )


def test_an_endpoint_that_declares_no_tools_refuses_to_load() -> None:
    """An empty `tools` list is the other way to be unclassified, and it used to be the quiet one.

    `_check_classification` partitions `tools` against `state_changing` and `read_only`, and a
    partition of nothing is trivially satisfied — so an endpoint that simply omitted `tools:`
    passed the very check written to make an omission loud. Both of its guarantees inverted at
    once: `registry` read the empty list as "no allow-list" and bound the server's entire
    advertised surface, and none of what arrived was in `state_changing_tool_names()`, so
    `side_effecting_call` answered `False` for every tool including a write — which is the input
    the plan gate (D-167) and the dry-run gate ask. The manifest that declared the least got the
    most, so the empty list is refused where the typo already was.
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
    """The new key's shape: absent by default, a positive number, and nothing else.

    Absent is the state of every manifest written before the field existed, and it must stay
    distinguishable from any declared number — `None` is what `child_execution_timeout` reads as
    "the deployment's ceiling, unchanged", so a zero or a negative silently coerced into it would
    turn a typo into a behaviour change nobody asked for. `gt=0` refuses both where a bundle author
    meets them, at load, rather than at the moment a child workflow is started with a ceiling of
    zero seconds.
    """
    assert JobSpec.model_validate(_JOB).timeout_seconds is None
    assert JobSpec.model_validate({**_JOB, "timeout_seconds": 900}).timeout_seconds == 900.0
    for bad in (0, -1, "soon"):
        with pytest.raises(ValidationError):
            JobSpec.model_validate({**_JOB, "timeout_seconds": bad})


def test_a_job_cannot_both_wait_on_a_person_and_declare_what_it_costs() -> None:
    """`awaits_answer` and `timeout_seconds` are opposite claims, so declaring both is refused.

    `timeout_seconds` says what this job's whole durable run costs; `awaits_answer` says the run
    has no wall-clock bound worth stating, because most of it is a person not having answered yet.
    Honouring both would rebuild the defect the field was added for — `_measure`'s fortnight-long
    wait under a ceiling sized for a CREST search — and honouring one silently would leave the
    other looking like a control it is not. Absent by default, because every job in the tree but
    one computes.
    """
    assert JobSpec.model_validate(_JOB).awaits_answer is False
    assert JobSpec.model_validate({**_JOB, "awaits_answer": True}).awaits_answer is True
    with pytest.raises(ValidationError, match="no wall-clock ceiling to declare"):
        JobSpec.model_validate({**_JOB, "awaits_answer": True, "timeout_seconds": 900})


def test_a_bad_ceiling_in_a_real_manifest_names_the_file_it_is_in(tmp_path: Path) -> None:
    """The one test here that reaches the loader, because the file name is the whole message.

    `JobSpec` raising a `ValidationError` is not by itself useful to whoever wrote the number: a
    bundle is discovered by existing on `connectors_dir`, so the report has to name *which*
    `connector.yaml` among however many are on that path. `registry._load_manifest` wraps every
    validation failure with the file it read, which is what makes a manifest problem a
    fail-closed startup error somebody can act on — asserted here on the field this module added,
    since a rule that is only checked in the abstract is a rule nobody can locate.
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
