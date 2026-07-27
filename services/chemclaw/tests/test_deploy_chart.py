"""Structural gate over the Helm chart, the image, and the entrypoint (gaps DEP-1…DEP-5).

`helm template | kubeconform` is a live-edge check that needs the helm binary and a cluster schema;
this suite is the offline half that runs everywhere and catches the failure modes that actually bit:

- an `include` naming a `define` that does not exist (renders empty, silently drops a volume),
- a `.Values.x.y` path that no longer exists in `values.yaml` (renders empty, same),
- an unbalanced `{{ if }}` / `{{ end }}`,
- a `CHEMCLAW_COMPONENT` the entrypoint has no case for (guaranteed crash loop),
- an image missing a directory the running components read at import/run time — which is how
  `skills/`, `scripts/` and `evals/` came to be absent from the image while every test passed.

None of these are visible to `mypy`/`pytest` on the Python tree, and all of them break a deployment
silently rather than loudly, which is why they earn a test of their own.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "chemclaw"
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
TEMPLATES = sorted((CHART / "templates").glob("*.yaml"))


def _template_text() -> dict[Path, str]:
    """Every rendered-template source keyed by path (the `.tpl` helpers included)."""
    files = {p: p.read_text() for p in TEMPLATES}
    files[CHART / "templates" / "_helpers.tpl"] = (CHART / "templates" / "_helpers.tpl").read_text()
    return files


def _values() -> dict[str, Any]:
    """The chart's default values, parsed."""
    loaded = yaml.safe_load((CHART / "values.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_every_include_resolves_to_a_define() -> None:
    """An `include` of a missing `define` renders as empty — a silently dropped volume or mount."""
    text = "\n".join(_template_text().values())
    defined = set(re.findall(r'define\s+"([^"]+)"', text))
    included = set(re.findall(r'include\s+"([^"]+)"', text))
    assert included <= defined, f"include with no define: {sorted(included - defined)}"


def test_every_values_path_exists() -> None:
    """A `.Values.a.b` that values.yaml no longer has renders empty, not as an error."""
    values = _values()
    missing: list[str] = []
    for path in sorted(
        set(re.findall(r"\.Values\.([A-Za-z0-9_.]+)", "\n".join(_template_text().values())))
    ):
        node: Any = values
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                missing.append(path)
                break
    assert not missing, f"templates reference absent values: {missing}"


@pytest.mark.parametrize("path", [*TEMPLATES, CHART / "templates" / "_helpers.tpl"])
def test_template_control_flow_is_balanced(path: Path) -> None:
    """Each `if`/`range`/`with`/`define` is closed by exactly one `end`."""
    text = path.read_text()
    # Strip template comments first: they legitimately contain the words below in prose.
    body = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.DOTALL)
    opens = len(re.findall(r"\{\{-?\s*(?:if|range|with|define)\s", body))
    ends = len(re.findall(r"\{\{-?\s*end\s*-?\}\}", body))
    assert opens == ends, f"{path.name}: {opens} open blocks vs {ends} end"


def test_every_declared_component_has_an_entrypoint_case() -> None:
    """A `CHEMCLAW_COMPONENT` the entrypoint cannot dispatch is a guaranteed crash loop."""
    entrypoint = (DEPLOY / "entrypoint.sh").read_text()
    cases = set(re.findall(r"^\s{2}([a-z0-9-]+)\)", entrypoint, flags=re.MULTILINE))
    declared = set(
        re.findall(
            r'name:\s*CHEMCLAW_COMPONENT\s*\n\s*value:\s*"([a-z0-9-]+)"',
            "\n".join(_template_text().values()),
        )
    )
    # Templated names (e.g. "connector-{{ $name }}") are checked by their prefix instead.
    concrete = {name for name in declared if "{{" not in name}
    assert concrete <= cases, f"components with no entrypoint case: {sorted(concrete - cases)}"


def test_the_entrypoint_has_no_case_the_chart_never_declares() -> None:
    """The other direction, which is how a deleted component stayed routable (D-117).

    The check above catches a chart component with no entrypoint case — a crash loop. It cannot
    catch the reverse: an entrypoint case for a component nothing deploys. That is what happened
    to `mcp-calc`. Its module was described as deleted in three separate documents, yet
    `entrypoint.sh` still carried `mcp-calc) exec python -m mcp_servers.calc.server`, so the image
    went on shipping and dispatching a second live copy of seven `calc`-bundle tools. Nothing
    failed, because nothing looked this way.

    `*` is the unknown-component guard, and the two `<prefix>-*` cases are the generic connector
    dispatch — the whole point of the seam is that they match names no chart line spells out.
    """
    entrypoint = (DEPLOY / "entrypoint.sh").read_text()
    cases = set(re.findall(r"^\s{2}([a-z0-9-]+)\)", entrypoint, flags=re.MULTILINE))
    prefixes = set(re.findall(r"^\s{2}([a-z0-9-]+)-\*\)", entrypoint, flags=re.MULTILINE))
    declared = set(
        re.findall(
            r'name:\s*CHEMCLAW_COMPONENT\s*\n\s*value:\s*"([a-z0-9-]+)"',
            "\n".join(_template_text().values()),
        )
    )
    # A chart value like "connector-{{ $name }}" reduces to the prefix the entrypoint globs on.
    templated_prefixes = {
        name.split("-")[0] for name in re.findall(r'value:\s*"([a-z0-9-]+)-\{\{', _all_templates())
    }
    orphans = {
        case
        for case in cases
        if case not in declared
        and case not in prefixes
        and not any(case.startswith(f"{prefix}-") for prefix in prefixes | templated_prefixes)
    }
    assert not orphans, (
        f"entrypoint dispatches components nothing deploys: {sorted(orphans)} — "
        "either the chart lost a component or the case outlived its module"
    )


def _all_templates() -> str:
    """Every chart template as one string (both component tests read it this way)."""
    return "\n".join(_template_text().values())


# Top-level directories that are never shipped: test code, and the docs/infra trees the image
# either does not need or COPYs under a different name. Everything else first-party must ship.
_NOT_SHIPPED = frozenset({"tests", "examples"})


def test_image_ships_every_first_party_package() -> None:
    """Every first-party Python package must be in the image, discovered — not listed by hand.

    This started as a hardcoded list and **missed `safety/`**: `main` added the package, the image
    never COPYd it, and the container died at import with `ModuleNotFoundError: No module named
    'safety'`. A hardcoded list only ever catches the omissions someone already thought of, which
    is precisely the failure it was written to prevent. Discovering the packages instead means the
    next one is covered on the day it is created.
    """
    root = DEPLOY.parent
    packages = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "__init__.py").exists() and path.name not in _NOT_SHIPPED
    }
    containerfile = (DEPLOY / "Containerfile").read_text()
    copied = set(re.findall(r"^COPY\s+(\S+)\s", containerfile, flags=re.MULTILINE))
    missing = sorted(packages - copied)
    assert not missing, f"Containerfile never COPYs first-party package(s): {missing}"


def test_image_ships_the_data_directories_read_at_runtime() -> None:
    """`skills/`, `knowledge/` and `infra/` are data, not packages, so discovery cannot find them.

    Their absence is invisible offline: the agent simply advertises no skills, the graph is empty,
    and migrations have no SQL — each a silent capability loss rather than a crash.
    """
    containerfile = (DEPLOY / "Containerfile").read_text()
    copied = set(re.findall(r"^COPY\s+(\S+)\s", containerfile, flags=re.MULTILINE))
    for required in ("skills", "knowledge", "infra"):
        assert required in copied, f"Containerfile never COPYs {required}/"


def test_image_installs_git() -> None:
    """The PR-gate shells out to git; an image without it fails every knowledge write at push."""
    assert "dnf install -y git" in (DEPLOY / "Containerfile").read_text()


def test_the_chart_gives_each_bundle_the_halves_its_manifest_declares() -> None:
    """`server`/`worker` in values must match the bundle's own `connector.yaml`, both ways.

    These two flags are the chart's only hand-maintained mirror of a manifest, and each direction
    fails silently in a different way. A bundle with `jobs:` and no `worker: true` gets no pod
    polling its queue, so every job it starts waits forever — the exact failure `workflows.registry`
    exists to prevent, one layer out. A bundle with `server: true` and no `endpoint:` gets an app
    Deployment running `uvicorn connectors.<name>.server.app:app` against a module that does not
    exist, which is a crash loop plus a bogus entry in the front door's address map.

    Derived from the manifests rather than listed, so a new bundle is covered on the day it is
    created.
    """
    from connectors.registry import discovered

    entries = _values()["connectors"]
    for name, (_bundle, manifest) in discovered().items():
        cfg = entries[name]
        assert bool(cfg.get("server")) is (manifest.endpoint is not None), name
        assert bool(cfg.get("worker")) is bool(manifest.jobs), name


def test_every_shipped_connector_has_a_chart_entry() -> None:
    """A bundle with no `connectors` entry could never be given pods — DEP-3's successor.

    The old guard here forced the MCP Deployments *off*, because they would have run a stdio server
    with no stdin. Connectors are HTTP servers with a health route, so the failure mode inverted:
    the
    risk is no longer deploying them, it is shipping a bundle the chart cannot deploy at all.
    """
    from connectors.registry import discovered

    entries = _values()["connectors"]
    for name in discovered():
        assert name in entries, f"connector bundle {name!r} has no entry in values.yaml connectors"
        assert "enabled" in entries[name] and "replicas" in entries[name]


def test_knowledge_volume_is_mounted_on_every_reading_component() -> None:
    """Readers resolve the graph as a local directory, so each needs the synced volume (DEP-1)."""
    for template in ("deployment-service.yaml", "deployment-workers.yaml"):
        text = (CHART / "templates" / template).read_text()
        assert 'include "chemclaw.knowledgeMounts"' in text, template
        assert 'include "chemclaw.knowledgeInit"' in text, template


def test_note_repo_clone_exists_wherever_notes_are_submitted() -> None:
    """The front door and the background worker both call `propose_note`, so both need a clone."""
    for template in ("deployment-service.yaml", "deployment-workers.yaml"):
        text = (CHART / "templates" / template).read_text()
        assert 'include "chemclaw.noteRepoInit"' in text, template
    # And nowhere else. A connector's worker returns its note in the job envelope for core to
    # PR-gate (D-118), so giving it a writable clone would hand a bundle a second write path into
    # the graph — the asymmetry the seam exists to enforce.
    connectors = (CHART / "templates" / "deployment-connectors.yaml").read_text()
    assert 'include "chemclaw.noteRepoInit"' not in connectors


def test_schedules_are_applied_by_a_post_install_hook() -> None:
    """Without this Job no Temporal Schedule exists, so no periodic job ever fires (DEP-5)."""
    job = (CHART / "templates" / "schedules-job.yaml").read_text()
    assert '"helm.sh/hook": post-install,post-upgrade' in job
    assert '"python", "-m", "scripts.schedules"' in job


def test_push_credential_is_declared() -> None:
    """Every agent-authored note fails at push without a git credential in the chart (DEP-2)."""
    assert "knowledgeRepoToken" in _values()["secrets"]["keys"]


def test_connector_urls_are_computed_from_the_deployed_set() -> None:
    """The address the front door dials must come from the values block that creates the Service.

    Hand-writing `CHEMCLAW_CONNECTOR_URLS` in `config` would let it name a connector with no
    pods, or
    miss one that has them — a failure that looks like a capability silently disappearing. The
    ConfigMap therefore *includes* the helper that ranges over `.Values.connectors`, and the helper
    builds each URL from the Service name and `connectorPort`.
    """
    config = (CHART / "templates" / "config.yaml").read_text()
    assert 'CHEMCLAW_CONNECTOR_URLS: {{ include "chemclaw.connectorUrls" . | quote }}' in config
    helper = (CHART / "templates" / "_helpers.tpl").read_text()
    assert 'define "chemclaw.connectorUrls"' in helper
    assert "range $name, $cfg := .Values.connectors" in helper
    assert "$.Values.connectorPort" in helper


def test_connectors_are_reachable_only_from_chemclaw_pods() -> None:
    """The identity headers are advisory, so the network boundary is what keeps them meaningful."""
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    assert "connector-ingress" in policy
    # Egress must allow the connector port, or the front door could not dial its own connectors.
    assert policy.count("{{ .Values.connectorPort }}") >= 2


def test_a_comment_never_swallows_the_line_after_it() -> None:
    """A `-}}` comment closure strips the following newline, gluing the next line onto the previous.

    Harmless at the top of a document (the next line is an unindented `apiVersion:`, and there is no
    preceding output to glue it to) and **fatal mid-document**: a `{{- /* … */ -}}` sitting inside a
    `data:` block appended `CHEMCLAW_NOTE_REPO_DIR:` to the line above, and `helm lint` failed with
    "did not find expected key".

    The brace-balance and include/values checks above could not see this — it is a *whitespace*
    bug, not a structural one — so CI's `helm lint` caught it first. This is the offline half:
    a comment closed with `-}}` must not be followed by an indented line.
    """
    offenders: list[str] = []
    for path in [*TEMPLATES, CHART / "templates" / "_helpers.tpl"]:
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines[:-1]):
            if not re.search(r"\*/\s*-\}\}", line):
                continue
            following = next((ln for ln in lines[index + 1 :] if ln.strip()), "")
            if following.startswith((" ", "\t")):
                offenders.append(f"{path.name}:{index + 1} swallows {following.strip()!r}")
    assert not offenders, "comment closures that eat the next line: " + "; ".join(offenders)


# Kinds kubeconform has no schema for, so `make helm-validate` runs with
# `-ignore-missing-schemas` and *skips* them rather than failing. Keeping the set explicit is what
# stops that flag from being a hole: a skipped kind is a deliberate entry here, not a silent pass.
_UNVALIDATED_KINDS = frozenset({"Route"})


def test_only_the_known_crd_is_unvalidated_by_kubeconform() -> None:
    """Pin which kinds the chart renders, so `-ignore-missing-schemas` cannot hide a new one.

    `make helm-validate` must pass `-ignore-missing-schemas` because the chart renders an OpenShift
    `route.openshift.io/v1 Route`, and no JSON schema for it exists in kubeconform's defaults or in
    the datreeio CRDs catalog — both 404. Without the flag the target can never pass, which is why
    it had never been seen to pass: the only workflow that ran it was stranded where GitHub Actions
    does not read (D-117).

    The cost of the flag is that an unknown kind is skipped instead of rejected. This test buys that
    back offline: every kind the chart renders is either a core Kubernetes kind (which kubeconform
    does validate) or is named here.
    """
    core_kinds = {
        "ConfigMap",
        "Deployment",
        "HorizontalPodAutoscaler",
        "Job",
        "NetworkPolicy",
        "Secret",
        "Service",
        "ServiceAccount",
    }
    rendered = set(re.findall(r"^kind:\s*([A-Za-z]+)", _all_templates(), flags=re.MULTILINE))
    unexpected = rendered - core_kinds - _UNVALIDATED_KINDS
    assert not unexpected, (
        f"the chart renders kind(s) {sorted(unexpected)} that kubeconform may silently skip — "
        "add a schema location, or add them to _UNVALIDATED_KINDS with the reason"
    )
    # And the exemption must stay earned: if Route ever gains a schema, drop it from the set.
    assert _UNVALIDATED_KINDS <= rendered, (
        f"_UNVALIDATED_KINDS names kind(s) the chart no longer renders: "
        f"{sorted(_UNVALIDATED_KINDS - rendered)}"
    )
