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

import re
from pathlib import Path
from typing import Any

import yaml

from chemclaw.config import Settings

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


def _chart_env_keys() -> set[str]:
    """Every `CHEMCLAW_*` env name the chart puts into a pod, from all sources."""
    return (
        set(_VALUES["config"])
        | set(_VALUES["secrets"]["keys"].values())
        | _TLS_ENV
        | _helper_env_keys()
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


def test_chart_config_values_load_as_settings() -> None:
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

    Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


def test_chart_declares_only_the_documented_secrets() -> None:
    """The chart names exactly the plain secrets the architecture signed off — no fifth crept in.

    Everything else is workload-identity federation, i.e. no client secret at rest; a new plain
    secret is an architecture change (D-047), so it should not pass unnoticed.

    The knowledge-repo push credential is the fourth, added deliberately (gap DEP-2, D-088): the
    PR-gate submitter shells out to `git push`, and a git host authenticates that push with a token
    — there is no federated exchange for it the way there is for Entra-fronted APIs. Without the
    credential every agent-authored note fails at push in-cluster, so the choice is a declared
    secret or a knowledge layer that cannot write. It is recorded here rather than waved through.
    """
    assert set(_VALUES["secrets"]["keys"].values()) == {
        "CHEMCLAW_LLM_API_KEY",
        "CHEMCLAW_HPC_API_TOKEN",
        "CHEMCLAW_POSTGRES_DSN",
        "CHEMCLAW_KNOWLEDGE_REPO_TOKEN",
    }
