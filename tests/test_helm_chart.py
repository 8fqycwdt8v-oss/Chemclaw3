"""The Helm chart's configuration matches the app's `Settings` (DA-10/D-2, the deployment edge).

The chart is the one artifact no other test exercises: it is rendered by `helm install`, in a
cluster, on deployment day. `make helm-validate` (CI) checks the rendered YAML against the
Kubernetes schemas — but a schema check cannot know whether `CHEMCLAW_FOO` is a *real* setting.
Two failure modes live in that gap, and both are silent until production:

1. **A key that is not a field** — pydantic-settings tolerates an unknown prefixed *environment*
   variable (unlike an unknown key in a `.env` file, which is what broke the quickstart in DA-1),
   so the operator who sets it gets no error and no effect. A setting they believe they turned on
   is quietly ignored. For a deployment that is worse than a crash.
2. **A malformed value on a real field** — this one *does* crash, at import, in every pod at once.

These tests close both, offline, against the same `Settings` the pods construct.

**What they read, and therefore what they cannot see.** Everything below asserts against the
chart's *source*: `values.yaml` parsed as YAML, and the `templates/` files as text. Nothing here
renders. So a claim of the form "the mount is read-only" is really "the string `readOnly: true`
appears inside that helper's body" — true of a helper that wraps it in a `{{- if }}` no deployment
satisfies, and true of one whose surrounding block never renders at all. The same holds for every
`include "…"` count and every "this key appears only in that file" check.

That is not a gap anyone can close here: `helm` is not a Python dependency and is absent from the
sandbox this suite runs in. It is closed *in CI*, but only halfway — the `chart` job renders with
`helm template` and pipes the result to `kubeconform`, which asks whether the YAML is schema-valid
and never asks whether it says what these tests claim. Asserting on rendered documents needs
`helm` in the job that runs pytest, which is a CI change rather than a test change; see
`docs/planning/BACKLOG.md` (LIVE — "assert on rendered chart YAML"). Until then, read a green run
here as "the template source says so", not "the cluster will see so".
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
    # `required "<message>" .Values.X` renders exactly as `.Values.X` whenever X is set, and this
    # helper reads a `config` key that must never be absent — an empty one publishes to
    # `<noteRepoPath>/`, where no reader looks. The wrapper is stripped rather than matched
    # verbatim so this substitution keeps asserting the *path*, and the refusal it adds is asserted
    # where it can be: `tests/test_deploy_chart.py` renders the key-absent case with `helm`.
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

    The helper's logic is reproduced here, which is a duplication worth taking: the alternative is
    shelling out to `helm`, and this suite is the *offline* half that runs everywhere (the rendered
    check is `make helm-validate`). What it buys is that the JSON `CHEMCLAW_CONNECTOR_URLS`
    actually produces is fed through `Settings`, so a render that emits something `dict[str, str]`
    cannot parse fails here rather than in the cluster.
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

    `chem`'s and `safety`'s manifests name their bearer with `token_env`
    (`connectors/manifest.py::BearerAuth`), and `connectors/identity.py::_EnvBearerAuth` reads it
    from `os.environ` per request rather than through the typed `Settings` object — so
    `_field_for("CHEMCLAW_CHEM_TOKEN")` is never going to be in `Settings.model_fields`, and the
    generic orphan check below would otherwise flag every one of these as a key nothing reads. The
    `calc` sibling server's bearer is the same shape one step removed: its name is a *setting's
    value* (`settings.calc_server_token_env`) rather than a manifest field, because `calc`'s own
    manifest must stay off `CHEMCLAW_CONNECTORS_DIR`
    (`D-2026-08-16-the-physics-leaves-the-cache-stays`).

    Reused from `chemclaw.cli.validate_prose_contract`, the module that already has to solve this
    exact problem for operator prose, rather than re-deriving it: both readers need "is this name
    genuinely consumed", and a second implementation is a second place for the two to drift as a
    fourth connector brings its own token.
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
    """A duplicate key in a YAML mapping is not an error to any parser this repository uses.

    Helm's takes the last one, `yaml.safe_load` takes the last one, and neither warns — so the whole
    gate agrees on a value while the file shows two. `config.CHEMCLAW_CALC_SERVER_URL` was declared
    twice, and the *first* occurrence is the one sitting under the comment block explaining why the
    key is stated at all ("**Stated rather than left to the code default**, which is a loopback
    address … every tool and all five durable jobs raised `CalcServerError` against nothing"). An
    operator who reads that paragraph and edits the line beneath it gets a rendered ConfigMap that
    still names the old address, and the failure they then hit is the one the paragraph describes.

    `yaml.compose()` rather than `safe_load`, because the duplicate is exactly what `safe_load`
    throws away: the node tree keeps every key, so the check is a walk over the mapping nodes.
    Applied to the whole document rather than to `config:`: any block can grow the same defect.
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

    A `Settings` field, a deploy script, or a connector's own bearer-token lookup — a key that is
    none of those is accepted silently by pydantic-settings when it arrives as an
    environment variable, so the operator who sets it gets no error and no effect. This is the only
    place that mistake can be caught.
    """
    orphans = {
        key
        for key in _chart_env_keys()
        if _field_for(key) not in Settings.model_fields
        and key not in _SHELL_CONSUMED_ENV
        and key not in _connector_token_envs()
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
    overrides["postgres_dsn"] = (
        # sslmode=verify-full because the chart enforces identity (entra_required=true), under which
        # a non-loopback DSN must state TLS — the security-review guard rejects a plaintext-capable
        # DSN in that posture. A production secret must carry the same (documented in the runbook).
        "postgresql://chemclaw:chemclaw@postgres:5432/chemclaw?sslmode=verify-full"
    )
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

    The framing envelope key is the sixth, and it is the first to land in `optionalKeys` rather
    than `keys` — a distinction that exists because putting it in `keys` broke every upgrade.
    `chemclaw.env` renders `keys` as a **required** `secretKeyRef`, and `secrets.create` defaults to
    false, so the Secret is operator-managed and predates any chart version naming a new key: a
    required addition takes every pod of an existing release into `CreateContainerConfigError` on
    `helm upgrade`. `chemclaw.migrationEnv` had already made that argument, two helpers below.

    Required is right for a credential whose absence silently breaks a capability — the four above.
    This one is the HMAC key `agent/framing.py` derives `ENVELOPE_TAG` from, it defaults to `""`,
    and the app starts either way. Not because unset is harmless: this docstring said the tag was
    then merely *predictable*, and `_envelope_nonce` actually falls back to `secrets.token_hex(8)`,
    a fresh random per process — so the tag is unguessable and *unshared*, and a durable session
    replayed by another replica or after a restart carries envelopes whose tag no longer matches,
    which the agent instructions make it read as ordinary prose. It is optional because the app
    starts and a required key breaks every upgrade, not because the deployment is fine without it.
    So it gets a Secret slot (not a `config` entry, which would render into a ConfigMap the `view`
    role can read) and an `optional: true` reference.

    The `chem`, `safety` and `calc` bearer tokens are the seventh through ninth, and they land in
    `optionalKeys` for the same *upgrade* reason the framing key does, not because their absence is
    harmless — it is not. `connectors/identity.py::_EnvBearerAuth` raises
    `MissingConnectorCredential` on the very first call with no token present, and for `calc` that
    first call is every SMILES-in tool and every durable calc job. They fit the "required is right"
    sentence above by that test alone; they are not `keys` anyway, because `chemclaw.env` is
    included by every Deployment the chart renders, not just the pods that call these three
    bundles, so a required entry would take the front door and every worker into
    `CreateContainerConfigError` on `helm upgrade` for a Secret edit that has nothing to do with
    them. `optionalKeys` is therefore doing two different jobs across its members: for the
    framing key, "optional" describes the capability; for a connector bearer, it describes only the
    upgrade, and an operator still has to set it before the bundle it gates works at all. (That
    sentence used to say "its four members" and count them; the map has grown twice since, so the
    number is gone and the assertion below is the count.)

    The tenth through fourteenth are the ones that were **missing**, and each is an exposure
    *reduction* rather than a new secret at rest — which is why they belong here without an
    architecture change. Every one has a live reader and a documented setting; what none of them
    had was a correct place to put the value. `chemclaw.env` mounts only what these maps name, so
    the sole remaining seam was `.Values.config` — a ConfigMap the OpenShift `view` role reads and
    `helm get values` prints — and `test_no_secret_is_carried_in_the_plaintext_config_map` refuses
    that, correctly, leaving the operator with nowhere to go. They are `optionalKeys` for the
    upgrade reason above and, unlike the connector bearers, genuinely optional in capability too:
    each gates a feature the shipped release does not use.

    `rxnlabelToken` is the labelling server's bearer, one hop away exactly as `calcToken` is.
    `llmFallbackApiKey` is the failover endpoint's own credential — `core/config/llm.py` calls the
    gap it closes "total rather than degraded", and enabling it took three keys of which this was
    the one with no slot. `vectorStoreApiKey` is Qdrant's / Databricks Vector Search's, unused by
    the `pgvector` default. `temporalApiKey` made the Temporal Cloud path unreachable from this
    chart at all. `sessionStoreDsn` falls back to `CHEMCLAW_POSTGRES_DSN`, so splitting the session
    store off had no seam.

    `mcpFaceToken` is the fifteenth, and it is the first whose *absence* is safe in the strong sense
    rather than the weak one. It is the bearer the read-only MCP face requires on `/mcp`, and the
    middleware fails **closed** on an unset variable: a face deployed without it answers 401 rather
    than serving the knowledge graph anonymously
    (`D-2026-08-29-a-digest-nobody-receives-is-not-delivered`). So "optional" here describes the
    capability honestly — the surface simply refuses — and the pod is not rendered at all unless
    `mcpFace.enabled`. It is a Secret slot rather than a `config` entry for the standing reason:
    `config` renders into a ConfigMap the `view` role can read, and anyone who learns this value can
    read the whole corpus through that surface.

    `rxnpredictToken` is the last, and it is not a new *kind* — it is a fourth of the seventh-
    through-ninth kind, arriving with the `rxnpredict` bundle that made `Chemclaw3-mcp`'s
    reaction/condition predictors addressable from this release at all. Its manifest names the
    variable with `token_env`, `_EnvBearerAuth` reads it per request, and unset means every
    prediction raises `MissingConnectorCredential` rather than degrading — the same fail-closed
    direction, and the same operator obligation, as `chem` and `safety`.

    Both maps are asserted, because "which secrets does this chart name" is one question and
    splitting the answer across two values is exactly how a key comes to be in neither.
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


def _hook_documents() -> dict[str, str]:
    """`migrate-job.yaml`'s two Job documents, keyed by the component label each carries.

    Split on the YAML document separator rather than parsed: the file is a Go template, so
    `yaml.safe_load_all` cannot read it — the same limitation the module docstring states for
    everything else here.
    """
    text = (_CHART / "templates" / "migrate-job.yaml").read_text()
    documents = {}
    for chunk in re.split(r"^---$", text, flags=re.MULTILINE):
        component = re.search(r"app\.kubernetes\.io/component:\s*(\S+)", chunk)
        if component:
            documents[component.group(1)] = chunk
    return documents


def test_the_pre_upgrade_hook_migrates_then_reconciles_grants() -> None:
    """Two steps whose order is not optional, in one container so the shell enforces it.

    The grants name tables the migrations create. One container rather than two hook Jobs, so the
    ordering is the shell's `&&` rather than two weights — and so a failed migration is never
    followed by a grant run at all.

    The stored-message conversion used to be the middle term of this `&&` and is deliberately no
    longer here; the test below is what says where it went and why.
    """
    documents = _hook_documents()
    migrate = " ".join(documents["migrate"].split())
    assert "python -m chemclaw.core.migrate && python -m chemclaw.core.grants" in migrate
    assert "chemclaw.agent.message_migration" not in migrate, (
        "the data conversion is back in the pre-upgrade hook, where it rewrites rows the previous "
        "release is still serving"
    )


def test_the_ddl_runs_before_the_rollout_and_the_data_conversion_after_it() -> None:
    """The whole of D-2026-08-27, asserted on the two hooks it splits.

    An additive migration is safe for the release still running, so the DDL keeps its `pre-upgrade`
    slot. Rewriting `session_messages` into a shape the previous release's reader raises on is not,
    so the conversion moved to `post-upgrade`: a release that fails its rollout — the case a
    pre-deploy hook exists to protect against — now converts nothing at all, because the hook that
    would have done it never fires.

    The credential split is the second half and is checked per *document*, not per file. Both Jobs
    live in `migrate-job.yaml`, so the file-level check above this cannot see which of them mounts
    the schema-owning DSN — and the converter runs as the runtime role, which already holds UPDATE
    on `session_messages`, so it must not hold it.
    """
    documents = _hook_documents()
    assert set(documents) == {"migrate", "convert"}, documents.keys()

    assert '"helm.sh/hook": pre-install,pre-upgrade' in documents["migrate"]
    assert '"helm.sh/hook-weight": "-5"' in documents["migrate"]

    convert = documents["convert"]
    assert '"helm.sh/hook": post-install,post-upgrade' in convert
    assert '"helm.sh/hook-weight": "5"' in convert
    assert '"python", "-m", "chemclaw.agent.message_migration"' in convert
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


def test_the_chart_states_its_privileged_roles_rather_than_omitting_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped chart closes every expensive job, and has to *say* so where an operator looks.

    `expensive: true` in a connector manifest derives into the trigger gate, and that gate fails
    closed on an empty role set. So under the shipped `CHEMCLAW_ENTRA_REQUIRED=true` with no
    privileged role, `sample_conformers` and its siblings are refused for every authenticated
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


def test_no_secret_is_carried_in_the_plaintext_config_map() -> None:
    """A credential in `.Values.config` is a credential in a ConfigMap.

    `templates/config.yaml` ranges over `.Values.config` into a `kind: ConfigMap`, so anything
    listed there is readable by every principal holding `get configmaps` — which the OpenShift
    `view` role grants, and which is a far wider audience than `get secrets`. `secrets.keys` is the
    other slot and the only correct one for a credential.

    Written as a check against the redaction inventory rather than against a hand-kept list,
    because those are the same question asked twice: `_SECRET_SETTINGS` is this codebase's own
    statement of which settings hold a credential, so a value it names has already been declared
    too sensitive to appear in a log line, and a ConfigMap is more durable than a log line. The
    framing envelope key was in `config`'s position by omission — it is not a credential to an
    external system, so it was never given a Secret slot — and that is exactly the case a
    name-shaped heuristic would miss and this one catches.
    """
    from chemclaw.core.logging import _SECRET_SETTINGS

    exposed = sorted(key for key in _VALUES["config"] if _field_for(key) in set(_SECRET_SETTINGS))
    assert not exposed, (
        f"these settings hold a credential (they are in _SECRET_SETTINGS) but are declared in "
        f".Values.config, which renders into a plaintext ConfigMap: {exposed}. Move each to "
        "secrets.keys and argue it in test_chart_declares_only_the_documented_secrets."
    )


def test_the_verifier_opt_in_is_documented_in_the_values_file() -> None:
    """The commented-out `CHEMCLAW_VERIFIER_*` block is the chart's opt-in surface — pinned as text.

    The verifier ships off (code default and chart alike), so the chart cannot *render* anything
    to assert; what a deployer has instead is the documented block in `values.yaml` naming the two
    facts that make the flip safe — the startup capability probe, and the review band. Prose in a
    values file is exactly the kind of claim that silently vanishes in a refactor, which is what
    this pin exists to make loud. It checks the raw text because the keys are comments: parsed
    YAML deliberately does not carry them, and that they are NOT in the parsed config is asserted
    too — an uncommented default would switch every deployment's judge on from a values edit that
    read like documentation.
    """
    text = (_CHART / "values.yaml").read_text(encoding="utf-8")
    for key in ("CHEMCLAW_VERIFIER_ENABLED", "CHEMCLAW_VERIFIER_CONFIDENCE_THRESHOLD"):
        assert f"# {key}" in text, f"the commented opt-in for {key} left values.yaml"
        assert key not in _VALUES["config"], f"{key} must stay a documented opt-in, not a default"
    assert "require_verifier_capability" in text, (
        "the comment must name the startup probe a deployer will hit"
    )


def test_every_credential_this_deployment_holds_has_a_secret_slot() -> None:
    """A credential with no Secret slot has exactly one chart seam left: the plaintext ConfigMap.

    `chemclaw.env` mounts every `secrets.keys`/`optionalKeys` entry on every pod, and
    `secrets.migrationKeys` on the hook Job — so a credential named in none of the three can only
    be set through `.Values.config`, which `templates/config.yaml` ranges into a `kind: ConfigMap`.
    `test_no_secret_is_carried_in_the_plaintext_config_map` then correctly refuses to let it be
    declared there, which leaves an operator with a documented setting and no correct way to set
    it. Four were in that state — `llm_fallback_api_key` (whose own comment calls the gap it closes
    "total rather than degraded"), `vector_store_api_key`, `temporal_api_key` (so the Temporal Cloud
    path was unreachable from the chart) and `session_store_dsn` — and the natural operator action
    for each is `--set config.CHEMCLAW_…=<secret>`, i.e. the credential in a ConfigMap the
    OpenShift `view` role reads and in `helm get values` output.

    Driven off `Settings` rather than a hand-kept list, so a credential added to the config object
    arrives here rather than at a deployment. It is the same question
    `tests/test_credentials.py::test_every_credential_shaped_setting_is_in_the_redaction_inventory`
    asks of the log filter, asked of the chart.
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
    # `live_probe_token` is the one exemption, and it is not a deployment credential at all: the
    # live lane mints it (`infra/live/processes.sh`) for a *client* pointed at a running front
    # door. Nothing in a pod reads it, so a Secret slot would be a credential this release neither
    # holds nor needs.
    wanted -= {"CHEMCLAW_LIVE_PROBE_TOKEN"}
    assert wanted <= slots, f"credentials with no Secret slot: {sorted(wanted - slots)}"


def test_the_labelling_server_is_addressable_from_the_chart() -> None:
    """`rxnlabel` is dialled by a live Temporal Schedule and had no deployment surface at all.

    `durable/schedules.py` creates a `reaction-labels` Schedule for any data source that `provides`
    reactions — deliberately with no separate enable flag (`core/config/labels.py`: "There is
    deliberately no `labels_enabled`"), so attaching a reaction corpus is the whole trigger. The
    client's default address is `http://127.0.0.1:8865/mcp`, chosen for a dev process, and the
    chart named no host, no bearer and no egress port for it.

    In a cluster that is D-131 exactly: the drain dials the *worker's own pod*, where nothing
    listens, and the corpus is never labelled — so every faceted precedent question answers from an
    empty label index, with no error anywhere. `connectors/registry.py` records the same defect
    ("in a cluster the front door probed `127.0.0.1:881x` — its own pod") and the connector seam
    fixed it for every bundle; this is the one client that was never given a chart value.

    The egress port is asserted with it because the two only work together: a NetworkPolicy egress
    rule restricts by port independently of its `to:` peer list, so an operator who sets the URL
    and adds the host to `egressDestinations` still has every packet dropped. That is the trap
    `values.yaml` documents for `chem`/`safety`/`calc` and did not apply to this one.
    """
    url = _VALUES["config"].get("CHEMCLAW_RXNLABEL_SERVER_URL")
    assert url, "the chart states no address for the labelling server"
    assert "127.0.0.1" not in url, f"the chart ships the dev loopback address: {url}"

    port = _VALUES["networkPolicy"]["egressPorts"].get("rxnlabel")
    assert port, "networkPolicy.egressPorts names no rxnlabel port"
    assert str(port) in url, f"the egress port {port} is not the port the URL dials ({url})"

    policy = (_CHART / "templates" / "networkpolicy.yaml").read_text(encoding="utf-8")
    assert "egressPorts.rxnlabel" in policy, (
        "the port is declared in values.yaml but never emitted in the egress rule, so it permits "
        "nothing"
    )


def test_every_externally_hosted_connector_can_actually_be_dialled() -> None:
    """The test above, generalised off the bundle set instead of one hand-written server.

    `connectors.<name>.url` says a sibling repository hosts this capability, and three separate
    things have to line up before a packet reaches it: the address, a `networkPolicy.egressPorts`
    entry carrying *that* port, and the template actually emitting the entry. Miss the second and
    the connection is dropped even with the host in `egressDestinations`, because a NetworkPolicy
    egress rule restricts by port independently of its `to:` peer list. Miss the third and the
    values entry is a knob that renders nothing
    (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`).

    Derived from the values file rather than listed, so the *next* externally-hosted bundle is
    covered on the day its `url:` is written. The hand-written version above stays because
    `rxnlabel` is not a connector at all — its address is a `config` key, and no walk of the
    `connectors` block can see it.
    """
    policy = (_CHART / "templates" / "networkpolicy.yaml").read_text(encoding="utf-8")
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
        assert f"egressPorts.{name}" in policy, (
            f"egressPorts.{name} is declared in values.yaml and never emitted in the egress rule, "
            "so it permits nothing"
        )


def test_no_egress_port_is_declared_without_being_emitted() -> None:
    """The other direction, over the whole map: a port entry no rule names permits nothing.

    The test above only reaches ports belonging to a connector `url:`. This one asks the question
    of every key in `egressPorts`, which is where a value added for a client that is *not* a
    connector — the labeller was one — would otherwise sit unreferenced and read, in review, as a
    control that had been set up.
    """
    policy = (_CHART / "templates" / "networkpolicy.yaml").read_text(encoding="utf-8")
    ports = _VALUES["networkPolicy"]["egressPorts"]
    assert ports, "networkPolicy.egressPorts is empty; this test would assert nothing"
    unemitted = sorted(key for key in ports if f"egressPorts.{key}" not in policy)
    assert not unemitted, (
        f"networkPolicy.egressPorts declares {unemitted}, which no egress rule emits. Either the "
        "rule is missing — in which case the destination is unreachable — or the entry is dead "
        "and should be deleted."
    )
