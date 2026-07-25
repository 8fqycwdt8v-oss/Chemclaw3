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

from pathlib import Path
from typing import Any

import yaml

from chemclaw.config import Settings

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "chemclaw"
_VALUES: dict[str, Any] = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))

# `CHEMCLAW_COMPONENT` is set per-Deployment to name the workload (service / workers / mcp). It is
# deliberately not a `Settings` field — nothing in the app reads it; it identifies the pod for an
# operator reading `kubectl describe`. Listed here so the parity check stays exact rather than
# loosened: any *other* non-field key is a real finding.
_NON_SETTINGS_ENV = {"CHEMCLAW_COMPONENT"}

# Env the chart injects outside the ConfigMap: the mTLS file paths (`_helpers.tpl`) and the secret
# refs. The pod sees these too, so a parity check that ignored them would miss half the surface.
_TLS_ENV = {"CHEMCLAW_TEMPORAL_TLS_CERT", "CHEMCLAW_TEMPORAL_TLS_KEY", "CHEMCLAW_TEMPORAL_TLS_CA"}


def _field_for(env_key: str) -> str:
    """The `Settings` field name an env key maps to (the `CHEMCLAW_` prefix, lowercased)."""
    return env_key.removeprefix("CHEMCLAW_").lower()


def _chart_env_keys() -> set[str]:
    """Every `CHEMCLAW_*` env name the chart puts into a pod, from all four sources."""
    return (
        set(_VALUES["config"])
        | set(_VALUES["secrets"]["keys"].values())
        | _TLS_ENV
        | _NON_SETTINGS_ENV
    )


def test_chart_config_keys_are_real_settings() -> None:
    """Every `CHEMCLAW_*` key the chart injects names a real `Settings` field.

    A key that is not a field is accepted silently by pydantic-settings when it arrives as an
    environment variable — so this is the only place the mistake can be caught.
    """
    unknown = {
        key
        for key in _chart_env_keys() - _NON_SETTINGS_ENV
        if _field_for(key) not in Settings.model_fields
    }
    assert not unknown, f"chart sets env that is not a Settings field: {sorted(unknown)}"


def test_chart_config_values_load_as_settings() -> None:
    """The chart's own values construct a valid `Settings` — the pods' boot path, proven offline.

    Models the real pod environment: the ConfigMap block, plus placeholder values for the three
    secret-provided keys and the mTLS paths the chart mounts. A malformed value (a bad enum, an
    out-of-range number) would crash every pod at import; here it fails a test instead.
    """
    overrides = {_field_for(key): str(value) for key, value in _VALUES["config"].items()}
    for env_key in _VALUES["secrets"]["keys"].values():
        overrides.setdefault(_field_for(env_key), "placeholder")
    mount = _VALUES["secrets"]["temporalTls"]["mountPath"]
    for env_key, filename in zip(sorted(_TLS_ENV), ["ca.crt", "tls.crt", "tls.key"], strict=True):
        overrides[_field_for(env_key)] = f"{mount}/{filename}"
    overrides["postgres_dsn"] = "postgresql://chemclaw:chemclaw@postgres:5432/chemclaw"

    Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


def test_chart_declares_the_three_documented_secrets() -> None:
    """The three-secret model (F6-T6) is what the chart actually names — no fourth crept in.

    Everything else is workload-identity federation, i.e. no client secret at rest; a new plain
    secret is an architecture change (D-047), so it should not pass unnoticed.
    """
    assert set(_VALUES["secrets"]["keys"].values()) == {
        "CHEMCLAW_LLM_API_KEY",
        "CHEMCLAW_HPC_API_TOKEN",
        "CHEMCLAW_POSTGRES_DSN",
    }
