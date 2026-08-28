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
    monkeypatch.setenv("CHEMCLAW_TOOL_ROLE_GATES", '{"sample_conformers": ["chemist"]}')
    monkeypatch.setenv("CHEMCLAW_TOOL_AUTHZ_DEFAULT", "deny")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_routes == {"verifier": "small"}
    assert settings.tool_role_gates == {"sample_conformers": ["chemist"]}
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
        entra_expensive_actions="sample_conformers, start_bo_campaign",
        entra_privileged_roles="compute,admin",
    )
    assert settings.entra_expensive_action_set == frozenset(
        {"sample_conformers", "start_bo_campaign"}
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


def test_deploy_defaults() -> None:
    """F6 keeps its dev default: no OTLP endpoint until a deployment names a collector."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.otel_endpoint == ""


def test_the_connector_job_ceiling_stays_above_the_activity_it_bounds() -> None:
    """A parent ceiling no larger than its child's longest activity is not a ceiling.

    `ConnectorJobWorkflow` gives its child `connector_job_timeout_seconds` as an execution timeout,
    and `CalcJobWorkflow` inside it gives `run_xtb_calculation` — a CREST search —
    `xtb_job_timeout_seconds` as a single-attempt `start_to_close`. Set them equal and one attempt
    consumes the entire parent budget: the activity's five-attempt retry policy can never reach a
    second attempt, and the run dies with a bare `WorkflowExecutionTimedOut` naming neither
    setting. This is the rule that used to guard the DFT poll's 24 h budget; the tier is gone
    (`D-2026-08-26-semiempirical-is-the-whole-tier`) and the rule is not.
    """
    with pytest.raises(ValueError, match="connector_job_timeout_seconds"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, connector_job_timeout_seconds=14_400.0, xtb_job_timeout_seconds=14_400
        )


def test_raising_only_the_activity_budget_is_refused_rather_than_silently_ignored() -> None:
    """The operator error the two comments invited, made loud.

    An operator whose CREST search on a large molecule needs eight hours raises
    `xtb_job_timeout_seconds` — and gets no behaviour change at all, because the parent's ceiling
    fires first. Failing at startup with both numbers named is the difference between a two-minute
    fix and an unexplained timeout hours into a search.
    """
    with pytest.raises(ValueError, match="raising the activity budget alone changes nothing"):
        Settings(_env_file=None, xtb_job_timeout_seconds=28_800)  # type: ignore[call-arg]

    # And the fix the message asks for actually works — a guard that cannot be satisfied is a wall.
    assert (
        Settings(  # type: ignore[call-arg]
            _env_file=None, xtb_job_timeout_seconds=28_800, connector_job_timeout_seconds=32_400.0
        ).connector_job_timeout_seconds
        == 32_400.0
    )


def test_the_activity_budget_stays_above_the_search_it_awaits() -> None:
    """The same equality-is-the-defect rule one level down: activity vs the sampler's client bound.

    The two shipped equal (14400 s each), so a sampling call that ran to its client bound
    exhausted the activity's `start_to_close` at the same instant — the activity died as a bare
    timeout instead of surfacing the sampler's error, and `activity_max_attempts` could never be
    reached. Refused at startup with both numbers named, exactly as the parent ceiling above.
    """
    with pytest.raises(ValueError, match="xtb_job_timeout_seconds"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            xtb_job_timeout_seconds=14_400,
            calc_sampling_timeout_seconds=14_400.0,
            connector_job_timeout_seconds=18_000.0,
        )


def test_the_shipped_defaults_boot() -> None:
    """Enforcing a rule the repository's own shipped configuration violates is a crash loop.

    So the ceiling's default carries the headroom rather than asking every deployment to discover
    it. The number is derived from `xtb_job_timeout_seconds`, which is why this asserts the
    relation rather than a literal.
    """
    default = Settings(_env_file=None)  # type: ignore[call-arg]
    assert default.connector_job_timeout_seconds > default.xtb_job_timeout_seconds
    assert default.xtb_job_timeout_seconds > (
        default.calc_sampling_timeout_seconds + default.activity_timeout_seconds
    )


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
        # Enforcing identity now requires the plan gate with it, or an explicit acceptance —
        # see `test_enforcing_identity_without_the_plan_gate_is_refused`.
        harness_enabled=True,
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
            entra_expensive_actions="sample_conformers",  # no role can pass the gate
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
        entra_privileged_roles="calc-operator",
        harness_enabled=True,
    )
    assert settings.entra_privileged_role_set == {"calc-operator"}
    assert settings.entra_expensive_action_set == frozenset()


def test_entra_required_full_config_is_accepted() -> None:
    """A complete enforcement config (audience + issuer + paired roles/actions) constructs fine."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        entra_required=True,
        entra_audience="api://x",
        entra_tenant_id="t",
        entra_expensive_actions="sample_conformers",
        entra_privileged_roles="compute",
        harness_enabled=True,
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


# The minimum a deployment must state to be in the enforced posture at all — `entra_required`
# alone is refused for a *different* reason (no audience, no issuer). `Any` because these are
# splatted into `Settings(**...)`, whose fields have twenty different types.
_ENFORCED: dict[str, Any] = {
    "entra_required": True,
    "entra_audience": "api://chemclaw",
    "entra_tenant_id": "00000000-0000-0000-0000-000000000000",
}


def test_enforcing_identity_without_the_plan_gate_is_refused() -> None:
    """The two settings that decide "is a turn supervised" were never checked against each other.

    `plan_gate.gate_applies` is `harness_enabled_for(profile) and autonomy_for(profile) ==
    "plan_only"`. `harness_autonomy` defaults to `plan_only`, but `harness_enabled` defaults to
    **False** — so the gate D-167/DARK-1 exists for is attached only where an operator turned the
    harness on. The shipped chart does (`CHEMCLAW_HARNESS_ENABLED: "true"`); the image run
    directly, `docker compose`, the live lane and any non-Helm deployment do not.

    A deployment that sets `CHEMCLAW_ENTRA_REQUIRED=true` believes it is in the enforced posture.
    Without the chart's ConfigMap it gets an agent with no plan approval at all, and an ordinary
    authenticated user holding no app roles can then run a turn that autonomously starts
    state-changing work with nothing to approve it — measured, `report_measurement` (which writes
    the calibration ledger), `compute_xtb_energy`, `watch_for` and `remember_preference` are all
    allowed by both `authorize_tool` and `authorize_trigger`.

    `_refuse_unauthenticated_exposure` already makes exactly this argument for the analogous
    "the safe posture is one env var away" pair.
    """
    with pytest.raises(ValueError, match="harness_enabled"):
        Settings(_env_file=None, **_ENFORCED)  # type: ignore[call-arg]


def test_the_enforced_posture_with_the_gate_attached_constructs() -> None:
    """The control: what the shipped chart sets must still boot."""
    assert Settings(_env_file=None, harness_enabled=True, **_ENFORCED) is not None  # type: ignore[call-arg]


def test_the_opt_out_is_stated_in_the_same_vocabulary_as_the_thing_it_declines() -> None:
    """A deployment that means "unsupervised" says so with `harness_autonomy`, not a security knob.

    `service_allow_insecure` was the obvious escape hatch and is the wrong one: it means "boot
    unauthenticated on a non-loopback bind", its own comment ends "Entra-enforced deployments never
    need it", and a knob that means two things is one that gets set for the wrong reason.

    `harness_autonomy=execute` is the right one because it is the *statement* this refusal is
    asking for. With the harness off it changes no behaviour at all — `autonomy_for` is read
    nowhere but `gate_applies`, which is already False — so it buys nothing except making the
    posture visible in `helm show values` and in a values diff, which is the whole point.

    A per-profile `harness_autonomy` still wins over it (`autonomy_for` prefers the profile), so
    the opt-out cannot silently disarm a profile that narrowed on purpose.
    """
    relaxed = Settings(_env_file=None, harness_autonomy="execute", **_ENFORCED)  # type: ignore[call-arg]
    assert relaxed.entra_required and not relaxed.harness_enabled
    assert (
        Settings(  # type: ignore[call-arg]
            _env_file=None, harness_enabled=True, harness_autonomy="execute", **_ENFORCED
        )
        is not None
    )


def test_a_wildcard_cors_origin_is_refused() -> None:
    """`*` is the one value this allow-list may not hold, and nothing checked it.

    `_add_cors` splits the field on commas and hands the result to `CORSMiddleware` verbatim, so
    `CHEMCLAW_SERVICE_CORS_ORIGINS=*` became `allow_origins=["*"]`. The blast radius is bounded —
    `allow_credentials` is left False and the API authenticates with a bearer rather than a cookie,
    so a hostile origin cannot ride a user's session — and "bounded" is not "intended": the knob's
    own comment calls the empty default "the safe default" without ever saying which values are the
    unsafe ones, and the bound depends on two properties of *other* code that nothing pins.

    Refused rather than opted out of, because there is no deployment that needs it: an empty value
    already means "no cross-origin access", a same-origin embedded UI needs none, and a browser
    client that does need access has an origin to name.
    """
    with pytest.raises(ValueError, match="service_cors_origins"):
        Settings(_env_file=None, service_cors_origins="*")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="service_cors_origins"):
        Settings(_env_file=None, service_cors_origins="https://ui.example, *")  # type: ignore[call-arg]


def test_a_named_cors_origin_is_still_accepted() -> None:
    """The control: an allow-list of real origins is what the field is for."""
    named = Settings(  # type: ignore[call-arg]
        _env_file=None, service_cors_origins="https://ui.example, https://alt.example"
    )
    assert named.service_cors_origins.startswith("https://ui.example")


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


def test_the_calculation_backend_budget_is_undeclared_by_default() -> None:
    """0 means "no opinion", not "a ceiling of zero" — the same split the other two budgets take.

    Sharper here than for either of them: the number belongs to a pod in *another* release
    (`Chemclaw3-mcp` `servers/calc`), so a code default other than "undeclared" would be this
    repository guessing at somebody else's CPU allocation.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, calc_fleet_worker_processes=8, worker_max_concurrent_activities=16
    )
    assert settings.calc_backend_max_concurrent_requests == 0


def test_a_calc_fleet_exactly_at_its_backend_ceiling_is_allowed() -> None:
    """`>`, not `>=`: a deployment sized exactly to what the backend serves is the correct one."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        calc_fleet_worker_processes=2,
        worker_max_concurrent_activities=8,
        calc_backend_max_concurrent_requests=16,
    )
    assert settings.calc_backend_max_concurrent_requests == 16


def test_a_release_with_no_calc_worker_dispatches_nothing_durably() -> None:
    """0 worker processes is legal, and it must not be floored to 1.

    The chart renders 0 whenever `connectors.calc.worker` is off, and a floor there would refuse a
    deployment over calculations it never makes.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        calc_fleet_worker_processes=0,
        worker_max_concurrent_activities=64,
        calc_backend_max_concurrent_requests=1,
    )
    assert settings.calc_fleet_worker_processes == 0


def test_the_calculation_backend_ceiling_error_names_both_sides_and_every_factor() -> None:
    """Scaling the calc worker is the lever that trips this, so the message has to name it.

    The per-process cap is the only setting whose name contains `concurrent`, and it is exactly the
    number that is *not* the whole story: `servers/calc` is one shared pod, so what it is offered is
    that cap times the worker replica count — the product nobody had computed (BS-07).
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            calc_fleet_worker_processes=4,
            worker_max_concurrent_activities=8,
            calc_backend_max_concurrent_requests=16,
        )
    message = str(excinfo.value)
    assert "32" in message and "16" in message
    assert "4 calc worker process" in message and "8 activities each" in message
    assert "worker_max_concurrent_activities" in message
    assert "calc_backend_max_concurrent_requests" in message


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


def test_no_calculator_setting_is_declared_without_a_reader() -> None:
    """A calculator knob nobody reads is not tidiness — it is a control that does not exist.

    When the physics moved to `Chemclaw3-mcp` (`D-2026-08-16-the-physics-leaves-the-cache-stays`),
    twenty-four fields stayed declared here: the binaries, the optimizer's convergence thresholds,
    the Hessian displacement and atom ceiling, the CREST budget, and both pKa calibration pairs.
    Every one of them still had a `.env.example` row, so the parity test above was green — the
    parity that was broken is the one nothing checked.

    What makes it a defect rather than clutter is that the server reads the *same* names under the
    *same* `CHEMCLAW_` prefix. An operator who set `CHEMCLAW_XTB_OPT_MAX_STEPS` on this deployment
    got no error, no warning and no effect, while the identically-spelled setting on the calculation
    server was the one that actually decided the calculation.

    Scoped to this one section deliberately. Elsewhere a field with no first-party reader can be
    legitimate — something a library or a chart consumes. Here it cannot: this section exists to
    configure code in this repository, and after the split that code is orchestration.
    """
    import ast

    section = Path(__file__).resolve().parent.parent / "src/chemclaw/core/config/calculators.py"
    declared = [
        node.target.id
        for node in ast.walk(ast.parse(section.read_text(encoding="utf-8")))
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert declared, "the section parsed to no fields at all, so this test proves nothing"

    src = Path(__file__).resolve().parent.parent / "src"
    sources = [
        path.read_text(encoding="utf-8")
        for path in src.rglob("*.py")
        if "core/config/" not in path.as_posix()
    ]
    unread = sorted(name for name in declared if not any(name in source for source in sources))

    assert not unread, (
        "declared in `calculators.py` and read nowhere in `src/`: "
        + ", ".join(unread)
        + ". The calculation server reads these same names under the same env prefix, so a "
        "field left here is a knob an operator can set on the wrong deployment and watch do "
        "nothing. Delete it here, or give it a reader."
    )


def test_note_reindex_is_derived_from_the_source_list_unless_overridden() -> None:
    """Enabling an index-backed leg must enable the reindex that builds what it queries.

    As an independent switch defaulting to off, `vector`/`lexical` could be enabled with the
    index never built — both legs then reported `chunks: 0, failed: false` forever, and the
    deployment believed it ran hybrid retrieval. The same derivation move
    `D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` made for connectors.
    """
    derived_on = Settings(_env_file=None, data_sources="graph,vector,lexical")  # type: ignore[call-arg]
    assert derived_on.note_reindex_effective is True
    derived_off = Settings(_env_file=None, data_sources="graph")  # type: ignore[call-arg]
    assert derived_off.note_reindex_effective is False
    # An explicit choice still wins in both directions.
    opted_out = Settings(  # type: ignore[call-arg]
        _env_file=None, data_sources="graph,vector,lexical", note_reindex_enabled=False
    )
    assert opted_out.note_reindex_effective is False
    forced = Settings(  # type: ignore[call-arg]
        _env_file=None, data_sources="graph", note_reindex_enabled=True
    )
    assert forced.note_reindex_effective is True


def test_the_connector_job_ceiling_covers_every_activity_a_bundle_child_can_run() -> None:
    """The ceiling rule has to name the longest activity, not the one the author remembered.

    `_the_job_ceiling_covers_the_activity_it_bounds` knew only about `xtb_job_timeout_seconds`, so
    the `results` bundle's republish walk — the other multi-hour activity a connector job can
    start — sailed past it: it was given `connector_job_timeout_seconds` *itself* as a
    start-to-close, which the parent ceiling equals rather than exceeds. Both then expire within
    milliseconds of each other, the activity's five-attempt retry policy is unreachable, and the
    run dies as a bare `WorkflowExecutionTimedOut` naming neither setting. The guard is written
    against the max over the budgets so the next long activity is covered by construction.
    """
    with pytest.raises(ValueError, match="connector_job_timeout_seconds"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            connector_job_timeout_seconds=18_000.0,
            result_republish_timeout_seconds=18_000.0,
        )


def test_the_shipped_republish_budget_is_strictly_inside_the_job_ceiling() -> None:
    """The relation the defect violated, asserted on the numbers that actually ship."""
    default = Settings(_env_file=None)  # type: ignore[call-arg]
    assert default.connector_job_timeout_seconds > (
        default.result_republish_timeout_seconds + default.activity_timeout_seconds
    )


def test_a_heartbeat_timeout_must_fit_inside_the_budget_it_reports_within() -> None:
    """The relation `background_activity_heartbeat_timeout_seconds`'s comment asserted as fact.

    Two concrete misconfigurations, both silent, and one inequality covers them:

    - `CHEMCLAW_RESULT_PUBLISH_TIMEOUT_SECONDS=30` with one sink gives the drain a 30 s
      start-to-close under the shipped 60 s heartbeat timeout, so the heartbeat can never fire
      first and the dead-worker detection it exists for is inert.
    - A 3600 s heartbeat timeout makes `durable/heartbeat.py::beating` derive a 900 s beat
      interval, longer than the 600 s `retention_timeout_seconds` — so the sweep sends no beat at
      all before its own start-to-close expires, and fails on a timeout that has nothing to do
      with the sweep.
    """
    with pytest.raises(ValueError, match="background_activity_heartbeat_timeout_seconds"):
        Settings(_env_file=None, result_publish_timeout_seconds=30.0)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="background_activity_heartbeat_timeout_seconds"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, background_activity_heartbeat_timeout_seconds=3600.0
        )


def test_the_shipped_heartbeat_is_strictly_inside_every_budget_it_sits_under() -> None:
    """The relation the defect violated, asserted on the numbers that actually ship."""
    default = Settings(_env_file=None)  # type: ignore[call-arg]
    assert default.background_activity_heartbeat_timeout_seconds < min(
        default.retention_timeout_seconds,
        default.note_reindex_timeout_seconds,
        default.result_publish_timeout_seconds,
    )


def test_the_two_metrics_expositions_may_not_claim_one_port() -> None:
    """One process serves both, so equal ports is a bind failure rather than a duplicate scrape.

    Measured with the port already held: the SDK's `Runtime(...)` raised `ValueError: Failed
    starting Prometheus exporter: Address already in use` from inside `connect_options()`, and
    `connect()`'s `except Exception` reported it to every durable tool as "the durable execution
    backend (Temporal) is unreachable … This is an infrastructure outage". That half now degrades;
    a deployment stating a thing it cannot have should still hear about it at startup, naming both
    settings, rather than from a counter somebody has to think to look at.
    """
    with pytest.raises(ValueError, match="temporal_metrics_port"):
        Settings(_env_file=None, temporal_metrics_port=9000)  # type: ignore[call-arg]
    # 0 is "disabled" for both and is therefore not a collision, which is the shipped default.
    assert Settings(_env_file=None).temporal_metrics_port == 0  # type: ignore[call-arg]
    # And a different port on the same host is fine — the rule is about the collision, not about
    # the two settings coexisting.
    assert (
        Settings(_env_file=None, temporal_metrics_port=9001).temporal_metrics_port == 9001  # type: ignore[call-arg]
    )
