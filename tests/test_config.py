"""Behavioral tests for the single config source (plan step 0.3, gate G3).

These prove the two contracts the rest of the system relies on: sane defaults
load with no `.env`, and any value is overridable via a prefixed env var.
"""

import os
from collections.abc import Iterator

import pytest

from chemclaw.config import Settings


def test_defaults_load_without_env() -> None:
    """A fresh checkout with no `.env` yields the documented dev defaults."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.temporal_address == "localhost:7233"
    assert settings.hpc_task_queue == "hpc-jobs"
    assert settings.background_task_queue == "background-jobs"
    assert settings.postgres_dsn.startswith("postgresql://")


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `CHEMCLAW_`-prefixed env var overrides the field it maps to."""
    monkeypatch.setenv("CHEMCLAW_TEMPORAL_ADDRESS", "temporal.internal:7233")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.temporal_address == "temporal.internal:7233"


def test_unknown_field_is_rejected() -> None:
    """`extra="forbid"` turns a typo'd setting into a startup error, not a silent no-op."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, unknown_setting="x")  # type: ignore[call-arg]


def test_skills_dirs_splits_the_path_list() -> None:
    """`skills_dirs` splits `skills_dir` on the OS path separator (like PATH), dropping empties."""
    single = Settings(_env_file=None)  # type: ignore[call-arg]
    assert single.skills_dirs == ["skills"]  # the default is one directory

    multi = Settings(_env_file=None, skills_dir=os.pathsep.join(["skills", "/opt/team"]))  # type: ignore[call-arg]
    assert multi.skills_dirs == ["skills", "/opt/team"]

    # A trailing separator (an easy admin typo) yields no empty entry.
    trailing = Settings(_env_file=None, skills_dir="skills" + os.pathsep)  # type: ignore[call-arg]
    assert trailing.skills_dirs == ["skills"]


def test_llm_provider_defaults_to_anthropic() -> None:
    """The default provider is the dev path, so the config singleton is valid with no endpoint."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "anthropic"
    assert settings.llm_temperature == 0.0
    assert settings.llm_max_tokens == 4096


def test_parity_defaults_are_backward_compatible() -> None:
    """F10 additions default to today's behavior: no model routing, allow-all tool authz."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_routes == {}  # single-model behavior
    assert settings.tool_role_gates == {}  # nothing gated
    assert settings.tool_authz_default == "allow"  # every tool callable by default
    assert settings.verifier_enabled is False  # deterministic citation gate, no LLM judge
    assert settings.verifier_confidence_threshold == 0.7
    assert settings.eval_drift_enabled is False  # no scheduled drift job until opted in
    assert settings.eval_drift_epsilon == 0.05  # relative band: 5% proportional move
    assert settings.eval_drift_timeout_seconds == 300.0
    assert settings.orchestrator_max_parallel_children == 8  # bounded child fan-out


def test_hybrid_retrieval_defaults_are_backward_compatible() -> None:
    """F10-A retrieval defaults keep today's behavior: hash embedder, graph (flat) mode."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.embedding_provider == "hash"
    assert settings.retrieval_mode == "graph"  # flat union, not hybrid fusion, by default
    assert settings.embedding_dim == 1536  # matches note_index.embedding vector(1536)
    assert "vector" not in settings.data_source_list  # new retrievers off until opted in


def test_parity_json_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dict-typed F10 knobs parse their JSON env overrides."""
    monkeypatch.setenv("CHEMCLAW_MODEL_ROUTES", '{"verifier": "small"}')
    monkeypatch.setenv("CHEMCLAW_TOOL_ROLE_GATES", '{"submit_qm_job": ["chemist"]}')
    monkeypatch.setenv("CHEMCLAW_TOOL_AUTHZ_DEFAULT", "deny")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_routes == {"verifier": "small"}
    assert settings.tool_role_gates == {"submit_qm_job": ["chemist"]}
    assert settings.tool_authz_default == "deny"


def test_openai_compatible_requires_endpoint_and_model() -> None:
    """Selecting the internal provider without a base_url/model fails at startup, clearly."""
    with pytest.raises(ValueError, match="llm_base_url"):
        Settings(_env_file=None, llm_provider="openai_compatible")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="llm_model"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            llm_provider="openai_compatible",
            llm_base_url="https://llm.internal/v1",
        )


def test_llm_base_url_overrides_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The internal endpoint is a `CHEMCLAW_`-prefixed env var, like every other setting."""
    monkeypatch.setenv("CHEMCLAW_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("CHEMCLAW_LLM_BASE_URL", "https://llm.internal/v1")
    monkeypatch.setenv("CHEMCLAW_LLM_MODEL", "internal-model")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_base_url == "https://llm.internal/v1"
    assert settings.llm_model == "internal-model"


def test_entra_defaults_and_derived_endpoints() -> None:
    """Entra is off by default; JWKS/issuer derive from the tenant unless explicitly overridden."""
    settings = Settings(_env_file=None, entra_tenant_id="tid-1")  # type: ignore[call-arg]
    assert settings.entra_required is False
    assert settings.entra_jwks_endpoint.endswith("/tid-1/discovery/v2.0/keys")
    assert settings.entra_issuer_url.endswith("/tid-1/v2.0")
    override = Settings(
        _env_file=None, entra_jwks_url="https://x/keys", entra_issuer="https://x/v2"
    )  # type: ignore[call-arg]
    assert override.entra_jwks_endpoint == "https://x/keys"
    assert override.entra_issuer_url == "https://x/v2"


def test_entra_authorization_sets_parse() -> None:
    """Expensive-action and privileged-role config parse from comma lists to sets."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        entra_expensive_actions="submit_qm_job, start_bo_campaign",
        entra_privileged_roles="compute,admin",
    )
    assert settings.entra_expensive_action_set == frozenset({"submit_qm_job", "start_bo_campaign"})
    assert settings.entra_privileged_role_set == frozenset({"compute", "admin"})


def test_session_store_defaults_to_memory() -> None:
    """The durable session store is opt-in; the default keeps the in-process provider."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.session_store == "memory"
    assert settings.session_store_dsn == ""


def test_service_defaults() -> None:
    """The front-door service binds a sane default port and no CORS origins (safe default)."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.service_port == 8080
    assert settings.service_cors_origins == ""


def test_hpc_and_deploy_defaults() -> None:
    """F5/F6 keep dev defaults: mock HPC backend, empty pipeline version, no OTLP endpoint."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.hpc_launch_interface == "mock"
    assert settings.hpc_pipeline_version == ""
    assert settings.otel_endpoint == ""
    assert settings.hpc_bridge_identity == "chemclaw-hpc"


def test_hpc_launch_interface_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real backend is selected by one `CHEMCLAW_`-prefixed env var, like every setting."""
    monkeypatch.setenv("CHEMCLAW_HPC_LAUNCH_INTERFACE", "nextflow")
    assert Settings(_env_file=None).hpc_launch_interface == "nextflow"  # type: ignore[call-arg]


def test_entra_required_needs_audience_and_issuer() -> None:
    """Under enforcement, an empty audience (deny-all) or no tenant/issuer fails at startup."""
    with pytest.raises(ValueError, match="entra_audience"):
        Settings(_env_file=None, entra_required=True)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="tenant_id or entra_issuer"):
        Settings(_env_file=None, entra_required=True, entra_audience="api://x")  # type: ignore[call-arg]


def test_entra_role_gate_must_be_configured_symmetrically() -> None:
    """Declaring expensive actions without privileged roles (or vice versa) leaves the gate open."""
    with pytest.raises(ValueError, match="must be set together"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            entra_required=True,
            entra_audience="api://x",
            entra_tenant_id="t",
            entra_expensive_actions="submit_qm_job",  # roles missing → gate silently open
        )


def test_entra_required_full_config_is_accepted() -> None:
    """A complete enforcement config (audience + issuer + paired roles/actions) constructs fine."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        entra_required=True,
        entra_audience="api://x",
        entra_tenant_id="t",
        entra_expensive_actions="submit_qm_job",
        entra_privileged_roles="compute",
    )
    assert settings.entra_required is True


def test_temporal_mtls_cert_and_key_must_pair() -> None:
    """A Temporal client cert without its key (or vice versa) is a half-config, rejected early."""
    with pytest.raises(ValueError, match="temporal_tls_cert and temporal_tls_key"):
        Settings(_env_file=None, temporal_tls_cert="/c.pem")  # type: ignore[call-arg]


def test_absolute_knowledge_dir_is_rejected() -> None:
    """An absolute `knowledge_dir` fails at startup (it would escape the note repo)."""
    with pytest.raises(ValueError, match="knowledge_dir must be relative"):
        Settings(_env_file=None, knowledge_dir="/etc/knowledge")  # type: ignore[call-arg]


def test_relative_knowledge_dir_is_accepted() -> None:
    """A relative `knowledge_dir` (the default kind) loads fine."""
    assert Settings(_env_file=None, knowledge_dir="knowledge").knowledge_dir == "knowledge"  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clear_prefixed_env() -> Iterator[None]:
    """Isolate each test from any CHEMCLAW_* vars present in the ambient shell."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("CHEMCLAW_")}
    for key in saved:
        del os.environ[key]
    yield
    os.environ.update(saved)
