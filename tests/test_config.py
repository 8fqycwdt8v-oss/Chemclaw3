"""Behavioral tests for the single config source (plan step 0.3, gate G3).

These prove the two contracts the rest of the system relies on: sane defaults
load with no `.env`, and any value is overridable via a prefixed env var.
"""

import ast
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from chemclaw.core.config import _BOUNDED_DRAINS, Settings

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


def test_the_default_gateway_is_the_mock_on_this_machine() -> None:
    """A fresh checkout is valid with no endpoint and no credential — and cannot leave the host.

    The default gateway is `cli/mock_llm` on loopback, so an unconfigured deployment is refused a
    connection rather than sending prompts outbound (`D-2026-09-04-a-gateway-is-the-only-provider`).
    Booting on it is refused by `core/llm_gateway.refuse_unconfigured_llm_gateway` unless the
    posture is stated. `test_no_provider_field_survives` holds that no provider field exists.
    """
    from chemclaw.cli.mock_llm import MOCK_BASE_URL

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_base_url == MOCK_BASE_URL
    assert settings.llm_model == "mock"
    # Unset, not 0.0: current frontier models reject an explicit temperature outright, so a
    # default of 0.0 made the shipped config fail every live turn with a 400.
    assert settings.llm_temperature is None
    assert settings.llm_max_tokens == 4096


def test_the_live_lane_derives_its_bundle_list_rather_than_naming_one() -> None:
    """The live lane derives its bundle list rather than naming one.

    With `CHEMCLAW_CONNECTORS_REQUIRED` true, a bundle enabled but not started refuses boot, so the
    lane starts what is enabled rather than a hand-kept list (and does not narrow the enabled set,
    which would drop the core-served bundles). The derivation must be an assignment, because bash
    does not propagate a command substitution's exit status in a `for` list under `set -e`; both
    directions are pinned so the lane fails loudly rather than starting half a fleet.
    """
    script = (
        Path(__file__).resolve().parent.parent / "infra" / "live" / "processes.sh"
    ).read_text()

    assert "fleet_bundle_names()" in script, "the lane must derive its fleet bundles"
    assert 'names="$(fleet_bundle_names "$python")" || die' in script, (
        "start_fleet_bundles must iterate the derived names rather than a list written here — "
        "and must check the derivation, which only an assignment can do"
    )
    assert "for name in $names; do" in script, (
        "the loop must run over the checked names, not re-derive them"
    )
    assert 'for name in $(fleet_bundle_names "$python"); do' not in script, (
        "the substitution must not be iterated directly: `for x in $(cmd)` does not propagate a "
        "non-zero exit under `set -e`, so a crash inside the derivation leaves the loop running "
        "over partial output — the same silent partial list the derivation replaced"
    )
    assert "export CHEMCLAW_CONNECTORS_ENABLED" not in script, (
        "the lane must not pin its enabled set: narrowing it drops the core-served bundles the "
        "dev connector process provides, which is a second way to break the same boot"
    )


def test_the_live_lane_does_not_transcribe_the_gateway_address_into_shell() -> None:
    """`infra/live/processes.sh` asks this config for the gateway, never carries a copy of it.

    The lane decides whether to start `cli/mock_llm` by comparing the resolved `llm_base_url` with
    `MOCK_BASE_URL`, both read from the interpreter it launches. A shell copy cannot follow the
    Python default. `.env.example` is excluded: mirroring every setting is its purpose and nothing
    branches on it.
    """
    from chemclaw.cli.mock_llm import MOCK_BASE_URL

    script = Path(__file__).resolve().parent.parent / "infra" / "live" / "processes.sh"
    text = script.read_text(encoding="utf-8")
    for address in {MOCK_BASE_URL, Settings(_env_file=None).llm_base_url}:  # type: ignore[call-arg]
        assert address not in text, (
            f"{script.name} writes out {address!r}, which this config also defines. Read it from "
            "`Settings`/`cli.mock_llm` instead — a shell copy of a Python default is exactly how "
            "the lane came to start a front door pointed at a mock it did not start."
        )


def test_no_provider_field_survives() -> None:
    """The provider concept is gone, not narrowed to one value.

    A one-value enum would leave every reader in place and the next vendor one commit away;
    `agent_model` is gone with it.
    """
    assert "llm_provider" not in Settings.model_fields
    assert "llm_prompt_caching" not in Settings.model_fields
    assert "agent_model" not in Settings.model_fields


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


def test_a_blanked_gateway_address_is_refused() -> None:
    """A blanked gateway address is refused.

    An empty base URL is the SDK's hardcoded public host, not "no destination". Both fields default
    to the mock, so this fires only when a deployment blanks one.
    """
    with pytest.raises(ValueError, match="llm_base_url"):
        Settings(_env_file=None, llm_base_url="")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="llm_model"):
        Settings(_env_file=None, llm_model="")  # type: ignore[call-arg]


def test_llm_base_url_overrides_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The internal endpoint is a `CHEMCLAW_`-prefixed env var, like every other setting."""
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
    """The connector-job ceiling stays above the activity it bounds.

    `ConnectorJobWorkflow`'s child gets `connector_job_timeout_seconds`, and the CREST activity
    inside it gets `xtb_job_timeout_seconds` as a single-attempt `start_to_close`. Equal values let
    one attempt consume the whole parent budget, so retries are unreachable and the run dies as a
    bare `WorkflowExecutionTimedOut`.
    """
    with pytest.raises(ValueError, match="connector_job_timeout_seconds"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, connector_job_timeout_seconds=14_400.0, xtb_job_timeout_seconds=14_400
        )


def test_raising_only_the_activity_budget_is_refused_rather_than_silently_ignored() -> None:
    """Raising only the activity budget is refused rather than silently ignored.

    The parent ceiling would fire first; failing at startup with both numbers named saves an
    unexplained timeout hours into a search.
    """
    with pytest.raises(ValueError, match="raising the activity budget alone changes nothing"):
        Settings(_env_file=None, xtb_job_timeout_seconds=28_800)  # type: ignore[call-arg]

    # And the fix the message asks for actually works: a guard that cannot be satisfied is a wall.
    # It takes a third setting, because a template `job` step is bounded by
    # `connector_job_timeout_seconds` plus the wrapper's post-child steps, which the template run
    # ceiling must contain (`_the_template_run_ceiling_covers_one_step`). 52,530 is that bound (the
    # post-child steps' `schedule_to_start + start_to_close`) plus the eight-ordinary-step allowance
    # the default is sized by.
    assert (
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            xtb_job_timeout_seconds=28_800,
            connector_job_timeout_seconds=32_400.0,
            template_run_timeout_seconds=52_530.0,
        ).connector_job_timeout_seconds
        == 32_400.0
    )


def test_the_activity_budget_stays_above_the_search_it_awaits() -> None:
    """The activity budget stays above the sampler's client bound it awaits.

    Equal values let a sampling call that runs to its client bound exhaust the activity at the same
    instant, surfacing a bare timeout instead of the sampler's error.
    """
    with pytest.raises(ValueError, match="xtb_job_timeout_seconds"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            xtb_job_timeout_seconds=14_400,
            calc_sampling_timeout_seconds=14_400.0,
            connector_job_timeout_seconds=18_000.0,
        )


def test_the_shipped_defaults_boot() -> None:
    """The shipped defaults boot.

    The ceiling's default is derived from `xtb_job_timeout_seconds`, so the relation is asserted
    rather than a literal.
    """
    default = Settings(_env_file=None)  # type: ignore[call-arg]
    assert default.connector_job_timeout_seconds > default.xtb_job_timeout_seconds
    assert default.xtb_job_timeout_seconds > (
        default.calc_sampling_timeout_seconds + default.activity_timeout_seconds
    )


def test_openai_compatible_embeddings_require_a_model_name() -> None:
    """Selecting the endpoint embedder without naming its model fails at startup, in its own words.

    Matches the embedding validator's own message; an empty `llm_base_url` is refused earlier by
    `_gateway_is_addressed` (`test_a_blanked_gateway_address_is_refused`).
    """
    with pytest.raises(ValueError, match="requires embedding_model"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
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
    """Privileged roles without an action list are accepted.

    `expensive: true` in a manifest derives into `authz.expensive_actions()`, so naming roles alone
    is the runbook's documented remedy and must construct.
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
    """`knowledge_path` joins `note_repo_dir` and `knowledge_dir`, where notes are written.

    A reader resolving `knowledge_dir` alone (relative to the CWD) would read a different tree
    whenever `note_repo_dir` points elsewhere.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, note_repo_dir="/clones/kg", knowledge_dir="knowledge"
    )
    assert settings.knowledge_path == Path("/clones/kg/knowledge")


def test_knowledge_path_matches_todays_default_when_note_repo_dir_is_unset() -> None:
    """With the dev default (`note_repo_dir="."`), `knowledge_path` is unchanged from before."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.knowledge_path == Path(settings.knowledge_dir)


def test_a_log_level_that_logging_will_not_accept_is_refused_at_construction() -> None:
    """A log level that `logging` will not accept is refused at construction.

    Otherwise the process dies at `configure_logging()` with an error naming neither setting nor
    variable. Checked against `logging.getLevelNamesMapping()`, not a `Literal`, so stdlib aliases
    like `WARN` stay valid.
    """
    for accepted in ("debug", "INFO", "WARN", "FATAL", "NOTSET"):
        assert Settings(_env_file=None, log_level=accepted).log_level  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None, log_level="INFOO")  # type: ignore[call-arg]


def test_env_example_documents_only_real_fields() -> None:
    """Every `CHEMCLAW_*` key in `.env.example` names a real `Settings` field.

    `Settings` forbids extras, so a stale key makes `cp .env.example .env` (the README quickstart)
    fail every entry point at import.
    """
    unknown = _documented_keys() - set(Settings.model_fields)
    assert not unknown, f".env.example documents non-existent settings: {sorted(unknown)}"


def test_env_example_documents_every_field() -> None:
    """Every `Settings` field appears in `.env.example`.

    Operators read that file to learn what is tunable; an undocumented field is an invisible knob.
    """
    undocumented = set(Settings.model_fields) - _documented_keys()
    assert not undocumented, f"settings missing from .env.example: {sorted(undocumented)}"


# `.env.example` fields whose shipped line deliberately differs from the code default, and why. A
# row is a claim the file is better off saying something else; there are none.
_DELIBERATE_ENV_EXAMPLE_OVERRIDES: dict[str, str] = {}


def test_env_example_ships_the_code_defaults(tmp_path: Path) -> None:
    """`.env.example` ships the code defaults, compared as values.

    A `.env` copied from it is configuration, so a drifted value is a regression for anyone
    following the quickstart. Compared as parsed values (`10` equals `10.0`); a path a
    `default_factory` computes from the install location is left commented out.
    """
    env = tmp_path / ".env"
    env.write_text(_ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    documented = Settings(_env_file=env)  # type: ignore[call-arg]
    shipped = Settings(_env_file=None)  # type: ignore[call-arg]

    differing = {
        name: (getattr(documented, name), getattr(shipped, name))
        for name in Settings.model_fields
        if name not in _DELIBERATE_ENV_EXAMPLE_OVERRIDES
        and getattr(documented, name) != getattr(shipped, name)
    }
    assert not differing, (
        ".env.example says it lists every field at its default, and these disagree "
        f"(example, code): {differing}"
    )
    stale = sorted(
        name
        for name in _DELIBERATE_ENV_EXAMPLE_OVERRIDES
        if getattr(documented, name) == getattr(shipped, name)
    )
    assert not stale, f"{stale} no longer differ; drop the override row"


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
    ("name", "overrides", "fires"),
    [
        # Each of these is a rule a field comment states; the guard enforces it at startup (REV-18,
        # D-136). `fires` is a phrase from the guard's own message, so a row cannot pass because an
        # earlier guard fired for a different reason.
        (
            # The workers guard reads `service_uvicorn_workers` alone, so the
            # store is not set here: it decides nothing.
            "uvicorn workers above one are refused whatever else is set",
            {"service_uvicorn_workers": 4},
            "service_uvicorn_workers>1 silently breaks five per-process guarantees",
        ),
        (
            "a fleet cannot admit more turns than its declared ceiling",
            {
                "service_fleet_replicas": 6,
                "service_max_concurrent_turns": 16,
                "service_fleet_max_concurrent_turns": 48,
            },
            r"may admit \d+ concurrent turns",
        ),
        # No row sets `service_uvicorn_workers` above one for the fleet product: the workers guard
        # refuses it first, so the workers factor in `replicas × workers × cap` is always 1.
        (
            "a mid-turn resume cannot outlive its turn",
            {
                "mid_turn_resume_enabled": True,
                "mid_turn_resume_timeout_seconds": 900.0,
                "service_turn_timeout_seconds": 600.0,
            },
            "mid_turn_resume_timeout_seconds must be smaller than",
        ),
        (
            "revision rounds cannot outlast the turn they run inside",
            {
                "verifier_enabled": True,
                "answer_review_max_rounds": 20,
                "verifier_timeout_seconds": 30.0,
                "service_turn_timeout_seconds": 600.0,
            },
            "of judging alone against a",
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
            "guards nothing; set at least one budget_max",
        ),
        (
            "embedding_dim must match the note_index vector column when vector search is on",
            {"embedding_dim": 768, "data_sources": "graph,vector"},
            "disagrees with the note_index vector column",
        ),
        # DARK-8: `reindex_notes` writes the embedding column for every note-index-backed source, so
        # the dimension check applies whenever any such source is on, not only the vector one.
        (
            "a lexical-only deployment reaches the same vector column",
            {"embedding_dim": 768, "data_sources": "graph,lexical"},
            "disagrees with the note_index vector column",
        ),
        (
            "the scheduled reindex writes it with no retrieve source at all",
            {"embedding_dim": 768, "data_sources": "graph", "note_reindex_enabled": True},
            "disagrees with the note_index vector column",
        ),
    ],
)
def test_configurations_the_comments_forbid_are_rejected(
    name: str,
    overrides: dict,  # type: ignore[type-arg]
    fires: str,
) -> None:
    """Configurations the comments forbid are rejected at startup, by the guard named.

    `match=` makes each row an assertion about which guard ran; a bare `pytest.raises(ValueError)`
    would pass if an earlier guard swallowed a later row.
    """
    with pytest.raises(ValueError, match=fires):
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
    """Enforcing identity without the plan gate is refused.

    `plan_gate.gate_applies` requires `harness_enabled` and `plan_only` autonomy. A deployment
    setting `CHEMCLAW_ENTRA_REQUIRED=true` with the harness off would let any authenticated user run
    turns that start state-changing work with nothing to approve. Same argument as
    `_refuse_unauthenticated_exposure`. The harness defaults on, so this fires only when it is
    turned off explicitly.
    """
    with pytest.raises(ValueError, match="harness_enabled"):
        Settings(_env_file=None, harness_enabled=False, **_ENFORCED)  # type: ignore[call-arg]


def test_the_enforced_posture_with_the_gate_attached_constructs() -> None:
    """The control: what the shipped chart sets must still boot — and is now also the default."""
    assert Settings(_env_file=None, harness_enabled=True, **_ENFORCED) is not None  # type: ignore[call-arg]
    assert Settings(_env_file=None, **_ENFORCED) is not None  # type: ignore[call-arg]


def test_the_opt_out_is_stated_in_the_same_vocabulary_as_the_thing_it_declines() -> None:
    """The opt-out is stated in the vocabulary of what it declines: `harness_autonomy=execute`.

    `service_allow_insecure` means something else and would get set for the wrong reason. With the
    harness on, `execute` keeps the todo list and removes the gate, which is what an unsupervised
    deployment wants. A per-profile `harness_autonomy` still wins, so the opt-out cannot disarm a
    profile that narrowed on purpose.
    """
    relaxed = Settings(  # type: ignore[call-arg]
        _env_file=None, harness_enabled=False, harness_autonomy="execute", **_ENFORCED
    )
    assert relaxed.entra_required and not relaxed.harness_enabled
    assert (
        Settings(  # type: ignore[call-arg]
            _env_file=None, harness_enabled=True, harness_autonomy="execute", **_ENFORCED
        )
        is not None
    )


def test_a_wildcard_cors_origin_is_refused() -> None:
    """A wildcard CORS origin is refused.

    `_add_cors` passes the field to `CORSMiddleware` verbatim. The blast radius is bounded today by
    two properties of other code (no credentials, bearer auth), which nothing pins. No deployment
    needs `*`: empty means no cross-origin access, and a browser client has an origin to name.
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
    """An undeclared fleet ceiling (0) checks nothing.

    A CLI, a test or a single-pod dev run has no fleet to bound; a guard firing there would be
    switched off everywhere.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        service_fleet_replicas=99,
        service_max_concurrent_turns=64,
        # Room for 64 turns' sockets, so the connection backstop is not what this test trips.
        service_max_connections=2048,
    )
    assert settings.service_fleet_max_concurrent_turns == 0


def test_a_fleet_exactly_at_its_ceiling_is_allowed() -> None:
    """A fleet exactly at its ceiling is allowed: the check is `>`, not `>=`.

    `values.yaml` declares exactly 6 replicas × 1 worker × 8 turns = 48, so off-by-one would stop
    the shipped chart booting.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        service_fleet_replicas=6,
        service_max_concurrent_turns=8,
        service_fleet_max_concurrent_turns=48,
    )
    assert settings.service_fleet_max_concurrent_turns == 48


def test_the_fleet_ceiling_error_names_both_sides_and_every_factor() -> None:
    """The fleet ceiling error names both sides and every factor.

    The per-process cap is not the whole story; the operator must see the product.
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
    """A fleet exactly at its connection ceiling is allowed: `>`, not `>=`.

    A release provisioning exactly what it opens is correct. The numbers sit exactly on the edge
    under the summed-pool arithmetic, and the assertion reads the figure the check compares.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        pg_fleet_pools=17,
        pg_pool_max_size=8,
        pg_fleet_max_connections=129,
    )
    assert settings.fleet_connections_per_server() == (129, 0)


def test_a_fleet_that_would_exhaust_the_server_is_refused_by_pools_not_by_pods() -> None:
    """A fleet that would exhaust the server is refused by pools, not by pods.

    A front-door process holds three pools (stores, `/readyz`, checkpointer;
    `tests/test_fleet_pools.py`), and the `/readyz` pool is one connection wide. So one front door
    at `pg_pool_max_size=16` is 33 connections, refused by a ceiling of 32 and fitting in 40.
    Written against the single-process case, the smallest fleet that fails.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pools=3,  # one front door
            pg_pool_max_size=16,
            pg_fleet_max_connections=30,
        )
    assert "33 Postgres connections" in str(excinfo.value)
    # The half a product cannot express: the same fleet against the ceiling the old arithmetic
    # refused it at. Asserting only the refusal above would pass on `pools × max_size` too.
    Settings(  # type: ignore[call-arg]
        _env_file=None, pg_fleet_pools=3, pg_pool_max_size=16, pg_fleet_max_connections=40
    )


def test_the_connection_ceiling_error_names_both_sides_and_every_factor() -> None:
    """The connection ceiling error names both sides and every factor.

    The operator needs both numbers and both levers, that the count is of pools rather than pods,
    and how many pools are narrow, so they can reproduce the refused number.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pools=17,
            pg_pool_max_size=16,
            pg_fleet_max_connections=136,
        )
    message = str(excinfo.value)
    assert "257" in message and "136" in message
    # The decomposition is printed from the terms the figure was built from, not from the raw
    # settings — see `test_the_refusal_prints_a_breakdown_that_reaches_its_own_number`, which is
    # what keeps 16 x 16 + 1 equal to the 257 this message refuses over.
    assert "16 pool(s) of 16 plus 1 of one" in message
    assert "pg_fleet_max_connections" in message and "pg_pool_max_size" in message
    assert "the front door holds three" in message


def test_the_calculation_backend_budget_is_undeclared_by_default() -> None:
    """The calculation backend budget is undeclared (0) by default.

    The number belongs to a pod in another release (`Chemclaw3-mcp` `servers/calc`), so any other
    default would be a guess.
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
    """The calculation backend ceiling error names both sides and every factor.

    `servers/calc` is one shared pod, offered the per-process cap times the worker replica count
    (BS-07); scaling workers is the lever that trips this.
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


def test_a_screen_fanned_calc_fleet_is_counted_at_its_fan_out() -> None:
    """A screen-fanned calc fleet is counted at its fan-out.

    `compose.solvent_comparison` and `compose.species_solvent_comparison` hold one `calc_session`
    per branch under `asyncio.Semaphore(calc_screen_max_parallel)`, so one activity can present that
    many concurrent sessions. The fleet is the one declared legal above, so only the fan-out is
    pinned.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            calc_fleet_worker_processes=2,
            worker_max_concurrent_activities=8,
            calc_backend_max_concurrent_requests=16,
            calc_screen_max_parallel=4,
        )
    message = str(excinfo.value)
    assert "64" in message and "16" in message
    assert "4 media in flight per screen" in message, message
    assert "calc_screen_max_parallel" in message


def test_the_embedding_width_check_still_leaves_the_standalone_embedder_alone() -> None:
    """The embedding width check leaves the standalone embedder alone.

    The check asks whether anything writes `note_index`, not whether an embedding is configured; a
    deployment without pgvector may choose any width.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, embedding_dim=768, data_sources="graph", note_reindex_enabled=False
    )
    assert settings.embedding_dim == 768


def test_no_calculator_setting_is_declared_without_a_reader() -> None:
    """No calculator setting is declared without a reader.

    The calc server reads the same `CHEMCLAW_` names, so a dead field here gives an operator no
    error and no effect while the server's identically spelled setting decides the calculation.
    Scoped to this section, which configures only code in this repository.
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
    """Note reindex is derived from the source list unless overridden.

    Enabling a `vector`/`lexical` leg must build the index it queries; otherwise both legs return
    `chunks: 0, failed: false` forever.
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
    """The connector-job ceiling covers every activity a bundle child can run.

    The `results` republish walk is another multi-hour activity; the guard uses the max over all
    such budgets, so a new long activity is covered by construction.
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
    """A heartbeat timeout must fit inside the budget it reports within.

    - A drain whose start-to-close is below the heartbeat timeout can never detect a dead worker.
    - A long heartbeat timeout derives a beat interval (`durable/heartbeat.py::beating`) longer than
      `retention_timeout_seconds`, so the sweep never beats before timing out.
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
    """The two metrics expositions may not claim one port.

    One process serves both, so equal ports are a bind failure that would surface as "Temporal is
    unreachable". Refused at startup, naming both settings.
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


def test_enforced_posture_refuses_a_plaintext_temporal_broker() -> None:
    """A plaintext non-loopback broker is refused under entra_required.

    It opens an unauthenticated gRPC channel, and identity rides inside the workflow payload.
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "entra_required": True,
        "entra_audience": "api://x",
        "entra_tenant_id": "t",
        "llm_base_url": "http://llm:8000/v1",
        "llm_model": "m",
        "harness_enabled": True,
    }
    with pytest.raises(ValueError, match="temporal"):
        Settings(temporal_address="temporal.prod:7233", **base)
    # a CA (or api key, or a loopback address) satisfies it
    Settings(temporal_address="temporal.prod:7233", temporal_tls_ca="/ca.pem", **base)
    Settings(temporal_address="localhost:7233", **base)


def test_enforced_posture_refuses_a_plaintext_postgres_dsn() -> None:
    """A plaintext-or-unverified non-loopback DSN is refused under entra_required.

    The connection carries the transcripts, the turn checkpoints and the audit trail.
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "entra_required": True,
        "entra_audience": "api://x",
        "entra_tenant_id": "t",
        "llm_base_url": "http://llm:8000/v1",
        "llm_model": "m",
        "harness_enabled": True,
        "temporal_tls_ca": "/ca.pem",
    }
    with pytest.raises(ValueError, match="sslmode"):
        Settings(postgres_dsn="postgresql://u:p@pg.prod:5432/db", **base)
    Settings(postgres_dsn="postgresql://u:p@pg.prod:5432/db?sslmode=verify-full", **base)
    Settings(postgres_dsn="postgresql://chemclaw:chemclaw@localhost:5432/chemclaw", **base)


def test_enforced_posture_refuses_a_plaintext_session_store_dsn() -> None:
    """The enforced posture refuses a plaintext session store DSN.

    `session_store_dsn` carries transcripts, approvals, turn costs and the effect ledger when the
    session layer is split. Empty is exempt: it falls back to `postgres_dsn`, checked above.
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "entra_required": True,
        "entra_audience": "api://x",
        "entra_tenant_id": "t",
        "llm_base_url": "http://llm:8000/v1",
        "llm_model": "m",
        "harness_enabled": True,
        "temporal_tls_ca": "/ca.pem",
        "postgres_dsn": "postgresql://u:p@pg.prod:5432/db?sslmode=verify-full",
    }
    with pytest.raises(ValueError, match="session_store_dsn"):
        Settings(session_store_dsn="postgresql://u:p@sessions.prod/db?sslmode=disable", **base)
    Settings(session_store_dsn="postgresql://u:p@sessions.prod/db?sslmode=verify-full", **base)
    Settings(session_store_dsn="", **base)


@pytest.mark.parametrize(
    ("why", "dsn"),
    [
        # libpq dials `hostaddr` when it is present; `host` then only names the certificate. A
        # `host=`-keyword read therefore took the loopback exemption while the socket went to
        # 10.0.0.5 over the network.
        ("hostaddr is the host libpq dials", "host=localhost hostaddr=10.0.0.5 dbname=c user=u"),
        # A repeated URL query parameter resolves to the *last* occurrence in libpq and to the
        # *first* in `parse_qs`, so this connected at `disable` while the guard read `require`.
        ("the last sslmode wins", "postgresql://u:p@pg.prod/db?sslmode=require&sslmode=disable"),
        # Nothing can say what libpq would do with a string libpq cannot read, so it is refused
        # rather than guessed at.
        ("an unparseable DSN fails closed", "this is not a conninfo string"),
    ],
)
def test_the_tls_guard_reads_a_dsn_the_way_libpq_does(why: str, dsn: str) -> None:
    """The TLS guard reads a DSN the way libpq does.

    Each case is a DSN a hand-rolled parse would misread in the direction that admits plaintext; the
    guard uses `conninfo_to_dict`, the same parser `core/db.py` uses.
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "entra_required": True,
        "entra_audience": "api://x",
        "entra_tenant_id": "t",
        "llm_base_url": "http://llm:8000/v1",
        "llm_model": "m",
        "harness_enabled": True,
        "temporal_tls_ca": "/ca.pem",
    }
    with pytest.raises(ValueError):
        Settings(postgres_dsn=dsn, **base)


_ENFORCED_POSTURE: dict[str, Any] = {
    "_env_file": None,
    "entra_required": True,
    "entra_audience": "api://x",
    "entra_tenant_id": "t",
    "llm_base_url": "http://llm:8000/v1",
    "llm_model": "m",
    "harness_enabled": True,
    "temporal_tls_ca": "/ca.pem",
}


@pytest.mark.parametrize(
    ("why", "dsn"),
    [
        # The spelling that used to start and stopped: the hand-rolled parser could not see `host=`
        # inside a URL query, so this took the loopback exemption by not being seen at all.
        ("URL with host= in the query", "postgresql://u:p@/chemclaw?host=/var/run/postgresql"),
        ("the keyword form of the same", "host=/var/run/postgresql dbname=chemclaw user=u"),
        # Linux's abstract namespace, libpq's `@` spelling — the same transport, no filesystem path.
        ("an abstract-namespace socket", "host=@/var/run/postgresql dbname=chemclaw user=u"),
        # A directory list is still only sockets.
        ("two socket directories", "host=/var/run/postgresql,/tmp dbname=chemclaw user=u"),
    ],
)
def test_the_tls_guard_exempts_a_unix_socket_because_a_socket_is_not_a_network(
    why: str, dsn: str
) -> None:
    """The TLS guard exempts a Unix socket, because a socket is not a network.

    libpq treats a `host` starting with `/` (or `@`) as a socket and ignores `sslmode`, so requiring
    it would force a false `sslmode=require` and block socket-based setups.
    """
    Settings(postgres_dsn=dsn, **_ENFORCED_POSTURE)


@pytest.mark.parametrize(
    ("why", "dsn", "escape"),
    [
        # `PQconninfoParse` reads the string; it does not open the service file, so the host and
        # the sslmode that file carries are both invisible here.
        (
            "a service file resolves the host",
            "service=chemclaw",
            "service=chemclaw sslmode=require",
        ),
        (
            "the same as a URL",
            "postgresql:///chemclaw?service=chemclaw",
            "postgresql:///chemclaw?service=chemclaw&sslmode=require",
        ),
        # No host and no service: libpq falls back to `PGHOST`, which is an environment this parse
        # never looks at either.
        (
            "PGHOST resolves the host",
            "dbname=chemclaw user=u",
            "dbname=chemclaw user=u sslmode=require",
        ),
    ],
)
def test_the_tls_guard_refuses_a_dsn_that_names_no_host_at_all(
    why: str, dsn: str, escape: str
) -> None:
    """The TLS guard refuses a DSN that names no host at all.

    `conninfo_to_dict` (`PQconninfoParse`) applies neither `PGHOST`/`PGSSLMODE` nor a `service=`
    file, so "no host" could be a remote plaintext connection. Refused rather than resolved, which
    would reimplement libpq's precedence; the fix is to name the host or state the sslmode.
    """
    with pytest.raises(ValueError, match="no host"):
        Settings(postgres_dsn=dsn, **_ENFORCED_POSTURE)
    # Stating the transport's own answer is enough; nothing here demands the host be spelled out.
    Settings(postgres_dsn=escape, **_ENFORCED_POSTURE)


def test_an_unparseable_dsn_is_refused_without_printing_its_password() -> None:
    """An unparseable DSN is refused without printing its password.

    libpq quotes the offending token, which can be the whole DSN, and this raises at import before
    `SecretRedactingFilter` is installed. The refusal names the setting, never the value.
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "entra_required": True,
        "entra_audience": "api://x",
        "entra_tenant_id": "t",
        "llm_base_url": "http://llm:8000/v1",
        "llm_model": "m",
        "harness_enabled": True,
        "temporal_tls_ca": "/ca.pem",
    }
    secret = "S3cr3t-Pa55w0rd"
    for dsn in (
        f"postgres//chemclaw:{secret}@db.internal/chemclaw",
        f" postgresql://chemclaw:{secret}@db.internal/chemclaw",
    ):
        with pytest.raises(ValueError) as raised:
            Settings(postgres_dsn=dsn, **base)
        assert secret not in str(raised.value), f"the refusal printed the password for {dsn!r}"
        assert "postgres_dsn" in str(raised.value), "the refusal does not name the setting to fix"


def test_the_tls_guard_still_exempts_the_forms_that_carry_no_network() -> None:
    """The TLS guard still exempts the forms that carry no network.

    A socket directory and an IPv6 loopback URL (unbracketed by libpq) stay exempt, or local dev
    under `entra_required` stops booting. A host-less DSN is not among them (see above).
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "entra_required": True,
        "entra_audience": "api://x",
        "entra_tenant_id": "t",
        "llm_base_url": "http://llm:8000/v1",
        "llm_model": "m",
        "harness_enabled": True,
        "temporal_tls_ca": "/ca.pem",
    }
    Settings(postgres_dsn="postgresql:///chemclaw?host=/var/run/postgresql", **base)
    Settings(postgres_dsn="postgresql://u:p@[::1]:5432/chemclaw", **base)


# The prompt-injection envelope's nonce and the durable session store
# (`D-2026-08-27-a-warning-is-the-shape-a-guard-takes-when-raising-would-break-a-deployment`): this
# rule warns rather than refuses, because the shipped chart ships the pairing it flags.


def _envelope_warnings(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """The records this guard emitted — matched on the env var it names, not on the logger."""
    return [r for r in records if "CHEMCLAW_FRAMING_ENVELOPE_SECRET" in r.getMessage()]


def test_a_durable_deployment_without_the_envelope_secret_is_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A durable deployment without the envelope secret is warned, naming both settings and the fix.

    With `session_store="postgres"` and no `framing_envelope_secret`, `agent/framing.py`'s
    per-process nonce orphans replayed envelopes. The contents are asserted: the line must name what
    to set.
    """
    with caplog.at_level(logging.WARNING, logger="chemclaw.core.config"):
        Settings(_env_file=None, session_store="postgres")  # type: ignore[call-arg]
    warnings = _envelope_warnings(caplog.records)
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    message = warnings[0].getMessage()
    assert warnings[0].levelno == logging.WARNING
    assert "CHEMCLAW_SESSION_STORE=postgres" in message
    assert "secrets.optionalKeys.framingEnvelopeSecret" in message
    assert "agent/framing.py" in message


def test_the_durable_pairing_is_warned_about_rather_than_refused() -> None:
    """The durable pairing is warned about rather than refused: it still constructs.

    The shipped chart is this configuration, so a refusal would fail every release on `helm
    upgrade`. Converting the warning means deleting this test.
    """
    durable = Settings(_env_file=None, session_store="postgres")  # type: ignore[call-arg]
    assert durable.session_store == "postgres"


def test_setting_the_envelope_secret_leaves_a_durable_deployment_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deployment that did the thing the warning asks for hears nothing further."""
    with caplog.at_level(logging.WARNING, logger="chemclaw.core.config"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            session_store="postgres",
            framing_envelope_secret=SecretStr("a-deployment-wide-secret"),
        )
    assert _envelope_warnings(caplog.records) == []


def test_a_memory_store_deployment_is_not_warned(caplog: pytest.LogCaptureFixture) -> None:
    """A memory-store deployment is not warned.

    A memory session lives in one process, so no envelope is replayed under another nonce; warning
    there would be noise operators learn to ignore.
    """
    with caplog.at_level(logging.WARNING, logger="chemclaw.core.config"):
        Settings(_env_file=None, session_store="memory")  # type: ignore[call-arg]
    assert _envelope_warnings(caplog.records) == []


def test_the_warning_reaches_stderr_with_no_logging_configured(tmp_path: Path) -> None:
    """The warning reaches stderr with no logging configured.

    `Settings()` is built during import, before `configure_logging()`, so the record reaches stderr
    via `logging.lastResort` (the JSON stream does not carry it). `caplog` cannot prove this.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHEMCLAW_")} | {
        "CHEMCLAW_SESSION_STORE": "postgres",
        "CHEMCLAW_FRAMING_ENVELOPE_SECRET": "",
    }
    done = subprocess.run(
        [sys.executable, "-c", "import chemclaw.core.config"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "CHEMCLAW_FRAMING_ENVELOPE_SECRET is unset" in done.stderr
    assert done.stdout == ""


def _shipped(session_store_dsn: str = "") -> Settings:
    """The chart's rendered fleet numbers, with the session layer wherever the caller says.

    Module level because two tests below stand on the same rendered topology: one asserting how a
    real split is charged, one pinning what a *spelling* of one server costs.
    """
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        pg_fleet_pools=26,
        pg_pool_max_size=8,
        service_fleet_replicas=6,
        postgres_dsn="postgresql://u:p@primary:5432/chemclaw",
        session_store_dsn=session_store_dsn,
    )


def test_a_split_session_store_charges_each_server_its_own_pools() -> None:
    """A split session store charges each server its own pools.

    Pointing the session layer at a second database moves the front door's `/readyz` and
    checkpointer pools there and adds a session pool (`tests/test_fleet_pools.py`). Each server is
    checked against its own ceiling: summing would refuse a deployment that fits.
    """
    assert _shipped().fleet_connections_per_server() == (166, 0)
    assert _shipped("postgresql://u:p@sessions:5432/sessions").fleet_connections_per_server() == (
        112,
        166,
    )

    # Two DSNs, one endpoint: databases split on one server share one ceiling, so the pools are
    # summed onto it. The endpoint is libpq's dialled address (`hostaddr` beats `host`) compared as
    # a string; see `test_one_server_spelled_two_ways_is_charged_as_two`.
    assert _shipped("postgresql://u:p@primary:5432/sessions").fleet_connections_per_server() == (
        278,
        0,
    )
    assert _shipped(
        "postgresql://u:p@primary:5432/sessions?hostaddr=10.0.0.5"
    ).fleet_connections_per_server() == (112, 166)


def test_one_server_spelled_two_ways_is_charged_as_two() -> None:
    """`pg_endpoint` compares strings, so one server spelled two ways is charged as two.

    Pinned as a known property: spelled identically the fleet is refused; spelled differently it
    starts with connections charged to a nonexistent server. Not normalised (that would reimplement
    libpq's precedence) and not measurable at import. The warning tells the operator to check the
    spelling before declaring a second ceiling.
    """
    same_box = {
        "one_spelling": "postgresql://u:p@primary:5432/sessions",
        "short_vs_fqdn": "postgresql://u:p@primary.ns.svc.cluster.local:5432/sessions",
        "omitted_port": "postgresql://u:p@primary/sessions",
        "uppercase_host": "postgresql://u:p@PRIMARY:5432/sessions",
        "trailing_dot_fqdn": "postgresql://u:p@primary.:5432/sessions",
    }
    assert _shipped(same_box["one_spelling"]).fleet_connections_per_server() == (278, 0)
    for name, dsn in list(same_box.items())[1:]:
        primary, elsewhere = _shipped(dsn).fleet_connections_per_server()
        assert (primary, elsewhere) == (112, 166), (
            f"{name}: one server spelled two ways is charged ({primary}, {elsewhere}); the "
            "identical deployment spelled once is charged (278, 0) and refused against 256"
        )


def test_a_hand_set_pool_count_that_cannot_hold_its_readiness_pools_is_charged_in_full() -> None:
    """A hand-set pool count too small to hold its readiness pools is charged in full.

    Fewer than `3 × service_fleet_replicas` pools does not describe a rendered fleet; subtracting
    narrow pools that are not there would under-declare, the direction that exhausts a server.
    """
    defaults = Settings(_env_file=None, pg_fleet_pools=1, pg_pool_max_size=16)  # type: ignore[call-arg]
    assert defaults.fleet_connections_per_server() == (16, 0)

    impossible = Settings(  # type: ignore[call-arg]
        _env_file=None, pg_fleet_pools=2, pg_pool_max_size=16, service_fleet_replicas=3
    )
    assert impossible.fleet_connections_per_server() == (32, 0)

    # A point that separates a `3 x replicas` threshold from `2 x`, which neither case above does.
    borderline = Settings(  # type: ignore[call-arg]
        _env_file=None, pg_fleet_pools=2, pg_pool_max_size=16, service_fleet_replicas=1
    )
    assert borderline.fleet_connections_per_server() == (32, 0), (
        "two pools cannot contain a front door's three, so neither is the readiness probe's and "
        "both are charged full width; subtracting one here is the under-declaration that exhausts "
        "a server"
    )


def test_a_split_session_store_with_no_ceiling_for_its_server_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A split session store with no ceiling for its server warns, not refuses.

    Refusing would break existing split deployments on the `helm upgrade` that introduces the
    setting. The primary server's half still raises.
    """
    with caplog.at_level(logging.WARNING):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pools=26,
            pg_pool_max_size=8,
            service_fleet_replicas=6,
            postgres_dsn="postgresql://u:p@primary:5432/chemclaw",
            session_store_dsn="postgresql://u:p@sessions:5432/sessions",
        )
    assert "CHEMCLAW_PG_SESSION_FLEET_MAX_CONNECTIONS" in caplog.text
    assert "166 connection(s) are charged there" in caplog.text
    assert "postgres.sessionStoreMaxConnections" in caplog.text
    # The warning also fires for one server spelled two ways, where declaring a second ceiling would
    # disable every check, so it says to verify they really are two servers first.
    assert "check that these really are two servers" in caplog.text

    # And silent when the second server is declared, so the warning stays a signal.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pools=26,
            pg_pool_max_size=8,
            service_fleet_replicas=6,
            postgres_dsn="postgresql://u:p@primary:5432/chemclaw",
            session_store_dsn="postgresql://u:p@sessions:5432/sessions",
            pg_session_fleet_max_connections=256,
        )
    assert "CHEMCLAW_PG_SESSION_FLEET_MAX_CONNECTIONS" not in caplog.text


def test_the_session_stores_ceiling_is_refused_when_it_is_exceeded_and_when_there_is_no_split() -> (
    None
):
    """The session store's ceiling is refused when exceeded and when there is no split.

    Without a split it would be a ceiling for a nonexistent server, which
    `ChemclawFleetAboveItsConnectionCeiling` adds to the real one. The setting is new, so refusing
    cannot break an upgrade.
    """
    with pytest.raises(ValueError) as exceeded:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pools=26,
            pg_pool_max_size=8,
            service_fleet_replicas=6,
            pg_fleet_max_connections=256,
            postgres_dsn="postgresql://u:p@primary:5432/chemclaw",
            session_store_dsn="postgresql://u:p@sessions:5432/sessions",
            pg_session_fleet_max_connections=100,
        )
    assert "166 Postgres connections on the session store's own server" in str(exceeded.value)

    with pytest.raises(ValueError) as unsplit:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pools=26,
            pg_pool_max_size=8,
            service_fleet_replicas=6,
            pg_fleet_max_connections=256,
            pg_session_fleet_max_connections=256,
        )
    assert "declares a ceiling for a split session store and there is none" in str(unsplit.value)


def test_a_declared_session_ceiling_is_checked_whether_or_not_the_primary_one_is() -> None:
    """A declared session ceiling is checked whether or not the primary one is.

    `postgres.maxConnections: 0` means "no primary ceiling" and must not silence a session ceiling
    the operator declared; the runtime alert is likewise independent.
    """
    split = {
        "_env_file": None,
        "pg_fleet_pools": 80,
        "pg_pool_max_size": 8,
        "service_fleet_replicas": 20,
        "postgres_dsn": "postgresql://u:p@primary:5432/chemclaw",
        "session_store_dsn": "postgresql://u:p@sessions:5432/sessions",
    }
    with pytest.raises(ValueError, match="on the session store's own server"):
        Settings(**split, pg_fleet_max_connections=0, pg_session_fleet_max_connections=180)  # type: ignore[arg-type]

    # And the primary's own check still self-disables at 0, which is what that value is for.
    settings = Settings(**split, pg_fleet_max_connections=0)  # type: ignore[arg-type]
    assert settings.fleet_connections_per_server() == (320, 500)


def test_the_refusal_prints_a_breakdown_that_reaches_its_own_number() -> None:
    """The refusal prints a breakdown that adds up to its own number.

    `_fleet_pool_widths` derives the terms through the same branches as
    `fleet_connections_per_server`, so `wide × pg_pool_max_size + narrow` is that function's answer.
    """
    import re

    def _refusal(pools: int, per_pool: int, replicas: int, ceiling: int, session: str = "") -> str:
        with pytest.raises(ValueError) as excinfo:
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                postgres_dsn="postgresql://u:p@primary:5432/c",
                pg_fleet_pools=pools,
                pg_pool_max_size=per_pool,
                service_fleet_replicas=replicas,
                pg_fleet_max_connections=ceiling,
                session_store_dsn=session,
            )
        return str(excinfo.value)

    for label, message in (
        ("split", _refusal(26, 8, 6, 100, "postgresql://u:p@sessions:5432/s")),
        ("no split", _refusal(26, 8, 6, 100)),
        ("pair too small for its readiness pools", _refusal(2, 16, 3, 10)),
    ):
        found = re.search(
            r"may open (\d+) Postgres connections on .*?"
            r"\((\d+) pool\(s\) of (\d+) plus (\d+) of one\)",
            message,
        )
        assert found is not None, f"{label}: the refusal printed no breakdown at all: {message}"
        total, wide, per_pool, narrow = (int(n) for n in found.groups())
        assert wide * per_pool + narrow == total, (
            f"{label}: the refusal says {total} and shows {wide}x{per_pool} + {narrow} = "
            f"{wide * per_pool + narrow}"
        )


def test_every_bounded_drain_can_finish_a_run_inside_the_ceiling_that_kills_it() -> None:
    """Every bounded drain can finish a run inside the ceiling that kills it.

    Four Schedules bound their run by an iteration count; iterations × dispatches × activity budget
    must fit `schedule_run_timeout_seconds`, or the run is killed as `TIMED_OUT` and some restart
    from page one. Asserted on live settings, so ENV overrides are held to the same rule.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    for iterations_name, budget_name, per_iteration in _BOUNDED_DRAINS:
        iterations = getattr(settings, iterations_name)
        budget = getattr(settings, budget_name)
        needed = budget * (1 + iterations * per_iteration)
        assert needed <= settings.schedule_run_timeout_seconds, (
            f"{iterations_name}={iterations} needs {needed}s of activity budget against a "
            f"{settings.schedule_run_timeout_seconds}s run ceiling"
        )


def test_the_dispatch_count_each_bounded_drain_declares_is_the_one_it_runs() -> None:
    """`_BOUNDED_DRAINS`' dispatch count is derived from the workflow's loop, never trusted.

    Counted by walking each `run` method's AST for dispatch calls inside a loop (as
    `tests/test_activity_queue_bound.py` does tree-wide); a regex could not tell the loop's
    dispatches from the planning call before it.
    """
    dispatch = {"execute_activity", "execute_local_activity", "start_activity"}
    src = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "durable"
    for iterations_name, _budget, declared in _BOUNDED_DRAINS:
        module = src / f"{iterations_name.removesuffix('_max_iterations')}.py"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        runs = [
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"
        ]
        assert len(runs) == 1, f"{module.name} no longer has exactly one workflow `run`"
        in_a_loop = {
            id(n)
            for loop in ast.walk(runs[0])
            if isinstance(loop, ast.While | ast.For)
            for n in ast.walk(loop)
        }
        counted = sum(
            1
            for n in ast.walk(runs[0])
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in dispatch
            and id(n) in in_a_loop
        )
        assert counted == declared, (
            f"{module.name}'s drain loop dispatches {counted} activities per iteration, but "
            f"_BOUNDED_DRAINS declares {declared} — the run-ceiling arithmetic is now wrong by "
            f"a factor of {counted / declared:.2f}"
        )


def test_a_per_actor_cap_at_or_above_the_process_cap_is_refused_at_startup() -> None:
    """A per-actor cap at or above the process cap is refused at startup.

    `chemclaw_turn_actor_capacity` publishes the number, so a cap that can never fire looks like
    protection. `tests/test_deploy_chart.py` reads `values.yaml` only; this sees overrides.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            service_max_concurrent_turns=12,
            service_max_concurrent_turns_per_actor=12,
        )
    message = str(excinfo.value)
    assert "service_max_concurrent_turns_per_actor" in message
    # Both numbers, because the remedy is to move one of them and the operator has to know which.
    assert "12" in message

    with pytest.raises(ValueError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            service_max_concurrent_turns=4,
            service_max_concurrent_turns_per_actor=8,
        )


def test_zero_is_the_off_switch_and_is_never_refused() -> None:
    """0 is "not consulted", which is the shipped code default and must survive the guard above.

    `chemclaw.cli.live_storm` drives tens of concurrent turns from one credential, so a code
    default that refused the combination would break the instrument that sweeps the admission cap.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        service_max_concurrent_turns=1,
        service_max_concurrent_turns_per_actor=0,
    )
    assert settings.service_max_concurrent_turns_per_actor == 0

    # And strictly-below is accepted, which is what the chart ships.
    ok = Settings(  # type: ignore[call-arg]
        _env_file=None,
        service_max_concurrent_turns=12,
        service_max_concurrent_turns_per_actor=4,
    )
    assert ok.service_max_concurrent_turns_per_actor == 4
