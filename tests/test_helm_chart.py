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


def _rendered_publish_path() -> str:
    """What `chemclaw.knowledgePublishPath` renders to under the chart's own values.

    Rendered from the template text rather than recomputed from `values.yaml`, because the claim
    being tested is about the *helper*: that the directory the knowledge-sync containers write to is
    the expression `Settings.knowledge_path` evaluates, over the same two values the ConfigMap hands
    the pods. Recomputing it here would only prove that two lines of this file agree.
    """
    template = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    # `[1]` starts mid-action (` -}}\n…`); drop through that closing delimiter to the body itself.
    define = template.split('define "chemclaw.knowledgePublishPath"')[1]
    body = define.split("-}}", 1)[1].split("{{- end -}}")[0]
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
    """The chart names exactly the plain secrets the architecture signed off.

    Deliberately without a number: the count belongs in the assertion below, not in a sentence
    describing it (D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose).

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


def test_the_migration_credential_is_mounted_on_the_hook_job_and_nowhere_else() -> None:
    """A credential that can rewrite the audit trail must not live on a pod for its whole life.

    The migration DSN owns the schema: it issues DDL, and under a split principal it is the only
    role that can `UPDATE` or `DELETE` `audit_events`
    (D-2026-08-05-append-only-by-grant-not-by-contract). `chemclaw.env` is included by every
    Deployment, so listing it in `secrets.keys` would mount it on the front door and every worker
    permanently — which would leave the exposure exactly where it was while appearing to fix it.
    Hence a second map and a second helper, used only by the hook Job.
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
    """The share is mounted, never called — and never written to.

    Two claims the chart has to make true rather than merely intend. `readOnly` on both the volume
    and the mount is what makes "this system never writes to a site's file share" enforced by the
    kubelet instead of by a promise in a docstring. And only the background worker gets it: the
    front door answers from the index, so a mount there would be attack surface bought for nothing.

    No entry appears under `secrets` because there is none to add — the CIFS mount credential
    belongs to the PersistentVolume and is read by the CSI driver, which is the whole point of
    mounting the share instead of speaking SMB from Python.
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


def test_the_hook_job_reconciles_grants_after_it_migrates() -> None:
    """The grants name tables the migrations create, so the order is not optional.

    One container rather than a second hook Job, so the ordering is the shell's `&&` rather than a
    weight in a different file — and so a failed migration is never followed by a grant run.
    """
    job = (_CHART / "templates" / "migrate-job.yaml").read_text()
    assert "chemclaw.core.migrate && python -m chemclaw.core.grants" in job


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


def test_the_chart_publishes_the_graph_where_settings_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synced knowledge tree lands on the path every reader resolves — one directory, not two.

    This is the inverse of a parity check and the reason it is needed: the chart used to declare
    `knowledge.publishPath: /app/knowledge` and mount an `emptyDir` there, while `Settings` resolved
    `knowledge_path` as `note_repo_dir / knowledge_dir` = `/var/lib/chemclaw/note-repo/knowledge`.
    Both paths were valid, both were mounted, and nothing compared them — this file *modelled* the
    mismatch (`_rendered_derived_values` fed the real `CHEMCLAW_NOTE_REPO_DIR` into `Settings`) and
    then asserted nothing about where the sync wrote.

    Nothing failed, which is the whole problem. `load_notes` `rglob`s a directory that does not
    exist, yields nothing and raises nothing, so the default install answered every question with
    zero knowledge-graph evidence and reported it nowhere; the configured install published merges
    into a directory no process read. A missing note is not an error, it is just less evidence.

    Asserted against `Settings` rather than against a literal, because the two ends must move
    together: change `noteRepoPath`, `CHEMCLAW_KNOWLEDGE_DIR` or the helper, and this fails unless
    all of them still name one place.
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
    """Every pod template carries a ConfigMap checksum, or a `helm upgrade` changes nothing.

    Non-secret config reaches a pod only through `envFrom: configMapRef`, and environment is read
    once at process start. Without an annotation derived from the ConfigMap, `helm upgrade` with a
    new `CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_ENTRA_REQUIRED`, `CHEMCLAW_BUDGET_ENABLED` or rate limit
    updated the ConfigMap, reported success, and applied to no running pod. With the HPA on by
    default the next scale-up then started pods that *did* read the new values — a fleet split
    across two configurations with no signal anywhere.

    Counted per pod template rather than checked per file: `deployment-connectors.yaml` holds two
    (the bundle's server and its worker), and a checksum on one of them is the same silent
    half-rollout in miniature.
    """
    expected = {
        "deployment-service.yaml": 1,
        "deployment-workers.yaml": 1,
        "deployment-connectors.yaml": 2,
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
    """The three PEM paths, the volume and the mount are one switch — or none of them are.

    `_tls_config()` short-circuits only when all three settings are empty, so exporting the paths
    unconditionally means `read_bytes()` on files that may not exist. The chart did exactly that
    against a Secret it never creates, mounted `optional: true`: a deployment without
    `chemclaw-temporal-tls` got `FileNotFoundError: /etc/temporal/tls/tls.crt` from the post-install
    hook Job — naming neither Temporal nor a Secret — and a worker crash loop, while the front door
    passed both probes because `/readyz` never touches Temporal. No value could turn the env off, so
    the plaintext path `connect_options()` documents was unreachable from the chart at any value.

    Both halves are asserted because either alone reintroduces a version of the bug: `optional:
    true` without a gate is the silent `FileNotFoundError`, and a gate that leaves the mount
    optional turns a missing Secret back into a runtime failure instead of an admission one.
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
    """`/metrics` is on the external host, and the chart says so instead of naming a rule.

    The compensating control was asserted in four places — `api/app.py`, this chart's NetworkPolicy
    comment, an ADR and a test — and held in none: a NetworkPolicy selects peers, not paths, and the
    front door's Route declares no `spec.path`, so every path the app serves is published wherever
    the Route is. The ingress rule *has* to allow the router; that is not containment.

    What is asserted here is the control that does exist at this layer — a source-CIDR allowlist on
    the Route — and that the chart no longer claims the other one. Empty by default, because a chart
    cannot invent a deployment's corporate ranges, and because what makes the endpoint acceptable by
    default is D-152's declared-label allowlist rather than anything in `deploy/`.
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
    from chemclaw.durable import schedules as schedules_module

    chart = _settings_from_chart(monkeypatch)
    assert chart.session_store == "postgres", "no durable audit sink; the precondition changed"
    monkeypatch.setattr(schedules_module, "settings", chart)

    planned = {job.schedule_id for job in schedules_module.planned_schedules()}
    assert "audit-verify" in planned, (
        "the shipped configuration plans no audit-chain verification, so the GxP hash chain is "
        "only ever checked by someone remembering to run `make audit-verify`"
    )


def test_the_chart_states_its_privileged_roles_rather_than_omitting_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped chart closes every expensive job, and has to *say* so where an operator looks.

    `expensive: true` in a connector manifest derives into the trigger gate, and that gate fails
    closed on an empty role set. So under the shipped `CHEMCLAW_ENTRA_REQUIRED=true` with no
    privileged role, `compute_dft_energy` and its three siblings are refused for every authenticated
    user — while the pod boots, both probes pass, and reads work. There is no crash to notice.

    That is the intended posture (the chart cannot know an organization's role names, and inventing
    a plausible one would ship a config that *looks* configured, grants nothing, and points the
    operator at Entra group membership instead of at `values.yaml`). What is not acceptable is
    reaching it by omission: an absent key appears in neither `helm show values`, nor the rendered
    ConfigMap, nor an operator's values diff, so "nothing works and nobody knows why" is the whole
    user experience. Present-and-empty appears in all three.

    So this pins the two halves together — the key is declared, it is empty, and empty means every
    declared-expensive job is refused — because either half alone is a fact about nothing.
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
