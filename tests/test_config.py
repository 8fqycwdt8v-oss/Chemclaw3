"""Behavioral tests for the single config source (plan step 0.3, gate G3).

These prove the two contracts the rest of the system relies on: sane defaults
load with no `.env`, and any value is overridable via a prefixed env var.
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import Settings

# `CHEMCLAW_FOO=...`, optionally commented out (a documented-but-unset key, e.g. the JSON
# spec tokens). Both forms count as "documented" for the parity test below.
_ENV_KEY = re.compile(r"^#?\s*CHEMCLAW_([A-Z0-9_]+)=", re.MULTILINE)
_ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def _documented_keys() -> set[str]:
    """The lower-cased field names `.env.example` documents."""
    return {m.lower() for m in _ENV_KEY.findall(_ENV_EXAMPLE.read_text(encoding="utf-8"))}


def test_defaults_load_without_env() -> None:
    """A fresh checkout with no `.env` yields the documented dev defaults."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.temporal_address == "localhost:7233"
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
    # Unset, not 0.0: the default `agent_model` rejects an explicit temperature outright, so a
    # default of 0.0 made the shipped config fail every live turn with a 400.
    assert settings.llm_temperature is None
    assert settings.llm_max_tokens == 4096


def test_parity_defaults_are_backward_compatible() -> None:
    """F10 additions default to today's behavior: no model routing, allow-all tool authz."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_routes == {}  # single-model behavior
    assert settings.tool_role_gates == {}  # nothing gated
    assert settings.tool_authz_default == "allow"  # every tool callable by default
    assert settings.verifier_enabled is False  # deterministic citation gate, no LLM judge
    assert settings.verifier_confidence_threshold == 0.7
    # Far under service_turn_timeout_seconds (600): a stalled judge degrades, never holds the turn.
    assert settings.verifier_timeout_seconds == 30.0
    assert settings.verifier_timeout_seconds < settings.service_turn_timeout_seconds
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
    monkeypatch.setenv("CHEMCLAW_TOOL_ROLE_GATES", '{"compute_dft_energy": ["chemist"]}')
    monkeypatch.setenv("CHEMCLAW_TOOL_AUTHZ_DEFAULT", "deny")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_routes == {"verifier": "small"}
    assert settings.tool_role_gates == {"compute_dft_energy": ["chemist"]}
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
        entra_expensive_actions="compute_dft_energy, start_bo_campaign",
        entra_privileged_roles="compute,admin",
    )
    assert settings.entra_expensive_action_set == frozenset(
        {"compute_dft_energy", "start_bo_campaign"}
    )
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
    """The real backend is selected by `CHEMCLAW_`-prefixed env vars — and never unversioned.

    This test used to stop after the selection: it set the interface, the launcher URL, the
    pipeline name and the artifact store, left `hpc_pipeline_version` at its empty default, and
    asserted the settings object came out with `hpc_launch_interface == "nextflow"`. That pinned
    the defect. An empty version slugs to `unversioned` in the calculation key, so two different
    real pipelines would key their DFT energies identically and the second would be served the
    first's number.
    """
    monkeypatch.setenv("CHEMCLAW_HPC_LAUNCH_INTERFACE", "nextflow")
    monkeypatch.setenv("CHEMCLAW_HPC_API_BASE_URL", "https://tower.internal/api")
    monkeypatch.setenv("CHEMCLAW_HPC_PIPELINE_NAME", "qm-dft")
    monkeypatch.setenv("CHEMCLAW_HPC_ARTIFACT_STORE_URL", "https://artifacts.internal")
    with pytest.raises(ValueError, match="hpc_pipeline_version"):
        Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setenv("CHEMCLAW_HPC_PIPELINE_VERSION", "1.0.0")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.hpc_launch_interface == "nextflow"
    assert settings.hpc_pipeline_version == "1.0.0"


def test_nextflow_requires_launcher_endpoints() -> None:
    """Selecting the real backend without its endpoints fails at startup, naming the fields."""
    with pytest.raises(ValueError, match="hpc_api_base_url"):
        Settings(_env_file=None, hpc_launch_interface="nextflow")  # type: ignore[call-arg]
    with pytest.raises(
        ValueError, match="hpc_pipeline_name, hpc_pipeline_version, hpc_artifact_store_url"
    ):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            hpc_launch_interface="nextflow",
            hpc_api_base_url="https://tower.internal/api",
        )


def test_nextflow_poll_interval_must_beat_run_heartbeat() -> None:
    """The nextflow poll heartbeats against its own timeout — the pair is validated too."""
    with pytest.raises(ValueError, match="hpc_run_heartbeat_timeout_seconds"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            hpc_launch_interface="nextflow",
            hpc_api_base_url="https://tower.internal/api",
            hpc_pipeline_name="qm-dft",
            hpc_artifact_store_url="https://artifacts.internal",
            hpc_poll_interval_seconds=300.0,
            qm_poll_heartbeat_timeout_seconds=600.0,  # mock pair satisfied...
            # ...but hpc_run_heartbeat_timeout_seconds stays at its 120s default.
        )


def _nextflow(**overrides: Any) -> Settings:
    """A fully-configured `nextflow` deployment, so the guard under test is the one that fires."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        hpc_launch_interface="nextflow",
        hpc_api_base_url="https://tower.internal/api",
        hpc_pipeline_name="qm-dft",
        hpc_pipeline_version="1.0.0",
        hpc_artifact_store_url="https://artifacts.internal",
        **overrides,
    )


def test_the_connector_job_ceiling_stays_above_the_poll_budget_it_bounds() -> None:
    """A parent ceiling no larger than its child's longest activity is not a ceiling.

    `ConnectorJobWorkflow` gives its child `connector_job_timeout_seconds` as an execution timeout,
    and the QM workflow inside it gives the HPC poll `hpc_run_timeout_seconds` as a single-attempt
    `start_to_close`. They used to be equal at 86400, so on the `nextflow` path — the one the
    shipped chart selects — one poll attempt consumed the entire parent budget: the poll's
    five-attempt retry policy could never reach a second attempt, and the run died with a bare
    `WorkflowExecutionTimedOut` naming neither setting.
    """
    with pytest.raises(ValueError, match="connector_job_timeout_seconds"):
        _nextflow(connector_job_timeout_seconds=86_400.0, hpc_run_timeout_seconds=86_400.0)


def test_raising_only_the_poll_budget_is_refused_rather_than_silently_ignored() -> None:
    """The operator error the two comments invited, made loud.

    `hpc_run_timeout_seconds`'s own comment says it "must cover the whole run", so an operator with
    a 48h DFT job raises it — and gets no behaviour change at all, because the parent's ceiling
    fires first. Failing at startup with both numbers named is the difference between a two-minute
    fix and an unexplained timeout a day into a cluster run.
    """
    with pytest.raises(ValueError, match="raising the poll budget alone changes nothing"):
        _nextflow(hpc_run_timeout_seconds=172_800.0)

    # And the fix the message asks for actually works — a guard that cannot be satisfied is a wall.
    assert (
        _nextflow(
            hpc_run_timeout_seconds=172_800.0, connector_job_timeout_seconds=176_400.0
        ).connector_job_timeout_seconds
        == 176_400.0
    )


def test_the_shipped_defaults_boot_on_the_backend_the_chart_selects() -> None:
    """`deploy/helm/chemclaw/values.yaml` sets `nextflow`, so the defaults must satisfy the guard.

    Enforcing a rule the repository's own shipped configuration violates would turn a latent
    mis-sizing into a crash loop, so the ceiling's default carries the headroom rather than asking
    every deployment to discover it.
    """
    assert _nextflow().connector_job_timeout_seconds > _nextflow().hpc_run_timeout_seconds


def test_the_mock_path_is_validated_against_the_budget_it_actually_uses() -> None:
    """The workflow branches on the backend, so the guard branches with it.

    Checking the mock deployment against `hpc_run_timeout_seconds` — a value it never reads —
    would reject a perfectly sound dev configuration; checking it against nothing would leave the
    default path unguarded. `hpc_mock_run_seconds + qm_activity_timeout_seconds` is what
    `connectors/qm/workflows.py` gives the poll when the interface is `mock`.
    """
    Settings(_env_file=None, connector_job_timeout_seconds=200.0)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="hpc_mock_run_seconds"):
        Settings(_env_file=None, connector_job_timeout_seconds=180.0)  # type: ignore[call-arg]


def test_openai_compatible_embeddings_require_endpoint_and_model() -> None:
    """The embedding provider reuses `llm_base_url`; selecting it half-configured fails early."""
    with pytest.raises(ValueError, match="llm_base_url"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            embedding_provider="openai_compatible",
            embedding_model="internal-embed",
        )
    with pytest.raises(ValueError, match="embedding_model"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            llm_provider="openai_compatible",
            llm_base_url="https://llm.internal/v1",
            llm_model="internal-model",
            embedding_provider="openai_compatible",
        )


def test_entra_required_needs_audience_and_issuer() -> None:
    """Under enforcement, an empty audience (deny-all) or no tenant/issuer fails at startup."""
    with pytest.raises(ValueError, match="entra_audience"):
        Settings(_env_file=None, entra_required=True)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="tenant_id or entra_issuer"):
        Settings(_env_file=None, entra_required=True, entra_audience="api://x")  # type: ignore[call-arg]


def test_entra_required_rejects_issuer_only_config() -> None:
    """An issuer alone cannot resolve the JWKS keys endpoint — reject the deny-all half-config."""
    with pytest.raises(ValueError, match="entra_jwks_url"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            entra_required=True,
            entra_audience="api://x",
            entra_issuer="https://login.microsoftonline.com/tid-1/v2.0",
        )


def test_entra_required_accepts_issuer_plus_jwks_url() -> None:
    """Explicit issuer + explicit JWKS URL is a complete config even without a tenant id."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        entra_required=True,
        entra_audience="api://x",
        entra_issuer="https://login.microsoftonline.com/tid-1/v2.0",
        entra_jwks_url="https://login.microsoftonline.com/tid-1/discovery/v2.0/keys",
    )
    assert settings.entra_required is True


def test_entra_expensive_actions_without_roles_is_rejected() -> None:
    """Naming a gated action with no privileged role refuses it to everyone — still an error."""
    with pytest.raises(ValueError, match="entra_expensive_actions needs entra_privileged_roles"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            entra_required=True,
            entra_audience="api://x",
            entra_tenant_id="t",
            entra_expensive_actions="compute_dft_energy",  # no role can pass the gate
        )


def test_entra_privileged_roles_without_actions_is_accepted() -> None:
    """Roles alone is the *documented remedy*, so it must construct — the other direction is not.

    `expensive: true` in a connector manifest derives into `authz.expensive_actions()`, so a
    deployment gates its declared jobs by naming roles and nothing else; `docs/guides/runbook.md`
    tells an operator to set exactly this. The validator used to demand the pair, which made the
    instructed remedy un-constructable unless the operator also hand-copied the job names the
    derivation removed.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        entra_required=True,
        entra_audience="api://x",
        entra_tenant_id="t",
        entra_privileged_roles="hpc-operator",
    )
    assert settings.entra_privileged_role_set == {"hpc-operator"}
    assert settings.entra_expensive_action_set == frozenset()


def test_entra_required_full_config_is_accepted() -> None:
    """A complete enforcement config (audience + issuer + paired roles/actions) constructs fine."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        entra_required=True,
        entra_audience="api://x",
        entra_tenant_id="t",
        entra_expensive_actions="compute_dft_energy",
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


def test_knowledge_path_joins_note_repo_dir_and_knowledge_dir() -> None:
    """`knowledge_path` is where notes actually live: readers must agree with the PR-gate.

    The PR-gate writes at `note_repo_dir/knowledge_dir/...`; a reader that resolved
    `knowledge_dir` alone (relative to the process CWD) would look at a different tree
    whenever `note_repo_dir` pointed elsewhere — the bug `knowledge_path` exists to close.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, note_repo_dir="/clones/kg", knowledge_dir="knowledge"
    )
    assert settings.knowledge_path == Path("/clones/kg/knowledge")


def test_knowledge_path_matches_todays_default_when_note_repo_dir_is_unset() -> None:
    """With the dev default (`note_repo_dir="."`), `knowledge_path` is unchanged from before."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.knowledge_path == Path(settings.knowledge_dir)


def test_env_example_documents_only_real_fields() -> None:
    """Every `CHEMCLAW_*` key in `.env.example` names a real `Settings` field.

    `Settings.model_config` sets `extra="forbid"`, so a stale key is not a cosmetic doc bug:
    `cp .env.example .env` (the README quickstart) makes `Settings()` raise at import time and
    every entry point dies. This test is the guard that keeps the documented onboarding path
    working.
    """
    unknown = _documented_keys() - set(Settings.model_fields)
    assert not unknown, f".env.example documents non-existent settings: {sorted(unknown)}"


def test_env_example_documents_every_field() -> None:
    """Every `Settings` field appears in `.env.example`.

    `docs/guides/runbook.md` and `docs/planning/implementation-plan.md` both promise "every field
    mirrored in `.env.example`" — an operator reads that file to learn what is tunable. An
    undocumented field is an invisible knob, so this makes the promise machine-checked rather than
    aspirational.
    """
    undocumented = set(Settings.model_fields) - _documented_keys()
    assert not undocumented, f"settings missing from .env.example: {sorted(undocumented)}"


def test_env_example_loads_as_a_real_env_file(tmp_path: Path) -> None:
    """`cp .env.example .env` boots — the end-to-end proof of the README quickstart.

    The two field-set tests above catch drift by name; this one catches anything that makes the
    file itself unloadable (a malformed value, a bad JSON spec token).
    """
    env = tmp_path / ".env"
    env.write_text(_ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    Settings(_env_file=env)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clear_prefixed_env() -> Iterator[None]:
    """Isolate each test from any CHEMCLAW_* vars present in the ambient shell."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("CHEMCLAW_")}
    for key in saved:
        del os.environ[key]
    yield
    os.environ.update(saved)


@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        # Each of these was already forbidden in a field comment and enforced by nothing, so a
        # deployment could set it and find out in production (REV-18, D-136).
        (
            "memory store cannot serve multiple workers",
            {"session_store": "memory", "service_uvicorn_workers": 4},
        ),
        (
            "a fleet cannot admit more turns than its declared ceiling",
            {
                "service_fleet_replicas": 6,
                "service_max_concurrent_turns": 16,
                "service_fleet_max_concurrent_turns": 48,
            },
        ),
        (
            "uvicorn workers multiply the fleet the same way replicas do",
            {
                "session_store": "postgres",
                "service_fleet_replicas": 6,
                "service_uvicorn_workers": 2,
                "service_max_concurrent_turns": 8,
                "service_fleet_max_concurrent_turns": 48,
            },
        ),
        (
            "a mid-turn resume cannot outlive its turn",
            {
                "mid_turn_resume_enabled": True,
                "mid_turn_resume_timeout_seconds": 900.0,
                "service_turn_timeout_seconds": 600.0,
            },
        ),
        (
            "budgets on with every cap unlimited guards nothing",
            {
                "budget_enabled": True,
                "budget_max_turns_per_session": 0,
                "budget_max_tokens_per_session": 0,
                "budget_max_turns_per_user": 0,
                "budget_max_tokens_per_user": 0,
            },
        ),
        (
            "embedding_dim must match the note_index vector column when vector search is on",
            {"embedding_dim": 768, "data_sources": "graph,vector"},
        ),
        # DARK-8: the check asked whether the *vector* source was on, while `reindex_notes` writes
        # the embedding column for every note-index-backed source. So these two configurations
        # passed validation and then failed every reindex on a pgvector dimension error, with
        # nothing pointing at the setting that caused it.
        (
            "a lexical-only deployment reaches the same vector column",
            {"embedding_dim": 768, "data_sources": "graph,lexical"},
        ),
        (
            "the scheduled reindex writes it with no retrieve source at all",
            {"embedding_dim": 768, "data_sources": "graph", "note_reindex_enabled": True},
        ),
    ],
)
def test_configurations_the_comments_forbid_are_rejected(name: str, overrides: dict) -> None:  # type: ignore[type-arg]
    """A rule worth writing in a comment is worth failing on at startup."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_the_shipped_defaults_still_construct() -> None:
    """The new guards must not reject the configuration the repository actually ships."""
    assert Settings(_env_file=None) is not None  # type: ignore[call-arg]


def test_an_undeclared_fleet_ceiling_checks_nothing() -> None:
    """0 must mean "no opinion", not "a ceiling of zero".

    The code default, because a CLI, a test and a single-pod dev run have no fleet to bound — the
    same split `budget_enabled` and the rate limiter already take. A guard that fired there is one
    people switch off everywhere, and the failure mode would be spectacular: every process refusing
    to start because it can admit eight turns and was allowed none.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, service_fleet_replicas=99, service_max_concurrent_turns=64
    )
    assert settings.service_fleet_max_concurrent_turns == 0


def test_a_fleet_exactly_at_its_ceiling_is_allowed() -> None:
    """The check is `>`, not `>=` — the shipped chart sits exactly on its own number.

    `values.yaml` declares 48 against 6 replicas × 1 worker × 8 turns, deliberately, so the ceiling
    ships as a statement of the current shape rather than as slack. Off by one here and the chart
    the repository ships would fail to boot.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        service_fleet_replicas=6,
        service_max_concurrent_turns=8,
        service_fleet_max_concurrent_turns=48,
    )
    assert settings.service_fleet_max_concurrent_turns == 48


def test_the_fleet_ceiling_error_names_both_sides_and_every_factor() -> None:
    """An operator has to be told which of three numbers to change, and what the product was.

    "Invalid configuration" would send them to the one setting whose name contains `concurrent`,
    which is exactly the per-process cap that is *not* the whole story — the point of the guard is
    that the ceiling is a product nobody had computed.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            service_fleet_replicas=6,
            service_max_concurrent_turns=16,
            service_fleet_max_concurrent_turns=48,
        )
    message = str(excinfo.value)
    assert "96" in message and "48" in message
    assert "6 replicas" in message and "16 per process" in message
    assert "service_fleet_max_concurrent_turns" in message


def test_the_connection_budget_is_undeclared_by_default() -> None:
    """A dev run, a CLI and a test have one process and no fleet, so there is nothing to bound.

    Same split the turn ceiling takes, and for the same reason: a guard that fires on a laptop is a
    guard people switch off in production too.
    """
    settings = Settings(_env_file=None, pg_pool_max_size=64)  # type: ignore[call-arg]
    assert settings.pg_fleet_max_connections == 0


def test_a_fleet_exactly_at_its_connection_ceiling_is_allowed() -> None:
    """`>`, not `>=` — the shipped chart sits exactly on its own number.

    `values.yaml` declares 136 against 17 pooled processes × a pool of 8, deliberately, so the
    ceiling ships as a statement of the current shape rather than as slack. Off by one here and
    every pod the chart renders refuses to start.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        pg_fleet_pooled_processes=17,
        pg_pool_max_size=8,
        pg_fleet_max_connections=136,
    )
    assert settings.pg_fleet_max_connections == 136


def test_the_connection_ceiling_error_names_both_sides_and_every_factor() -> None:
    """The product is the thing nobody had computed, so the message has to show it.

    `core/config/store.py` stated "keep it under the server's max_connections" in prose and nothing
    computed the left-hand side, so the shipped chart ran every pod on the default pool of 16 and
    the fleet's real ceiling was ~272 against the max_connections=100 D-119 measured against. An
    operator seeing this needs both numbers and both levers, not the name of one setting.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pooled_processes=17,
            pg_pool_max_size=16,
            pg_fleet_max_connections=136,
        )
    message = str(excinfo.value)
    assert "272" in message and "136" in message
    assert "17 pooled process" in message and "16 per pool" in message
    assert "pg_fleet_max_connections" in message and "pg_pool_max_size" in message


def test_the_embedding_width_check_still_leaves_the_standalone_embedder_alone() -> None:
    """Widening the scope must not make it unconditional.

    The embedder is used on its own — the hash embedder's unit tests pick a small dim and touch no
    database — so a deployment that cannot reach pgvector at all must still be free to choose any
    width. The question the check asks is "does anything here write `note_index`", not "is an
    embedding configured".
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, embedding_dim=768, data_sources="graph", note_reindex_enabled=False
    )
    assert settings.embedding_dim == 768
