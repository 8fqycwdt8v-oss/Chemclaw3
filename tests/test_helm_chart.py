"""The Helm chart's configuration matches the app's `Settings`.

`make helm-validate` checks rendered YAML against Kubernetes schemas but cannot know whether
`CHEMCLAW_FOO` is a real setting. Two silent failure modes are closed here, offline, against the
`Settings` the pods construct:

1. A key that is not a field: pydantic-settings ignores an unknown prefixed environment variable,
   so the setting has no effect and no error.
2. A malformed value on a real field: every pod crashes at import.

These tests read the chart's source (`values.yaml` as YAML, `templates/` as text) and render
nothing, so a green run means "the template source says so", not "the cluster will see so".
Rendered-chart assertions live in `tests/test_deploy_chart.py`.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from chemclaw.core.config import Settings
from tests.siblings import SIBLING_SKIP, sibling_root

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "chemclaw"
_VALUES: dict[str, Any] = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))

# Env the chart injects outside the ConfigMap: the mTLS file paths (`_helpers.tpl`) and the secret
# refs. The pod sees these too, so a parity check that ignored them would miss half the surface.
_TLS_ENV = {"CHEMCLAW_TEMPORAL_TLS_CERT", "CHEMCLAW_TEMPORAL_TLS_KEY", "CHEMCLAW_TEMPORAL_TLS_CA"}

# Some `CHEMCLAW_*` envs are read by shell, not Python: the entrypoint dispatches on
# `CHEMCLAW_COMPONENT`, and `deploy/knowledge-sync.sh` takes its configuration from env. They are
# discovered from the scripts, so the exemption is never wider than what something reads.
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

    They arrive as literal `- name:` entries (mTLS paths, the knowledge-sync block), so they belong
    in the same parity check as ConfigMap keys.
    """
    template = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    return set(re.findall(r"name:\s*(CHEMCLAW_[A-Z0-9_]+)", template))


def _derived_config_keys() -> set[str]:
    """`CHEMCLAW_*` keys the ConfigMap computes rather than copying from `.Values.config`.

    `CHEMCLAW_NOTE_REPO_DIR` and `CHEMCLAW_CONNECTOR_URLS` are derived in `templates/config.yaml`;
    the latter is a `dict[str, str]` parsed from rendered JSON, the shape that crashes pods when
    wrong. Discovered from the template so a new derived key is covered.
    """
    template = (_CHART / "templates" / "config.yaml").read_text(encoding="utf-8")
    return set(re.findall(r"^\s*(CHEMCLAW_[A-Z0-9_]+):", template, flags=re.MULTILINE))


def _rendered_publish_path() -> str:
    """What `chemclaw.knowledgePublishPath` renders to under the chart's own values.

    Rendered from the helper's template text, since the claim is that the helper's path equals what
    `Settings.knowledge_path` evaluates over the same values.
    """
    template = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    # `[1]` starts mid-action (` -}}\n…`); drop through that closing delimiter to the body itself.
    define = template.split('define "chemclaw.knowledgePublishPath"')[1]
    body = define.split("-}}", 1)[1].split("{{- end -}}")[0]
    # `required "<message>" .Values.X` renders as `.Values.X` when X is set, so the wrapper is
    # stripped to keep asserting the path; the refusal itself is rendered with `helm` in
    # `tests/test_deploy_chart.py`.
    body = re.sub(r'\{\{ required "[^"]*" (\.Values\.[A-Za-z0-9_.]+) \}\}', r"{{ \1 }}", body)
    rendered = (
        body.replace("{{ .Values.knowledge.noteRepoPath }}", _VALUES["knowledge"]["noteRepoPath"])
        .replace(
            "{{ .Values.config.CHEMCLAW_KNOWLEDGE_DIR }}",
            _VALUES["config"]["CHEMCLAW_KNOWLEDGE_DIR"],
        )
        .strip()
    )
    assert "{{" not in rendered, f"unsubstituted template expression in the publish path: {body!r}"
    return rendered


def _rendered_derived_values() -> dict[str, str]:
    """What the ConfigMap's derived keys render to under the chart's own values.

    The helper's logic is reproduced here to stay offline; it feeds the `CHEMCLAW_CONNECTOR_URLS`
    JSON through `Settings`, so a render `dict[str, str]` cannot parse fails here.
    """
    # `cfg["url"]` wins where it is set: that bundle's server is hosted outside this release, so
    # there is no Service to compute an address from (`chemclaw.connectorUrls`).
    urls = {
        name: cfg.get("url") or f"http://chemclaw-connector-{name}:{_VALUES['connectorPort']}/mcp"
        for name, cfg in _VALUES["connectors"].items()
        if cfg.get("enabled") and cfg.get("server")
    }
    autoscaling = _VALUES["service"]["autoscaling"]
    calc = _VALUES["connectors"]["calc"]
    return {
        "CHEMCLAW_NOTE_REPO_DIR": _VALUES["knowledge"]["noteRepoPath"],
        "CHEMCLAW_CONNECTOR_URLS": json.dumps(urls),
        "CHEMCLAW_SERVICE_FLEET_REPLICAS": str(
            autoscaling["maxReplicas"] if autoscaling["enabled"] else _VALUES["service"]["replicas"]
        ),
        # `0` when this release runs no calc worker: it then dispatches no durable calculation, and
        # a rendered floor of 1 would refuse a deployment over work it never does.
        "CHEMCLAW_CALC_FLEET_WORKER_PROCESSES": str(
            calc.get("workerReplicas", calc.get("replicas"))
            if calc.get("enabled") and calc.get("worker")
            else 0
        ),
    }


def _connector_token_envs() -> set[str]:
    """`CHEMCLAW_*` bearer-token names read directly by name, never through a `Settings` field.

    Connector manifests name their bearer with `token_env`, read from `os.environ` per request by
    `connectors/identity.py::_EnvBearerAuth`; the `calc` backend's name is the value of
    `settings.calc_server_token_env`. Reused from `chemclaw.cli.validate_prose_contract`, which
    solves the same "is this name consumed" question.
    """
    from chemclaw.cli.validate_prose_contract import _connector_token_envs as _declared_names

    return {f"CHEMCLAW_{name.upper()}" for name in _declared_names()}


def _chart_env_keys() -> set[str]:
    """Every `CHEMCLAW_*` env name the chart puts into a pod, from all sources."""
    return (
        set(_VALUES["config"])
        | set(_VALUES["secrets"]["keys"].values())
        | set(_VALUES["secrets"]["optionalKeys"].values())
        | _TLS_ENV
        | _helper_env_keys()
        | _derived_config_keys()
        | {"CHEMCLAW_COMPONENT"}
    )


def test_no_values_key_is_declared_twice() -> None:
    """No values key is declared twice.

    Helm and `yaml.safe_load` both silently take the last duplicate, so an operator editing the
    first occurrence changes nothing. `yaml.compose()` keeps every key, so the check walks mapping
    nodes over the whole document.
    """
    duplicates: list[str] = []

    def walk(node: yaml.Node, path: str) -> None:
        if isinstance(node, yaml.MappingNode):
            seen: dict[str, int] = {}
            for key, value in node.value:
                if key.value in seen:
                    duplicates.append(
                        f"{path}.{key.value} (lines {seen[key.value]} "
                        f"and {key.start_mark.line + 1})"
                    )
                seen[key.value] = key.start_mark.line + 1
                walk(value, f"{path}.{key.value}")
        elif isinstance(node, yaml.SequenceNode):
            for index, value in enumerate(node.value):
                walk(value, f"{path}[{index}]")

    walk(yaml.compose((_CHART / "values.yaml").read_text(encoding="utf-8")), "values")
    assert not duplicates, (
        "values.yaml declares a key twice; every parser silently keeps the last, so an edit to the "
        f"other one is discarded with no error anywhere: {duplicates}"
    )


def test_chart_config_keys_have_a_consumer() -> None:
    """Every `CHEMCLAW_*` key the chart injects has a reader.

    A `Settings` field, a deploy script, or a connector's bearer lookup; anything else is silently
    ignored by pydantic-settings as an environment variable.
    """
    orphans = {
        key
        for key in _chart_env_keys()
        if _field_for(key) not in Settings.model_fields
        and key not in _SHELL_CONSUMED_ENV
        and key not in _connector_token_envs()
        and key not in _MOUNTED_BUNDLE_TOKENS
    }
    assert not orphans, f"chart sets env nothing reads: {sorted(orphans)}"


#: Bearer slots for fleet bundles this image does not ship, reaching a pod only through
#: `extraConnectors`. Agreement with the fleet's manifest is asserted in
#: `tests/test_sibling_manifest_agreement.py`.
_MOUNTED_BUNDLE_TOKENS = frozenset({"CHEMCLAW_PYEXEC_TOKEN"})


def test_chart_config_values_load_as_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chart's own values construct a valid `Settings`: the pods' boot path, proven offline.

    Models the pod environment: the ConfigMap block plus placeholders for secret-provided keys and
    mounted mTLS paths. Secret keys no field claims (the knowledge repo's push credential, consumed
    by `deploy/knowledge-sync.sh`) are skipped, since passing them as init kwargs would fail
    `extra="forbid"` for a correct configuration.
    """
    overrides = {_field_for(key): str(value) for key, value in _VALUES["config"].items()}
    for env_key in _VALUES["secrets"]["keys"].values():
        if _field_for(env_key) in Settings.model_fields:
            overrides.setdefault(_field_for(env_key), "placeholder")
    mount = _VALUES["secrets"]["temporalTls"]["mountPath"]
    for env_key, filename in zip(sorted(_TLS_ENV), ["ca.crt", "tls.crt", "tls.key"], strict=True):
        overrides[_field_for(env_key)] = f"{mount}/{filename}"
    overrides["postgres_dsn"] = (
        # sslmode=verify-full because the chart enforces identity (entra_required=true), under which
        # a non-loopback DSN must state TLS — the security-review guard rejects a plaintext-capable
        # DSN in that posture. A production secret must carry the same (documented in the runbook).
        "postgresql://chemclaw:chemclaw@postgres:5432/chemclaw?sslmode=verify-full"
    )
    # Derived keys are passed as environment, as the pod receives them: pydantic-settings
    # JSON-decodes a complex field from env but not from an init kwarg.
    for env_key, value in _rendered_derived_values().items():
        monkeypatch.setenv(env_key, value)

    loaded = Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]
    # Asserted, not merely constructed: `connector_urls` is the one derived value whose *shape*
    # matters, and a JSON render that produced a list or a nested object would still construct
    # some `Settings` while pointing the front door at nothing.
    assert loaded.connector_urls, "the chart renders no connector URLs; every bundle is unreachable"
    assert all(url.startswith("http") for url in loaded.connector_urls.values())


def test_chart_declares_only_the_documented_secrets() -> None:
    """The chart names exactly the plain secrets the architecture signed off.

    The list is written out rather than derived, so each addition is argued here. In brief:

    - `keys` are rendered as required `secretKeyRef`s on every pod: credentials whose absence
      silently
      breaks a capability (LLM, database, knowledge-repo push). The push token is
      required only where a remote is configured.
    - `optionalKeys` exist because the Secret is operator-managed and predates a new chart version,
      so
      a required addition would break every pod on `helm upgrade`. They include the framing envelope
      key (unset means a per-process tag, unshared across replicas), the connector bearers (fail
      closed with `MissingConnectorCredential` when unset), credentials for features off by default
      (LLM fallback, vector store, Temporal Cloud, a split session store), the MCP face bearer (the
      face refuses without it), and bearers for bundles that ship off.
    - Credentials never go in `config`, which renders into a ConfigMap the `view` role can read.

    Both maps are asserted, because splitting the answer across two values is how a key ends up in
    neither.
    """
    assert set(_VALUES["secrets"]["keys"].values()) == {
        "CHEMCLAW_LLM_API_KEY",
        "CHEMCLAW_POSTGRES_DSN",
        "CHEMCLAW_KNOWLEDGE_REPO_TOKEN",
    }
    assert set(_VALUES["secrets"]["optionalKeys"].values()) == {
        "CHEMCLAW_BO_MCP_TOKEN",
        "CHEMCLAW_CALC_MCP_TOKEN",
        "CHEMCLAW_MOLFP_MCP_TOKEN",
        "CHEMCLAW_RXNFP_MCP_TOKEN",
        "CHEMCLAW_FRAMING_ENVELOPE_SECRET",
        "CHEMCLAW_CHEM_TOKEN",
        "CHEMCLAW_SAFETY_TOKEN",
        "CHEMCLAW_CALC_TOKEN",
        "CHEMCLAW_RXNPREDICT_TOKEN",
        "CHEMCLAW_RXNLABEL_TOKEN",
        "CHEMCLAW_LLM_FALLBACK_API_KEY",
        "CHEMCLAW_VECTOR_STORE_API_KEY",
        "CHEMCLAW_TEMPORAL_API_KEY",
        "CHEMCLAW_SESSION_STORE_DSN",
        "CHEMCLAW_MCP_FACE_TOKEN",
        "CHEMCLAW_PROPS_TOKEN",
        "CHEMCLAW_THERMALSAFETY_TOKEN",
        "CHEMCLAW_KINETICS_TOKEN",
        "CHEMCLAW_UNITOPS_TOKEN",
        "CHEMCLAW_SUITABILITY_TOKEN",
        "CHEMCLAW_PYEXEC_TOKEN",
    }


def test_the_migration_credential_is_mounted_on_the_hook_job_and_nowhere_else() -> None:
    """The migration credential is mounted on the hook Job and nowhere else.

    It owns the schema and, under a split principal, is the only role that can rewrite
    `audit_events`. `chemclaw.env` is on every Deployment, so it has its own map and helper used
    only by the hook Job.
    """
    assert set(_VALUES["secrets"]["migrationKeys"].values()) == {"CHEMCLAW_POSTGRES_MIGRATION_DSN"}
    assert not (
        set(_VALUES["secrets"]["migrationKeys"].values()) & set(_VALUES["secrets"]["keys"].values())
    ), "the migration credential is also in the map every Deployment mounts"

    helpers = (_CHART / "templates" / "_helpers.tpl").read_text()
    _, _, migration_env = helpers.partition('define "chemclaw.migrationEnv"')
    assert migration_env, "no chemclaw.migrationEnv helper"
    # Optional, or a single-principal deployment (every dev database, CI, `make up`) cannot start.
    assert "optional: true" in migration_env.split("{{- end -}}")[0]

    for template in sorted((_CHART / "templates").glob("*.yaml")):
        if template.name == "migrate-job.yaml":
            assert 'include "chemclaw.migrationEnv"' in template.read_text()
        else:
            assert 'include "chemclaw.migrationEnv"' not in template.read_text(), (
                f"{template.name} mounts the migration credential; only the hook Job may"
            )


def test_the_document_share_is_read_only_and_only_on_the_worker_that_crawls_it() -> None:
    """The document share is read-only and only on the worker that crawls it.

    `readOnly` on volume and mount makes "never writes to a site's share" kubelet-enforced, and only
    the background worker needs it. No Secret: the CIFS credential belongs to the PersistentVolume
    and is read by the CSI driver.
    """
    share = _VALUES["documentShare"]
    assert share["enabled"] is False, "a share nobody declared must not be crawled by default"

    helpers = (_CHART / "templates" / "_helpers.tpl").read_text()
    for helper in ("chemclaw.documentShareMount", "chemclaw.documentShareVolume"):
        _, _, body = helpers.partition(f'define "{helper}"')
        assert body, f"no {helper} helper"
        assert "readOnly: true" in body.split("{{- end -}}")[0], helper

    for template in sorted((_CHART / "templates").glob("*.yaml")):
        rendered = template.read_text()
        if template.name == "deployment-workers.yaml":
            assert 'include "chemclaw.documentShareMount"' in rendered
            assert 'include "chemclaw.documentShareVolume"' in rendered
        else:
            assert "documentShare" not in rendered, (
                f"{template.name} mounts the file share; only the background worker crawls it"
            )


def _hook_documents() -> dict[str, str]:
    """`migrate-job.yaml`'s two Job documents, keyed by their component label.

    Split on the document separator, since the Go template cannot be YAML-parsed.
    """
    text = (_CHART / "templates" / "migrate-job.yaml").read_text()
    documents = {}
    for chunk in re.split(r"^---$", text, flags=re.MULTILINE):
        component = re.search(r"app\.kubernetes\.io/component:\s*(\S+)", chunk)
        if component:
            documents[component.group(1)] = chunk
    return documents


def _entrypoint_case(component: str) -> str:
    """The body of `deploy/entrypoint.sh`'s `case` branch for `component`.

    A chart `command:` would replace the image `ENTRYPOINT` and skip arming the compiled egress
    layer, so the hook Jobs name a component and what they run lives in the script.
    """
    script = (_CHART.parents[1] / "entrypoint.sh").read_text(encoding="utf-8")
    body = script.split(f"\n  {component})\n", 1)
    assert len(body) == 2, f"entrypoint.sh has no `{component})` case"
    return body[1].split("\n    ;;", 1)[0]


def test_the_pre_upgrade_hook_migrates_then_reconciles_grants() -> None:
    """The pre-upgrade hook migrates, sets up the agent store, then reconciles grants, in order.

    The grants name tables the earlier steps create, including `store`/`store_migrations`, which
    upstream creates at runtime (`chemclaw.agent.store_setup`). One container under `set -e`, so a
    failed step stops the sequence. Asserted as the exact list, so a new step needs a decision about
    where it goes; `tests/test_database_privileges.py` holds the reasoning for the order.
    """
    documents = _hook_documents()
    migrate = " ".join(documents["migrate"].split())
    assert 'name: CHEMCLAW_COMPONENT value: "migrate"' in migrate, migrate
    assert "command:" not in migrate, (
        "the DDL Job declares its own `command:` again, which replaces the image ENTRYPOINT and so "
        "starts it with the compiled egress layer unarmed"
    )
    case = _entrypoint_case("migrate")
    steps = [line for line in case.splitlines() if "python -m" in line]
    assert [step.split("python -m ")[1].strip() for step in steps] == [
        "chemclaw.core.migrate",
        "chemclaw.agent.store_setup",
        "chemclaw.core.grants",
    ], case
    assert "chemclaw.agent.message_migration" not in case, (
        "the data conversion is back in the pre-upgrade hook, where it rewrites rows the previous "
        "release is still serving"
    )


def test_the_ddl_runs_before_the_rollout_and_the_data_conversion_after_it() -> None:
    """The DDL runs before the rollout and the data conversion after it.

    Additive DDL is safe for the running release; rewriting `session_messages` into a shape the old
    reader rejects is not, so it runs `post-upgrade` and a failed rollout converts nothing.
    Credentials are checked per document: the converter runs as the runtime role and must not mount
    the schema-owning DSN.
    """
    documents = _hook_documents()
    assert set(documents) == {"migrate", "convert"}, documents.keys()

    assert '"helm.sh/hook": pre-install,pre-upgrade' in documents["migrate"]
    assert '"helm.sh/hook-weight": "-5"' in documents["migrate"]

    convert = documents["convert"]
    assert '"helm.sh/hook": post-install,post-upgrade' in convert
    assert '"helm.sh/hook-weight": "5"' in convert
    assert 'value: "convert"' in convert, convert
    assert "chemclaw.agent.message_migration" in _entrypoint_case("convert")
    assert 'include "chemclaw.migrationEnv"' not in convert, (
        "the conversion Job mounts the credential that owns the schema and can rewrite the audit "
        "trail; it issues no DDL and does not need it"
    )

    # A hook Helm waits on with no deadline leaves the release `pending-upgrade` — the argument
    # `migrateJob.activeDeadlineSeconds` was added for, and it applies to a post hook identically.
    assert re.search(r"^\s*activeDeadlineSeconds:", convert, flags=re.MULTILINE), convert
    bounds = _VALUES["convertJob"]
    assert bounds["activeDeadlineSeconds"] > bounds["backoffLimit"] * 60, (
        "the deadline leaves no room for the retries the same Job is configured to make"
    )


def _settings_from_chart(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """`Settings` as the pods build them, from the chart's own values.

    The tests below ask whether what a value switches on actually works, not only whether it loads.
    """
    overrides = {_field_for(key): str(value) for key, value in _VALUES["config"].items()}
    for env_key in _VALUES["secrets"]["keys"].values():
        if _field_for(env_key) in Settings.model_fields:
            overrides.setdefault(_field_for(env_key), "placeholder")
    mount = _VALUES["secrets"]["temporalTls"]["mountPath"]
    for env_key, filename in zip(sorted(_TLS_ENV), ["ca.crt", "tls.crt", "tls.key"], strict=True):
        overrides[_field_for(env_key)] = f"{mount}/{filename}"
    overrides["postgres_dsn"] = (
        # sslmode=verify-full because the chart enforces identity (entra_required=true), under which
        # a non-loopback DSN must state TLS — the security-review guard rejects a plaintext-capable
        # DSN in that posture. A production secret must carry the same (documented in the runbook).
        "postgresql://chemclaw:chemclaw@postgres:5432/chemclaw?sslmode=verify-full"
    )
    for env_key, value in _rendered_derived_values().items():
        monkeypatch.setenv(env_key, value)
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


def test_the_chart_publishes_the_graph_where_settings_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chart publishes the knowledge graph where `Settings` reads it.

    A mismatch fails silently: `load_notes` over a missing directory yields nothing, so every
    question gets zero graph evidence. Asserted against `Settings` so `noteRepoPath`,
    `CHEMCLAW_KNOWLEDGE_DIR` and the helper must all still name one place.
    """
    chart = _settings_from_chart(monkeypatch)
    published = _rendered_publish_path()

    assert published == str(chart.knowledge_path), (
        f"the chart publishes the knowledge graph to {published} and every reader resolves "
        f"{chart.knowledge_path} — a graph published where nothing reads it is answered as "
        "'no evidence', silently"
    )
    # And the sync containers take the path from the helper rather than from a value of their own,
    # which is what stops the two from drifting apart again.
    helpers = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    sync_env = helpers.split('define "chemclaw.knowledgeSyncEnv"')[1].split("{{- end -}}")[0]
    assert 'include "chemclaw.knowledgePublishPath"' in sync_env, (
        "CHEMCLAW_KNOWLEDGE_PUBLISH_DIR names a path of its own again"
    )
    # And that path is inside a volume the reading pods actually mount: `chemclaw.knowledgeMounts`
    # mounts `noteRepoPath`, so the published tree is on a real volume rather than the container's
    # ephemeral layer.
    mounts = helpers.split('define "chemclaw.knowledgeMounts"')[1].split("{{- end -}}")[0]
    assert 'include "chemclaw.noteRepoMount"' in mounts, (
        "the published tree is no longer inside a mounted volume"
    )
    assert published.startswith(_VALUES["knowledge"]["noteRepoPath"] + "/")


def test_a_config_change_restarts_the_pods_that_read_it() -> None:
    """Every pod template carries a ConfigMap checksum, so a config change restarts the pods that
    read it.

    Environment is read once at start; without the annotation `helm upgrade` updates the ConfigMap
    and no running pod, and later scale-ups split the fleet across two configurations. Counted per
    pod template, since `deployment-connectors.yaml` holds two.
    """
    expected = {
        "deployment-service.yaml": 1,
        "deployment-workers.yaml": 1,
        "deployment-connectors.yaml": 2,
        "deployment-interactive-workers.yaml": 1,
    }
    for filename, pod_templates in expected.items():
        text = (_CHART / "templates" / filename).read_text(encoding="utf-8")
        found = text.count('include "chemclaw.configChecksum"')
        assert found == pod_templates, (
            f"{filename}: {found} pod templates carry checksum/config, expected {pod_templates}"
        )
    helpers = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    body = helpers.split('define "chemclaw.configChecksum"')[1].split("{{- end -}}")[0]
    # The hash must be over the rendered ConfigMap template. A checksum of anything else (a values
    # subtree, a constant) annotates the pod without tracking what it is supposed to track.
    assert '"/config.yaml"' in body and "sha256sum" in body


def test_the_temporal_mtls_paths_are_gated_on_the_secret_the_chart_asks_for() -> None:
    """The Temporal mTLS paths, volume and mount are gated together on the Secret.

    `_tls_config()` reads the files whenever any path is set, so unconditional paths over an
    optional mount fail with a bare `FileNotFoundError`, and the plaintext path is unreachable. Both
    halves are
    asserted: the gate, and a non-optional mount so a missing Secret fails at admission.
    """
    helpers = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    gate = "{{- if .Values.secrets.temporalTls.enabled }}"

    env = helpers.split('define "chemclaw.env"')[1].split("{{- end -}}")[0]
    tls_block = env.split("CHEMCLAW_TEMPORAL_TLS_CERT")[0]
    assert gate in tls_block, "the mTLS paths are exported whether or not the Secret exists"

    mount = helpers.split('define "chemclaw.tlsMount"')[1].split("{{- end -}}")[0]
    assert gate in mount, "the mTLS mount is unconditional while its env is not"

    volumes = helpers.split('define "chemclaw.volumes"')[1].split("{{- end -}}")[0]
    assert gate in volumes, "the mTLS volume is unconditional while its env is not"
    assert "optional: true" not in volumes, (
        "an optional mTLS Secret turns a missing Secret into a FileNotFoundError inside a Temporal "
        "connect instead of a pod event naming the Secret"
    )
    assert _VALUES["secrets"]["temporalTls"]["enabled"] is True, (
        "the chart describes an in-cluster Temporal with mTLS (D-049); switching this off by "
        "default would ship a plaintext durable core"
    )


def test_the_public_route_carries_the_only_control_that_bounds_it() -> None:
    """The public Route carries the only control that bounds `/metrics`.

    A NetworkPolicy selects peers, not paths, and the Route publishes every path, so the control at
    this layer is a source-CIDR allowlist on the Route. Empty by default: the chart cannot know a
    deployment's ranges, and the declared-label allowlist is what makes the default acceptable.
    """
    route = (_CHART / "templates" / "service-route.yaml").read_text(encoding="utf-8")
    assert "haproxy.router.openshift.io/ip_whitelist" in route, (
        "the Route offers no way to bound who reaches it, and no path control exists at this layer"
    )
    assert ".Values.route.ipWhitelist" in route, "the allowlist is a literal, not a value"
    assert _VALUES["route"]["ipWhitelist"] == []

    policy = (_CHART / "templates" / "networkpolicy.yaml").read_text(encoding="utf-8")
    assert "keeps it inside the cluster" not in policy, (
        "the NetworkPolicy comment claims to contain `/metrics` again; it selects peers, not paths"
    )


def test_the_shipped_budget_guard_actually_refuses_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped budget guard actually refuses a turn past a cap.

    Off is the right code default for CLIs and tests; a deployment serving users needs it on because
    the number of turns is unbounded. Executed rather than read off the flag, since enabled with
    every cap at 0 parses and guards nothing.
    """
    from chemclaw.api.budget import BudgetTracker

    chart = _settings_from_chart(monkeypatch)
    assert chart.budget_enabled, "the chart no longer enables the runaway-cost guard"
    monkeypatch.setattr("chemclaw.api.budget.settings", chart)

    tracker = BudgetTracker()
    tracker.record("s1", "alice", tokens=chart.budget_max_tokens_per_session)
    with pytest.raises(Exception) as refused:
        asyncio.run(tracker.check("s1", "alice"))
    assert "budget" in str(refused.value).lower() or "cap" in str(refused.value).lower()


def test_the_chart_states_its_privileged_roles_rather_than_omitting_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chart states its privileged roles rather than omitting them.

    An expensive job's trigger gate fails closed on an empty role set, so with Entra required and no
    privileged role every expensive job is refused while the pod looks healthy. That is intended
    (the chart cannot know an organisation's role names), but present-and-empty shows in `helm show
    values`, the ConfigMap and values diffs, where an absent key does not. Both halves are pinned.
    """
    from chemclaw.agent.authz import AuthorizationError, authorize_trigger
    from chemclaw.connectors.registry import enabled
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity

    key = "CHEMCLAW_ENTRA_PRIVILEGED_ROLES"
    assert key in _VALUES["config"], (
        f"{key} is not declared in the chart, so the deployment's most consequential silent "
        "failure is invisible in `helm show values`, in the ConfigMap and in a values diff"
    )

    chart = _settings_from_chart(monkeypatch)
    assert chart.entra_required, "the shipped chart no longer enforces identity; re-read this test"
    assert chart.entra_privileged_role_set == frozenset(), (
        "the chart now names a privileged role. If that is deliberate, it must be a role the "
        "target tenant really grants — a placeholder here authorizes nobody while looking "
        "configured, which is the failure this test exists to prevent"
    )

    declared = {job.name for manifest in enabled() for job in manifest.jobs if job.expensive}
    assert declared, "no enabled bundle declares an expensive job; this test would prove nothing"

    # Patched where `authorize_trigger` reads it: `authz` does `from ... import settings`, so it
    # holds its own binding and patching the config module would leave the gate on the real one.
    monkeypatch.setattr("chemclaw.agent.authz.settings", chart)
    token = set_current_identity("chemist-1", frozenset({"process-chemist"}))
    try:
        for job in sorted(declared):
            with pytest.raises(AuthorizationError, match="privileged role"):
                authorize_trigger(job)
    finally:
        reset_current_identity(token)


def test_no_secret_is_carried_in_the_plaintext_config_map() -> None:
    """No secret is carried in the plaintext ConfigMap.

    `.Values.config` renders into a ConfigMap readable by the `view` role. Checked against the
    redaction inventory (`_SECRET_SETTINGS`), the codebase's own list of credential settings, which
    also catches non-obvious ones like the framing envelope key.
    """
    from chemclaw.core.logging import _SECRET_SETTINGS

    exposed = sorted(key for key in _VALUES["config"] if _field_for(key) in set(_SECRET_SETTINGS))
    assert not exposed, (
        f"these settings hold a credential (they are in _SECRET_SETTINGS) but are declared in "
        f".Values.config, which renders into a plaintext ConfigMap: {exposed}. Move each to "
        "secrets.keys and argue it in test_chart_declares_only_the_documented_secrets."
    )


def test_the_verifier_opt_in_is_documented_in_the_values_file() -> None:
    """The commented-out `CHEMCLAW_VERIFIER_*` opt-in block is pinned as text.

    The verifier ships off, so the documented block (naming the startup capability probe and the
    review band) is the opt-in surface. Checked on raw text because the keys are comments; that they
    are absent from parsed config is asserted too, so documentation cannot switch the judge on.
    """
    text = (_CHART / "values.yaml").read_text(encoding="utf-8")
    for key in ("CHEMCLAW_VERIFIER_ENABLED", "CHEMCLAW_VERIFIER_CONFIDENCE_THRESHOLD"):
        assert f"# {key}" in text, f"the commented opt-in for {key} left values.yaml"
        assert key not in _VALUES["config"], f"{key} must stay a documented opt-in, not a default"
    assert "require_verifier_capability" in text, (
        "the comment must name the startup probe a deployer will hit"
    )


def test_every_credential_this_deployment_holds_has_a_secret_slot() -> None:
    """Every credential this deployment holds has a Secret slot.

    A credential in none of `keys`, `optionalKeys` or `migrationKeys` can only be set through the
    ConfigMap, which the test above refuses. Driven off `Settings`, so a new credential arrives here
    rather than at a deployment; the log filter's equivalent is
    `tests/test_credentials.py::test_every_credential_shaped_setting_is_in_the_redaction_inventory`.
    """
    from tests.test_credentials import _credential_shaped

    slots = {
        name
        for section in ("keys", "optionalKeys", "migrationKeys")
        for name in _VALUES["secrets"][section].values()
    }
    # Plus every credential the config names by *variable* rather than by value: a `*_token_env`
    # field holds the name of the variable a bearer is read from, so the slot the chart owes is
    # that name. `calc_server_token_env` is the worked example already in `optionalKeys`.
    wanted = {f"CHEMCLAW_{name.upper()}" for name in _credential_shaped(Settings)} | {
        str(field.default)
        for name, field in Settings.model_fields.items()
        if name.endswith("_token_env")
    }
    # `live_probe_token` is minted by the live lane for a client; no pod reads it.
    wanted -= {"CHEMCLAW_LIVE_PROBE_TOKEN"}
    assert wanted <= slots, f"credentials with no Secret slot: {sorted(wanted - slots)}"


def test_the_labelling_server_is_addressable_from_the_chart() -> None:
    """The labelling server is addressable from the chart.

    A `reaction-labels` Schedule exists for any source providing reactions, and the client's default
    address is loopback, which in a cluster is the worker's own pod: the corpus would never be
    labelled and precedent questions would answer from an empty index. The egress port is asserted
    with the URL, since a NetworkPolicy rule restricts by port independently of its peers.
    """
    url = _VALUES["config"].get("CHEMCLAW_RXNLABEL_SERVER_URL")
    assert url, "the chart states no address for the labelling server"
    assert "127.0.0.1" not in url, f"the chart ships the dev loopback address: {url}"

    port = _VALUES["networkPolicy"]["egressPorts"].get("rxnlabel")
    assert port, "networkPolicy.egressPorts names no rxnlabel port"
    assert str(port) in url, f"the egress port {port} is not the port the URL dials ({url})"

    # That the entry is emitted is asserted on the rendered NetworkPolicy in
    # `tests/test_deploy_chart.py::test_every_declared_egress_port_reaches_the_rendered_policy`.


def test_every_externally_hosted_connector_can_actually_be_dialled() -> None:
    """Every externally hosted connector can actually be dialled.

    A `connectors.<name>.url` needs a matching `networkPolicy.egressPorts` entry, or packets are
    dropped even with the host in `egressDestinations`; that the rule emits it is asserted on the
    rendered object (`tests/test_deploy_chart.py`). Derived from the values file so the next bundle
    is covered; `rxnlabel` is checked separately because its address is a `config` key, not a
    connector.
    """
    ports = _VALUES["networkPolicy"]["egressPorts"]
    external = {name: cfg["url"] for name, cfg in _VALUES["connectors"].items() if cfg.get("url")}
    assert external, "no externally-hosted connector found; this test would assert nothing"
    for name, url in external.items():
        dialled = re.search(r":(\d+)", url.split("//", 1)[-1])
        assert dialled, f"{name}: url {url!r} names no port"
        assert name in ports, (
            f"{name} is dialled at {url} and networkPolicy.egressPorts has no `{name}` entry, so "
            "every packet to it is dropped whatever egressDestinations says"
        )
        assert str(ports[name]) == dialled.group(1), (
            f"{name}: egressPorts.{name} is {ports[name]} but the url dials {dialled.group(1)}"
        )


def _fleet_addresses() -> dict[str, tuple[str, str, int]]:
    """Every address in `values.yaml` that names a `Chemclaw3-mcp` server, by where it is declared.

    Each entry is `(service name, whole host, port)`. Hosts are matched loosely
    (`chemclaw<digits?>-mcp-`) so a misspelling is picked up and checked rather than silently
    excluded. Only the first label is the Service name.
    """
    found: dict[str, tuple[str, str, int]] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            match = re.match(r"https?://(chemclaw[0-9]*-mcp-[a-z0-9-]+)(?:\.[^:/]+)?:(\d+)", node)
            if match:
                host = match.group(1)
                found[path] = (host.split(".", 1)[0], host, int(match.group(2)))

    walk(_VALUES, "")
    return found


def _fleet_services(checkout: Path) -> dict[str, int]:
    """Every Service the sibling fleet actually creates, name onto port.

    Read from `servers/*/deploy/service.yaml`, which the fleet applies untransformed.
    """
    services: dict[str, int] = {}
    for manifest in sorted(checkout.glob("servers/*/deploy/service.yaml")):
        document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        services[document["metadata"]["name"]] = int(document["spec"]["ports"][0]["port"])
    return services


def test_every_fleet_address_names_a_service_the_sibling_actually_creates() -> None:
    """Every fleet address names a Service the sibling actually creates.

    A wrong name is an NXDOMAIN the probes never see; `CHEMCLAW_CALC_SERVER_URL` is dialled for
    every calculation. Without the sibling checkout this asserts nothing and says which addresses it
    did not check, so a skip does not read as a pass.
    """
    addresses = _fleet_addresses()
    assert addresses, (
        "no fleet address found in values.yaml — either the chart stopped naming "
        "Chemclaw3-mcp's servers, or `_fleet_addresses` no longer matches how it names them"
    )

    checkout, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if checkout is None:
        listed = ", ".join(f"{path}={host}:{port}" for path, (_, host, port) in addresses.items())
        # `SIBLING_SKIP` so the epilogue counts it — `reason` alone does not carry the marker.
        pytest.skip(
            f"{SIBLING_SKIP} {reason}; NOT checked against the fleet's own Services: {listed}"
        )

    services = _fleet_services(checkout)
    assert services, f"{checkout} declares no servers/*/deploy/service.yaml to compare against"

    wrong: list[str] = []
    for path, (service, host, port) in sorted(addresses.items()):
        if service not in services:
            wrong.append(
                f"values.yaml `{path}` dials {host}:{port}, and {checkout} creates no Service "
                f"named {service!r} — it creates {sorted(services)}"
            )
        elif services[service] != port:
            wrong.append(
                f"values.yaml `{path}` dials {host}:{port}, and {checkout}'s Service "
                f"{service!r} serves port {services[service]}"
            )
    assert not wrong, "\n".join(wrong)
