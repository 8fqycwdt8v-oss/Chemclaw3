"""The Helm chart's configuration matches the app's `Settings` (DA-10/D-2, the deployment edge).

The chart is the one artifact no other test exercises: it is rendered by `helm install`, in a
cluster, on deployment day. `make helm-validate` (CI) checks the rendered YAML against the
Kubernetes schemas — but a schema check cannot know whether `CHEMCLAW_FOO` is a *real* setting.
Two failure modes live in that gap, and both are silent until production:

1. **A key that is not a field** — pydantic-settings tolerates an unknown prefixed *environment*
   variable (unlike an unknown key in a `.env` file, which is what broke the quickstart in DA-1),
   so the operator who sets it gets no error and no effect. A setting they believe they turned on
   is quietly ignored. In a GxP deployment that is worse than a crash.
2. **A malformed value on a real field** — this one *does* crash, at import, in every pod at once.

These tests close both, offline, against the same `Settings` the pods construct.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from chemclaw.core.config import Settings

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "chemclaw"
_VALUES: dict[str, Any] = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))

# Env the chart injects outside the ConfigMap: the mTLS file paths (`_helpers.tpl`) and the secret
# refs. The pod sees these too, so a parity check that ignored them would miss half the surface.
_TLS_ENV = {"CHEMCLAW_TEMPORAL_TLS_CERT", "CHEMCLAW_TEMPORAL_TLS_KEY", "CHEMCLAW_TEMPORAL_TLS_CA"}

# Not every `CHEMCLAW_*` env the chart sets is read by Python: the entrypoint dispatches on
# `CHEMCLAW_COMPONENT`, and the knowledge-sync init/sidecar takes its whole configuration
# (repo URL, checkout paths, push credential, interval) from `deploy/knowledge-sync.sh`. Those are
# first-party consumers, just shell ones. They are *discovered* from the scripts rather than listed
# here, so the exemption can never be wider than what something actually reads — an enumerated
# guard catches drift; a hardcoded one only catches what someone already thought of.
_DEPLOY_SCRIPTS = (Path(__file__).resolve().parents[1] / "deploy").glob("*.sh")
_SHELL_CONSUMED_ENV = {
    key
    for script in _DEPLOY_SCRIPTS
    for key in re.findall(r"CHEMCLAW_[A-Z0-9_]+", script.read_text(encoding="utf-8"))
}


def _field_for(env_key: str) -> str:
    """The `Settings` field name an env key maps to (the `CHEMCLAW_` prefix, lowercased)."""
    return env_key.removeprefix("CHEMCLAW_").lower()


def _helper_env_keys() -> set[str]:
    """`CHEMCLAW_*` names injected from `_helpers.tpl` rather than the ConfigMap.

    Read from the template text: these arrive as literal `- name:` entries (mTLS paths, the
    knowledge-sync block), so they are pod env exactly like the ConfigMap keys and belong in the
    same parity check.
    """
    template = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    return set(re.findall(r"name:\s*(CHEMCLAW_[A-Z0-9_]+)", template))


def _derived_config_keys() -> set[str]:
    """`CHEMCLAW_*` keys the ConfigMap *computes* rather than copying from `.Values.config`.

    REV-15: the parity check read `.Values.config` and the helpers and stopped there, so the two
    keys `templates/config.yaml` derives — `CHEMCLAW_NOTE_REPO_DIR` from the knowledge volume
    layout and `CHEMCLAW_CONNECTOR_URLS` from the enabled bundle set — were outside *both* tests.
    Neither "is this a real setting" nor "does this value load" applied to them, and
    `connector_urls` is a `dict[str, str]` parsed from rendered JSON, which is exactly the shape
    that crashes every pod at import when it renders wrong.

    Discovered from the template rather than listed, so a third derived key is covered on the day
    it is added.
    """
    template = (_CHART / "templates" / "config.yaml").read_text(encoding="utf-8")
    return set(re.findall(r"^\s*(CHEMCLAW_[A-Z0-9_]+):", template, flags=re.MULTILINE))


def _rendered_derived_values() -> dict[str, str]:
    """What the ConfigMap's two derived keys render to under the chart's own values.

    The helper's logic is reproduced here, which is a duplication worth taking: the alternative is
    shelling out to `helm`, and this suite is the *offline* half that runs everywhere (the rendered
    check is `make helm-validate`). What it buys is that the JSON `CHEMCLAW_CONNECTOR_URLS`
    actually produces is fed through `Settings`, so a render that emits something `dict[str, str]`
    cannot parse fails here rather than in the cluster.
    """
    urls = {
        name: f"http://chemclaw-connector-{name}:{_VALUES['connectorPort']}/mcp"
        for name, cfg in _VALUES["connectors"].items()
        if cfg.get("enabled") and cfg.get("server")
    }
    autoscaling = _VALUES["service"]["autoscaling"]
    return {
        "CHEMCLAW_NOTE_REPO_DIR": _VALUES["knowledge"]["noteRepoPath"],
        "CHEMCLAW_CONNECTOR_URLS": json.dumps(urls),
        "CHEMCLAW_SERVICE_FLEET_REPLICAS": str(
            autoscaling["maxReplicas"] if autoscaling["enabled"] else _VALUES["service"]["replicas"]
        ),
    }


def _chart_env_keys() -> set[str]:
    """Every `CHEMCLAW_*` env name the chart puts into a pod, from all sources."""
    return (
        set(_VALUES["config"])
        | set(_VALUES["secrets"]["keys"].values())
        | _TLS_ENV
        | _helper_env_keys()
        | _derived_config_keys()
        | {"CHEMCLAW_COMPONENT"}
    )


def test_chart_config_keys_have_a_consumer() -> None:
    """Every `CHEMCLAW_*` key the chart injects is read by `Settings` or by a deploy script.

    A key that is neither is accepted silently by pydantic-settings when it arrives as an
    environment variable, so the operator who sets it gets no error and no effect. This is the only
    place that mistake can be caught.
    """
    orphans = {
        key
        for key in _chart_env_keys()
        if _field_for(key) not in Settings.model_fields and key not in _SHELL_CONSUMED_ENV
    }
    assert not orphans, f"chart sets env nothing reads: {sorted(orphans)}"


def test_chart_config_values_load_as_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chart's own values construct a valid `Settings` — the pods' boot path, proven offline.

    Models the real pod environment: the ConfigMap block, plus placeholder values for the
    secret-provided keys and the mTLS paths the chart mounts. A malformed value (a bad enum, an
    out-of-range number) would crash every pod at import; here it fails a test instead.

    Secret keys that no `Settings` field claims are skipped rather than forced in: the knowledge
    repo's push credential is consumed by `deploy/knowledge-sync.sh`, so passing it as an init
    kwarg would model the pod wrongly (the pod gets it as *env*, which pydantic-settings ignores)
    and fail on `extra="forbid"` for a configuration that is in fact correct.
    """
    overrides = {_field_for(key): str(value) for key, value in _VALUES["config"].items()}
    for env_key in _VALUES["secrets"]["keys"].values():
        if _field_for(env_key) in Settings.model_fields:
            overrides.setdefault(_field_for(env_key), "placeholder")
    mount = _VALUES["secrets"]["temporalTls"]["mountPath"]
    for env_key, filename in zip(sorted(_TLS_ENV), ["ca.crt", "tls.crt", "tls.key"], strict=True):
        overrides[_field_for(env_key)] = f"{mount}/{filename}"
    overrides["postgres_dsn"] = "postgresql://chemclaw:chemclaw@postgres:5432/chemclaw"
    # The two keys the ConfigMap derives rather than copies, passed as *environment* — which is
    # how the pod receives them and, for `connector_urls`, the only way the value works at all:
    # pydantic-settings JSON-decodes a complex field from an env var and does not from an init
    # kwarg, so handing the rendered JSON string to `Settings(...)` fails with `dict_type`. That
    # asymmetry is why modelling the pod matters here rather than merely type-checking a literal,
    # and it is the half of the pod environment this test did not reach before (REV-15).
    for env_key, value in _rendered_derived_values().items():
        monkeypatch.setenv(env_key, value)

    loaded = Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]
    # Asserted, not merely constructed: `connector_urls` is the one derived value whose *shape*
    # matters, and a JSON render that produced a list or a nested object would still construct
    # some `Settings` while pointing the front door at nothing.
    assert loaded.connector_urls, "the chart renders no connector URLs; every bundle is unreachable"
    assert all(url.startswith("http") for url in loaded.connector_urls.values())


def test_chart_declares_only_the_documented_secrets() -> None:
    """The chart names exactly the plain secrets the architecture signed off — no sixth crept in.

    Everything else is workload-identity federation, i.e. no client secret at rest; a new plain
    secret is an architecture change (D-047), so it should not pass unnoticed. Each addition is
    argued here rather than waved through, which is why this list is written out instead of derived.

    The knowledge-repo push credential is the fourth (gap DEP-2, D-088): the PR-gate submitter
    shells out to `git push`, and a git host authenticates that push with a token — there is no
    federated exchange for it the way there is for Entra-fronted APIs. Without it every
    agent-authored note fails at push in-cluster.

    The webhook-signing secret is the fifth (D-2026-07-31-a-proposal-is-a-record-not-a-branch), and
    it is the same argument one step further along the same path: the git host now tells the
    deployment which notes were merged, and that claim closes a proposal — so it must be
    authenticated, and the host authenticates itself by signing, not by holding an Entra identity.
    Without it every reviewed note stays in the queue forever, which is a review surface nobody
    works. Note the polarity: an *absent* secret is safe here (the route refuses to decide
    anything), unlike the four above, where absent means the capability fails.

    The audit-anchor key is the sixth (D-2026-08-01-a-restore-is-a-truncation-nobody-can-see), and
    it is a secret for the reason that makes the anchor evidence rather than a note-to-self: an
    actor able to delete audit rows is able to insert a lower anchor too, so the seal only means
    something if its key lives somewhere a database compromise does not reach. It shares the
    webhook secret's polarity — absent is *safe* rather than broken. The chain keeps catching
    modification, reordering, interior deletion and prefix truncation, and a point-in-time restore
    stays what it has always been: a trailing deletion nothing can see.
    """
    assert set(_VALUES["secrets"]["keys"].values()) == {
        "CHEMCLAW_LLM_API_KEY",
        "CHEMCLAW_HPC_API_TOKEN",
        "CHEMCLAW_POSTGRES_DSN",
        "CHEMCLAW_KNOWLEDGE_REPO_TOKEN",
        "CHEMCLAW_NOTE_WEBHOOK_SECRET",
        "CHEMCLAW_AUDIT_ANCHOR_SECRET",
    }


def _settings_from_chart(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """`Settings` as the pods build them, from the chart's own values.

    Shared by the tests below, which are the *inverse* of the parity check above: parity asks
    whether a shipped value loads, these ask whether the thing it switches on actually happens.
    That distinction is the whole of REV-15 — `otel_enabled=True` loaded perfectly and then
    CrashLoopBackOff'd every pod, because loading a bool proves nothing about executing it.
    """
    overrides = {_field_for(key): str(value) for key, value in _VALUES["config"].items()}
    for env_key in _VALUES["secrets"]["keys"].values():
        if _field_for(env_key) in Settings.model_fields:
            overrides.setdefault(_field_for(env_key), "placeholder")
    mount = _VALUES["secrets"]["temporalTls"]["mountPath"]
    for env_key, filename in zip(sorted(_TLS_ENV), ["ca.crt", "tls.crt", "tls.key"], strict=True):
        overrides[_field_for(env_key)] = f"{mount}/{filename}"
    overrides["postgres_dsn"] = "postgresql://chemclaw:chemclaw@postgres:5432/chemclaw"
    for env_key, value in _rendered_derived_values().items():
        monkeypatch.setenv(env_key, value)
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


def test_the_shipped_budget_guard_actually_refuses_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CHEMCLAW_BUDGET_ENABLED: "true"` must refuse past a cap, not merely parse (REV-16).

    Off is the right *code* default — a CLI or a test must not 429 — but a deployment serving real
    users has no reason to be unguarded. A turn is iteration-capped and the *number* of turns is
    not, so a client or an automated push-back loop can accumulate unbounded LLM spend; the load
    run that validated this system ran with budgets on, so "on" is the configuration that was
    actually measured.

    Executed rather than asserted on the flag, because `budget_enabled=true` with every cap at 0
    also parses and guards nothing — a configuration the composed-`Settings` validator now rejects,
    and which this would catch independently.
    """
    from chemclaw.api.budget import BudgetTracker

    chart = _settings_from_chart(monkeypatch)
    assert chart.budget_enabled, "the chart no longer enables the runaway-cost guard"
    monkeypatch.setattr("chemclaw.api.budget.settings", chart)

    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=chart.budget_max_tokens_per_session)
    with pytest.raises(Exception) as refused:
        tracker.check("s1", "alice")
    assert "budget" in str(refused.value).lower() or "cap" in str(refused.value).lower()


def test_the_shipped_config_schedules_the_audit_chain_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tamper-evident chain is verified on a Schedule under the shipped values (REV-16).

    `audit_verify_enabled` "only earns a Schedule where a durable audit sink is actually
    configured" — and this chart sets `SESSION_STORE: postgres`, which is precisely what makes
    `default_audit_sink()` durable. So the precondition holds here and the flag was still off: the
    one deployment that *has* a chain was the one never checking it, and a chain nobody checks
    detects tampering only after somebody thinks to look.

    Asserted on the built schedule list rather than on the flag, because the flag is one branch
    away from a schedule that is planned but never applied.
    """
    from chemclaw.cli import schedules as schedules_module

    chart = _settings_from_chart(monkeypatch)
    assert chart.session_store == "postgres", "no durable audit sink; the precondition changed"
    monkeypatch.setattr(schedules_module, "settings", chart)

    planned = {job.schedule_id for job in schedules_module.planned_schedules()}
    assert "audit-verify" in planned, (
        "the shipped configuration plans no audit-chain verification, so the GxP hash chain is "
        "only ever checked by someone remembering to run `make audit-verify`"
    )
