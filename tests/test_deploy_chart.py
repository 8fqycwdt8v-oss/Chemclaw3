"""Structural gate over the Helm chart, the image, and the entrypoint (gaps DEP-1…DEP-5).

`helm template | kubeconform` is a live-edge check that needs the helm binary and a cluster schema;
this suite is the offline half that runs everywhere and catches the failure modes that actually bit:

- an `include` naming a `define` that does not exist (renders empty, silently drops a volume),
- a `.Values.x.y` path that no longer exists in `values.yaml` (renders empty, same),
- an unbalanced `{{ if }}` / `{{ end }}`,
- a `CHEMCLAW_COMPONENT` the entrypoint has no case for (guaranteed crash loop),
- an image missing a directory the running components read at import/run time — which is how
  the skills, the terminal entrypoints and the eval case-set all came to be absent from the
  image while every test passed.

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

    That module path is the pre-D-148 spelling and is left as written: it quotes a file as it
    actually was. D-148's rewrite of every `mcp_servers.…` path caught this line too and made the
    quotation say something the entrypoint never said — a small instance of the same class of error
    the paragraph is about.

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


# Directories the image reads at runtime that are *data*, not code, so no package discovery can
# find them. Each absence is invisible offline and silent in production: the agent simply advertises
# no skills or fewer capabilities, the graph is empty, migrations have no SQL. `tests/` and
# `examples/` are the two first-party trees deliberately not shipped.
_RUNTIME_DATA = ("data", "skills", "knowledge", "infra")


def _copied() -> set[str]:
    """Every path the Containerfile COPYs."""
    containerfile = (DEPLOY / "Containerfile").read_text()
    return set(re.findall(r"^COPY\s+(\S+)\s", containerfile, flags=re.MULTILINE))


def test_image_ships_the_first_party_source_tree() -> None:
    """All first-party code must be in the image.

    This started as a hardcoded list and **missed `safety/`**: `main` added the package, the image
    never COPYd it, and the container died at import with `ModuleNotFoundError: No module named
    'safety'`. It then discovered the eighteen top-level packages instead, so the next one was
    covered on the day it was created.

    Since D-148 there is one package under `src/`, so the discovery has nothing left to discover —
    and a test that iterates an empty set passes vacuously, which is worse than the hardcoded list
    it replaced. What it asserts now is the property that actually keeps the image complete: `src/`
    is COPYd whole, and `tests/test_packaging.py` separately forbids a first-party package from
    reappearing anywhere else.
    """
    assert "src" in _copied(), "Containerfile never COPYs src/ — the image would ship no code"


def test_image_ships_the_data_directories_read_at_runtime() -> None:
    """The data trees are not code, so nothing about them is caught by an import failure."""
    copied = _copied()
    for required in _RUNTIME_DATA:
        assert required in copied, f"Containerfile never COPYs {required}/"


def test_every_runtime_data_directory_actually_exists() -> None:
    """A COPY of a vanished directory fails the build; one that moved fails silently at start-up.

    The pairing matters: `eln/exports` became `data/eln-exports` in D-148, and had the Containerfile
    kept COPYing `eln/` the build would have broken loudly — but had the *config default* alone
    moved, the image would have started fine and read an empty export directory forever.

    D-156 moved three more (`profiles`, `templates`, `evals`) under `data/`, so this list shrank
    rather than grew. Four entries now: `data/` is every corpus the code reads, and `skills/`,
    `knowledge/` and `infra/` are the three that are not configuration: layers 3 and 4, and the SQL.
    """
    root = DEPLOY.parent
    for required in _RUNTIME_DATA:
        assert (root / required).is_dir(), (
            f"Containerfile COPYs {required}/ but no such directory exists at the repository root"
        )


def _dnf_installed_packages() -> set[str]:
    """Every package the image installs with dnf, parsed rather than substring-matched.

    Matching the literal install line meant a test could keep passing while the thing it named had
    moved: `"dnf install -y git" in text` is satisfied by `dnf install -y git` and by
    `dnf install -y github-cli`, and it says nothing about the second package the sync path needs.
    """
    text = (DEPLOY / "Containerfile").read_text()
    return {
        package
        for line in re.findall(r"dnf install -y ([^\n&|]+)", text)
        for package in line.split()
        if not package.startswith("-")
    }


def test_image_installs_the_binaries_the_knowledge_layer_shells_out_to() -> None:
    """Both directions of the knowledge layer are shell-outs, and both were not equally supplied.

    `git` was installed and asserted from the start — the PR-gate pushes with it. `rsync` was
    neither: `knowledge-sync.sh` publishes the read replica with it and fell back to
    `rm -rf "${publish_dir}"/*` when the call failed, with stderr discarded — so a package that was
    never installed became a silent, recurring deletion of the tree the front door reads live.
    """
    assert {"git", "rsync"} <= _dnf_installed_packages()


def test_the_sync_never_deletes_what_it_is_about_to_replace() -> None:
    """The publish step must fail loudly rather than empty the directory the app is reading.

    Guarding the *shape* and not just the missing package: reintroducing any `rm -rf` of the publish
    directory reintroduces the outage even with rsync present, because the destructive branch is
    reachable on any rsync failure (a dead remote, a full disk, a permission change).
    """
    script = (DEPLOY / "knowledge-sync.sh").read_text()
    destructive = [
        line
        for line in script.splitlines()
        if "rm -rf" in line and "publish_dir" in line and not line.lstrip().startswith("#")
    ]
    assert not destructive, f"knowledge-sync.sh must never rm -rf the published tree: {destructive}"

    publish = script.split("Publish into the directory")[1]
    assert "command -v rsync" in publish, "a missing rsync must be detected, not swallowed"
    assert "rsync -a --delete" in publish and "2>/dev/null" not in publish.split("rsync -a")[1], (
        "rsync must run with its stderr visible, or a missing binary looks like a transfer error"
    )


def test_the_image_carries_the_revision_it_was_built_from() -> None:
    """`deployment_revision` must be settable by a build, or AG-14 reads as met while being unmet.

    `core/config.py` has always said the F6 image build injects the revision, and until REV-17
    no build did: nothing in the Containerfile, the chart or CI set `CHEMCLAW_DEPLOYMENT_REVISION`,
    so every audit record in every deployment carried the literal `"unknown"`. The whole point of
    the field is tying a past agent result to the exact prompt/skill/config version that produced
    it, and a constant answers no such question.

    Pinned in three parts because each is separately droppable: the ARG must exist, it must reach
    the image's environment under the name the settings prefix reads, and CI must actually pass a
    value. The image workflow additionally runs the built image and compares — only that can prove
    the value arrived, and only a built image can do it.
    """
    containerfile = (DEPLOY / "Containerfile").read_text()
    assert "ARG CHEMCLAW_REVISION" in containerfile, "the Containerfile declares no revision ARG"
    assert "CHEMCLAW_DEPLOYMENT_REVISION=${CHEMCLAW_REVISION}" in containerfile, (
        "the revision ARG never reaches the environment, so `settings.deployment_revision` "
        "stays at its 'unknown' default in every built image"
    )
    workflow = (DEPLOY.parent / ".github" / "workflows" / "image.yml").read_text()
    assert "--build-arg" in workflow and "CHEMCLAW_REVISION=" in workflow, (
        "the image workflow builds without passing CHEMCLAW_REVISION, so the ARG falls back to "
        "its 'unknown' default and the wiring above is inert"
    )


def test_the_chart_gives_each_bundle_the_halves_its_manifest_declares() -> None:
    """`server`/`worker` in values must match the bundle's own `connector.yaml`, both ways.

    These two flags are the chart's only hand-maintained mirror of a manifest, and each direction
    fails silently in a different way. A bundle with `jobs:` and no `worker: true` gets no pod
    polling its queue, so every job it starts waits forever — the exact failure
    `chemclaw.durable.registry`
    exists to prevent, one layer out. A bundle with `server: true` and no `endpoint:` gets an app
    Deployment running `uvicorn connectors.<name>.server.app:app` against a module that does not
    exist, which is a crash loop plus a bogus entry in the front door's address map.

    Derived from the manifests rather than listed, so a new bundle is covered on the day it is
    created.
    """
    from chemclaw.connectors.registry import discovered

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
    from chemclaw.connectors.registry import discovered

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
    assert '"python", "-m", "chemclaw.cli.schedules"' in job


def test_the_route_pins_a_browser_to_one_front_door_pod() -> None:
    """Session affinity is a correctness requirement of the front door, not a tuning preference.

    The chart runs the front door at two replicas and autoscales to six. The per-session turn
    guard is durable now (`session_turns`, D-121), but a conversation still depends on state that
    lives only in the process that created it: uploaded attachments, the harness todo list, and
    the live `AgentSession` handle. Land the follow-up request on a sibling pod and the agent
    simply cannot see the file the chemist just uploaded.

    Asserted rather than left to the haproxy router's default, because a default that is silently
    flipped cluster-wide would break attachments with no change to this repository.
    """
    route = (CHART / "templates" / "service-route.yaml").read_text()
    assert 'haproxy.router.openshift.io/disable_cookies: "false"' in route


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


# CRDs kubeconform validates against the **datreeio catalog** rather than its bundled defaults.
# These are checked as strictly as a core kind; they are listed apart only because the `Makefile`
# has to supply the catalog `-schema-location` for them to resolve at all.
#
# `ServiceMonitor` sat in `_UNVALIDATED_KINDS` and did not belong there. The CI run that first
# rendered a `PrometheusRule` reported `29 resources found — Valid: 28, Skipped: 1`: exactly one
# kind in the whole chart lacks a schema, so both Prometheus-operator CRDs were being validated all
# along. The exemption had never been checked against what kubeconform actually did.
_CATALOG_VALIDATED_KINDS = frozenset({"ServiceMonitor", "PodMonitor", "PrometheusRule"})

# The one kind kubeconform genuinely has no schema for, so `make helm-validate` runs with
# `-ignore-missing-schemas` and *skips* it rather than failing. Keeping the set explicit is what
# stops that flag from being a hole: a skipped kind is a deliberate entry here, not a silent pass.
_UNVALIDATED_KINDS = frozenset({"Route"})

# What the CI gate reports for the chart as it stands: every rendered resource validated except the
# OpenShift `Route`. Pinned as a number because the two sets above are claims about kubeconform's
# behaviour, and a claim about someone else's tool is worth stating in a form that can be compared
# against its actual output rather than believed.
_EXPECTED_SKIPPED_RESOURCES = 1


def test_only_the_known_crd_is_unvalidated_by_kubeconform() -> None:
    """Pin which kinds the chart renders, so `-ignore-missing-schemas` cannot hide a new one.

    `make helm-validate` must pass `-ignore-missing-schemas` because the chart renders an OpenShift
    `route.openshift.io/v1 Route`, and no JSON schema for it exists in kubeconform's defaults or in
    the datreeio CRDs catalog — both 404. Without the flag the target can never pass, which is why
    it had never been seen to pass: the only workflow that ran it was stranded where GitHub Actions
    does not read (D-117).

    The cost of the flag is that an unknown kind is skipped instead of rejected. This test buys that
    back offline: every kind the chart renders is a core Kubernetes kind, a CRD the catalog covers,
    or the one genuinely unvalidated kind named above.
    """
    core_kinds = {
        "ConfigMap",
        "Deployment",
        "HorizontalPodAutoscaler",
        "Job",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "Secret",
        "Service",
        "ServiceAccount",
    }
    rendered = set(re.findall(r"^kind:\s*([A-Za-z]+)", _all_templates(), flags=re.MULTILINE))
    unexpected = rendered - core_kinds - _CATALOG_VALIDATED_KINDS - _UNVALIDATED_KINDS
    assert not unexpected, (
        f"the chart renders kind(s) {sorted(unexpected)} that kubeconform may silently skip — "
        "add a schema location, or add them to _UNVALIDATED_KINDS with the reason"
    )
    # Both exemptions must stay earned: a kind the chart stopped rendering is stale bookkeeping,
    # and — the failure this test itself had — an exemption nobody ever checked against the tool.
    stale = (_UNVALIDATED_KINDS | _CATALOG_VALIDATED_KINDS) - rendered
    assert not stale, f"exempted kind(s) the chart no longer renders: {sorted(stale)}"
    assert len(_UNVALIDATED_KINDS) == _EXPECTED_SKIPPED_RESOURCES, (
        "the count CI reports as `Skipped` must match what this file claims is unvalidated; "
        "if they diverge, one of them is wrong about kubeconform rather than about the chart"
    )


def test_something_actually_scrapes_the_metrics_endpoint() -> None:
    """`/metrics` must be collected, not merely served (REV-2).

    The route has existed since DEP-4 and nothing under `deploy/` scraped it — no ServiceMonitor,
    no PodMonitor, no `prometheus.io/scrape` annotation — so every counter, gauge and histogram in
    the system was exposed and uncollected in production. That is the quiet way an observability
    story fails: the code is written, the endpoint answers, and no dashboard or alert has ever had
    a data point. Three of the metrics this repo added most recently exist specifically so an
    operator can see a degraded turn or a failing PR-gate; none of them was reaching anyone.
    """
    monitor = next(
        (
            path
            for path, text in _template_text().items()
            if "kind: ServiceMonitor" in text or "PodMonitor" in text
        ),
        None,
    )
    assert monitor is not None, "no chart template collects /metrics; every metric is uncollected"


def test_the_scrape_targets_every_service_by_port_name() -> None:
    """It must select the Services that serve `/metrics`, on the port those Services name.

    By *name* rather than number, so a port change cannot silently orphan the scrape.

    And **all** of them. This assertion used to require `component: service` — the front door alone
    — on the reasoning that a connector records through `chemclaw.core.metrics_bridge`, "whose
    contract is that a metric recorded outside the front door is a no-op". That reasoning was
    false: the bridge imports a stdlib-only module, so the import succeeds in every process and a
    connector's counters were landing in a live registry nothing read. Pinning the narrow selector
    is how a wrong sentence in a docstring became a wrong deployment and stayed one.
    """
    text = (CHART / "templates" / "servicemonitor.yaml").read_text()
    assert "app.kubernetes.io/component:" not in text.split("spec:", 1)[1], (
        "the scrape selects one component, so every other Service in the release goes uncollected"
    )
    assert re.search(r"^\s*- port: http\s*$", text, flags=re.MULTILINE), (
        "the scrape names a port number rather than the Service's `http` port name"
    )
    for template in ("service-route.yaml", "deployment-connectors.yaml"):
        service = (CHART / "templates" / template).read_text()
        assert re.search(r"^\s*- name: http\s*$", service, flags=re.MULTILINE), (
            f"{template}'s Service no longer names its port `http`, so the ServiceMonitor "
            "selects it and scrapes nothing"
        )


def test_every_worker_is_probed_and_scraped() -> None:
    """The processes with no Service are exactly the ones that were invisible.

    Three chart templates asserted "no probes: liveness is the Temporal poll itself" — an intent
    nothing enforced. A worker whose poll loop died held its process open, so Kubernetes reported
    `Running`, no probe disagreed, and no metric reached anyone either: the two gaps hid each other,
    because both were waiting on the same missing HTTP surface.

    Asserted through the shared helper rather than per template, which is the point of there being
    one: a connector bundle enabled tomorrow is probed and scraped without an edit here.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert 'define "chemclaw.workerProbes"' in helpers

    def _includes(text: str, helper: str) -> bool:
        """Whether `text` *invokes* the helper, as opposed to mentioning it.

        Matched as a template action anchored to its line, because both worker templates also
        name these helpers in their explanatory comments — and a substring check let a mutation
        that deleted the connector worker's probes pass on the strength of the comment describing
        them. A test a comment can satisfy is a test of the comment.
        """
        return re.search(rf'^\s*\{{\{{-\s*include "{helper}"', text, flags=re.MULTILINE) is not None

    for path in ("deployment-workers.yaml", "deployment-connectors.yaml"):
        text = (CHART / "templates" / path).read_text()
        assert _includes(text, "chemclaw.workerProbes"), f"{path} renders a worker with no probes"
        assert _includes(text, "chemclaw.workerMetricsEnv"), (
            f"{path}'s worker does not receive CHEMCLAW_WORKER_METRICS_PORT, so `worker_http` "
            "binds a port the container never declared"
        )
    monitor = (CHART / "templates" / "podmonitor.yaml").read_text()
    assert re.search(r"^\s*- port: metrics\s*$", monitor, flags=re.MULTILINE), (
        "the PodMonitor does not name the `metrics` container port the workers declare"
    )
    assert "name: metrics" in helpers, (
        "the workers no longer declare a `metrics` container port, so the PodMonitor selects "
        "pods and scrapes none of them"
    )


def test_the_scraped_path_is_a_route_the_app_serves() -> None:
    """The executed half: the path the chart scrapes has to exist on the real app (D-142).

    A ServiceMonitor naming `/metric` renders, validates, deploys, and collects nothing forever —
    Prometheus reports the target as down and an operator reads it as a broken pod. Nothing in the
    chart can catch that, because the chart has no idea what routes the app declares. This is the
    same lesson as the OTel crash loop: a production value has to be executed, not type-checked.

    One `monitoring.path` now reaches three kinds of process, so all three are checked against the
    real app they run: the front door, a connector's MCP server, and a worker's probe surface.
    """
    from mcp.server.fastmcp import FastMCP

    from chemclaw.api.app import create_app
    from chemclaw.connectors.server import connector_app
    from chemclaw.core.worker_http import _build_app

    path = _values()["monitoring"]["path"]
    apps = {
        "front door": create_app(),
        "connector server": connector_app(FastMCP("probe"), name="probe"),
        "worker": _build_app("probe", lambda: True),
    }
    for label, app in apps.items():
        routes = {getattr(route, "path", None) for route in app.routes}
        assert path in routes, (
            f"the chart scrapes {path!r}, which the {label} does not serve; its metric-ish routes "
            f"are {sorted(r for r in routes if r and 'metric' in r)}"
        )


# Every file that carries a pod template, with how many pod specs it declares. Explicit rather than
# discovered, so *adding* a pod spec without its security context is a failing test rather than a
# silently-unchecked new workload.
_POD_SPECS: dict[str, int] = {
    "deployment-service.yaml": 1,
    "deployment-workers.yaml": 1,
    "deployment-connectors.yaml": 2,  # the MCP server and the bundle's Temporal worker
    "migrate-job.yaml": 1,
    "schedules-job.yaml": 1,
}


def test_every_pod_spec_declares_the_restricted_profile() -> None:
    """A `restricted` PSA namespace rejects a pod that does not *declare* it runs as non-root.

    The image has run as a non-root UID since F6-T1, and that is a different statement from the pod
    saying it must — Pod Security Admission reads the declaration. With none of these present, a
    namespace labelled `pod-security.kubernetes.io/enforce=restricted` (the default posture for a
    regulated OpenShift cluster) rejected every workload in this chart. The image being correct is
    what made it easy to miss: nothing fails until admission, in someone else's cluster.
    """
    for filename, expected in _POD_SPECS.items():
        text = (CHART / "templates" / filename).read_text()
        found = text.count('include "chemclaw.podSecurityContext"')
        assert found == expected, (
            f"{filename}: {found} pod securityContext blocks, expected {expected}"
        )


def test_every_container_drops_its_capabilities() -> None:
    """The container half of the same profile, on main containers, init containers and sidecars.

    PSA evaluates *every* container in the pod, so a compliant app container beside a sidecar that
    declares nothing still fails admission. The knowledge-sync sidecar and the two init containers
    are as much a part of this chart's attack surface as the app.
    """
    containers = sum(
        (CHART / "templates" / name)
        .read_text()
        .count('include "chemclaw.containerSecurityContext"')
        for name in [*_POD_SPECS, "_helpers.tpl"]
    )
    # 6 main containers (one per pod spec, two in the connectors file) + 3 helper-defined
    # containers: the knowledge-sync init, the refresh sidecar, and the note-repo init.
    assert containers == 9, f"{containers} containers declare a security context, expected 9"


def test_the_restricted_profile_itself_is_not_a_toggle() -> None:
    """`runAsNonRoot`/`drop: ALL`/`seccompProfile` are asserted, never read from values.

    A chart that lets a deployment switch off `allowPrivilegeEscalation: false` is offering a
    footgun rather than a knob. Only `readOnlyRootFilesystem` — which is *not* part of the
    restricted profile and cannot be defaulted on while the workers shell out to xtb/crest — is
    configurable, and it is documented in `values.yaml` with what must be provisioned first.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    profile = helpers.split('define "chemclaw.podSecurityContext"')[1].split("{{- end -}}")[0]
    container = helpers.split('define "chemclaw.containerSecurityContext"')[1].split("{{- end -}}")[
        0
    ]
    assert "runAsNonRoot: true" in profile and "RuntimeDefault" in profile
    assert "allowPrivilegeEscalation: false" in container and "- ALL" in container
    assert ".Values" not in profile, "the restricted profile must not be switchable"
    assert _values()["securityContext"]["readOnlyRootFilesystem"] is False


def test_the_front_door_has_an_ingress_policy_at_all() -> None:
    """`/metrics` is unauthenticated *because* a NetworkPolicy is said to contain it.

    `api/app.py` justifies serving `/metrics` without auth on the grounds that the NetworkPolicy
    keeps it inside the cluster. The chart's only policy declared `policyTypes: [Egress]`, so no
    ingress rule existed and any pod in any namespace could read live session counts, token totals
    and pool state. The justification named a control that was never written.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    assert "-service-ingress" in policy, "the front door has no ingress NetworkPolicy"
    assert "app.kubernetes.io/component: service" in policy
    assert _values()["networkPolicy"]["serviceIngress"]["enabled"] is True


def test_a_new_listening_port_came_with_the_rule_that_bounds_it() -> None:
    """Opening a port on the worker pods without an ingress rule would have widened the fleet.

    A NetworkPolicy only restricts pods that some Ingress-typed rule selects, and the workers were
    selected by none — so before they had a listener, "accepts everything" was true and harmless.
    Giving them `/healthz`, `/readyz` and `/metrics` is what made it matter, so the rule ships in
    the same change as the port rather than as a follow-up
    (D-2026-08-01-every-process-carries-its-own-witness).

    The scraper is granted through `monitoringNamespaces` and not `ingressNamespaces`, which is the
    whole reason the second list exists: the front door needs the router *and* the scraper, and
    nothing else in the chart should be reachable from the router.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    assert "-worker-ingress" in policy, (
        "the workers now listen on a port and no ingress rule selects them"
    )
    assert "background-worker" in policy and "connector-worker-" in policy, (
        "the worker ingress rule misses one of the two worker kinds"
    )
    assert ".Values.workerMetricsPort" in policy, (
        "the rule names a port literal rather than the value the containers bind"
    )
    assert policy.count(".Values.networkPolicy.monitoringNamespaces") == 2, (
        "the scraper must be granted on both the worker probe port and the connector port — a "
        "connector serves /metrics on the port its Service already exposes"
    )
    assert _values()["networkPolicy"]["monitoringNamespaces"], (
        "no namespace may scrape anything but the front door, so every metric this change "
        "exposed is collected by nobody — the exact failure it set out to fix"
    )


def test_a_drain_outlasts_the_work_it_interrupts() -> None:
    """The default 30 s grace period against a 600 s turn and a 120 s activity drain.

    Every rolling update, node drain and scale-down SIGKILLed whatever was in flight — and for the
    front door that is worse than lost capacity, because the conversation state that would make a
    turn resumable (attachments, harness todos, the live `AgentSession`) lives in the pod's memory
    by design (D-121). For a worker it means Temporal re-runs the activity only after its
    start-to-close timeout elapses, so the deploy stalls a job for no reason but how it was killed.

    Both grace periods are *derived* from the budget they must outlast, so raising one raises the
    other. A hand-written number is the failure being avoided: a setting that looks configured and
    is silently overridden by a kubelet timer nobody thought to move.
    """
    service = (CHART / "templates" / "deployment-service.yaml").read_text()
    assert "CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS" in service, (
        "the front door's grace period is a literal, so raising the turn budget starts SIGKILLing "
        "turns at the old number"
    )
    assert "preStop" in service and ".Values.service.drainSeconds" in service, (
        "no preStop hook: the Endpoint is removed and SIGTERM sent concurrently, so the router "
        "keeps routing to a pod that has stopped accepting"
    )
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert "CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS" in helpers, (
        "the workers' grace period does not follow the drain budget the worker itself honours"
    )
    for path in ("deployment-workers.yaml", "deployment-connectors.yaml"):
        text = (CHART / "templates" / path).read_text()
        assert re.search(r'^\s*\{\{-\s*include "chemclaw.workerGracePeriod"', text, re.MULTILINE), (
            f"{path}'s worker keeps the 30 s default, which SIGKILLs through its own drain"
        )

    # The two keys the derivations read must exist, or `int nil` renders 0 and the grace period
    # silently collapses to the margin alone.
    config = _values()["config"]
    assert int(config["CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS"]) > 0
    assert int(config["CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS"]) > 0


def test_the_drain_budget_the_chart_grants_covers_the_one_the_code_takes() -> None:
    """The two halves have to agree, and only one of them is in the chart.

    `durable/serve.py` waits `worker_graceful_shutdown_seconds` for in-flight activities; the
    kubelet SIGKILLs at `terminationGracePeriodSeconds`. If the second is not strictly larger the
    code change buys nothing — the drain is interrupted at exactly the point it was added to avoid.
    Executed against the real default rather than asserted about the YAML, because the number that
    matters is the one the worker process actually reads.
    """
    from chemclaw.core.config import settings

    granted = int(_values()["config"]["CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS"])
    assert granted == int(settings.worker_graceful_shutdown_seconds), (
        "the chart's worker drain budget and the code default disagree, so a deployment reading "
        "one and a developer reading the other are looking at different systems"
    )
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    margin = re.search(r"CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS\)\s*(\d+)", helpers)
    assert margin is not None and int(margin.group(1)) > 0, (
        "the pod's grace period equals the drain budget exactly, leaving no time for cancellation "
        "to propagate or the Postgres pool to close"
    )


def test_two_replicas_may_not_be_one_node_or_one_eviction() -> None:
    """`minReplicas: 2` bounds what the HPA runs and nothing about where it lands or what may go.

    Both replicas could be scheduled onto one node, and a drain could evict both at once — so the
    second replica bought nothing against either failure it exists for. That matters more here than
    for a stateless service: the Route pins a browser to one pod on purpose (D-121), so losing a
    node loses conversation state and not merely capacity.

    Deliberately front-door only. A PDB over the singleton background worker would be worse than
    none — `minAvailable: 1` makes it un-evictable and blocks every node drain forever — and the
    singleton is a separate open row needing a distributed checkout lock, not a policy object.
    """
    service = (CHART / "templates" / "deployment-service.yaml").read_text()
    assert "chemclaw.spreadAcrossNodes" in service, "both front-door replicas may land on one node"

    budget = (CHART / "templates" / "poddisruptionbudget.yaml").read_text()
    # As YAML keys, not as text: this template *discusses* `minAvailable` in the comment explaining
    # why it is not used, and a substring check reads that explanation as the thing it warns
    # against. The same trap as the worker-probes assertion above, hit twice on one branch.
    keys = set(re.findall(r"^\s*(minAvailable|maxUnavailable):", budget, flags=re.MULTILINE))
    assert keys == {"maxUnavailable"}, (
        "minAvailable would permit five of six pods to be evicted together once the HPA scales up; "
        "what needs bounding is how many conversations one drain can end"
    )
    assert "component: service" in budget, "the disruption budget does not select the front door"
    assert _values()["service"]["disruptionBudget"]["enabled"] is True

    workers = (CHART / "templates" / "deployment-workers.yaml").read_text()
    assert "PodDisruptionBudget" not in workers and "-background-worker" not in budget, (
        "a PDB over a replicas:1 worker either blocks every node drain in the cluster or permits "
        "exactly what no PDB permits — neither is worth rendering"
    )


def test_the_migration_hook_cannot_hold_a_release_open_forever() -> None:
    """Helm waits for a `pre-upgrade` hook, so a Job with no deadline is an unbounded wait.

    A migration that keeps failing — most often on a lock it cannot get — would retry to its
    `backoffLimit` and leave the release in `pending-upgrade`, a state that blocks every later
    `helm upgrade` and needs a recovery an operator has to already know. With a deadline the Job
    fails, Helm reports it, and `docs/guides/runbook.md` §(xi) documents the way out.

    Unlike the Deployments' grace periods this one is *not* derived, and deliberately: it bounds the
    Job including its retries, and the term it would need — how long this deployment's slowest
    `CREATE INDEX` takes on its own data — is not something the chart can know. A stated default an
    operator raises beats a formula that pretends to compute one.
    """
    job = (CHART / "templates" / "migrate-job.yaml").read_text()
    assert re.search(r"^\s*activeDeadlineSeconds:", job, flags=re.MULTILINE), (
        "the migration hook has no deadline, so a failing migration wedges the release"
    )
    settings_ = _values()["migrateJob"]
    assert settings_["activeDeadlineSeconds"] > settings_["backoffLimit"] * 60, (
        "the deadline leaves no room for the retries the same Job is configured to make"
    )


def test_the_front_door_is_launched_with_transport_bounds() -> None:
    """Three limits the application cannot impose on itself, so they have to be uvicorn flags.

    By the time a request reaches an ASGI app, the socket is accepted and the headers are parsed —
    so a connection flood, a hoard of idle keep-alives and a dribbled unbounded header block are all
    ways to exhaust the process without ever sending a request the app could refuse.
    `_BodySizeLimit` covers the request body; these cover everything before it.

    Every value comes from a setting rather than a literal in the script, for the usual reason and
    for a second one: these are the only knobs an operator must tune against the *connection* count,
    and buried in a shell script is where they would never be found.
    """
    entrypoint = (DEPLOY / "entrypoint.sh").read_text()
    for flag, setting in (
        ("--limit-concurrency", "CHEMCLAW_SERVICE_MAX_CONNECTIONS"),
        ("--timeout-keep-alive", "CHEMCLAW_SERVICE_KEEPALIVE_SECONDS"),
        ("--h11-max-incomplete-event-size", "CHEMCLAW_SERVICE_MAX_HEADER_BYTES"),
    ):
        assert flag in entrypoint, f"uvicorn is launched without {flag}"
        assert setting in entrypoint, f"{flag} is a literal rather than reading {setting}"

    from chemclaw.core.config import settings

    assert settings.service_max_connections > settings.service_max_concurrent_turns, (
        "the connection ceiling is at or below the turn cap, so a connection merely *waiting* for "
        "an admission permit would be refused at the transport — the backstop has become the policy"
    )


def test_every_pod_takes_the_same_image_reference() -> None:
    """A digest has to be honoured everywhere or it is honoured nowhere.

    `values.yaml` deployed a mutable tag and nine templates each interpolated
    `repository:tag` themselves. A tag is a pointer: `helm rollback` to a release naming `0.1.0`
    fetches whatever `0.1.0` means *now*, which is the one thing a rollback must not do — and this
    system stamps a build revision onto every audit record (AG-14), so "which bytes produced this
    result" stops being answerable the moment a tag is re-pushed.

    Asserted as "no template builds its own reference" rather than "the helper exists", because the
    failure mode is a tenth pod spec added later that interpolates the tag directly and quietly
    ignores the digest the other nine honour.
    """
    for path, text in _template_text().items():
        assert "Values.image.repository" not in text or path.name == "_helpers.tpl", (
            f"{path.name} builds its own image reference instead of `chemclaw.image`, so a pinned "
            "digest would apply to some pods and not others"
        )
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert ".Values.image.digest" in helpers, "the chart cannot deploy by digest at all"
    assert _values()["image"]["digest"] == "", (
        "a digest is committed as the default; it would be stale within weeks and every dev "
        "`helm install .` would fail on an image nobody pushed"
    )


def test_a_private_registry_is_reachable() -> None:
    """`imagePullSecrets` did not exist as a field, on any pod spec.

    An operator whose registry needs authentication had nothing to set and no signal that the chart
    assumed an open one — the pods simply fail to pull, which reads as a broken image rather than a
    missing credential. Every pod spec, because a half-covered fleet is a deployment that comes up
    partly.
    """
    for name, pods in _POD_SPECS.items():
        text = (CHART / "templates" / name).read_text()
        # Counted per pod spec, not merely present in the file: `deployment-connectors.yaml`
        # declares two, and a check that one include exists somewhere in it passes with the
        # bundle's Temporal worker left unable to pull. Found by exactly that mutation.
        found = len(
            re.findall(r'^\s*\{\{-\s*include "chemclaw.imagePullSecrets"', text, re.MULTILINE)
        )
        assert found == pods, (
            f"{name} declares {pods} pod spec(s) and {found} can pull from a private registry"
        )
    assert _values()["image"]["pullSecrets"] == [], "an image pull secret is hardcoded in the chart"


def test_the_supply_chain_has_a_gate_that_can_fail() -> None:
    """Three controls the row said were absent, and the property that makes them controls.

    Each is *blocking*. A non-blocking scanner is a scanner nobody reads, which is the same failure
    the ServiceMonitor row had one layer down: the control exists, produces output, and reports to
    nobody. `make deps-audit` is the same command CI runs, so a developer can reproduce a red build
    rather than guessing at it.
    """
    image_workflow = (DEPLOY.parent / ".github" / "workflows" / "image.yml").read_text()
    assert "pip-audit" in image_workflow, "no dependency scan"
    assert "syft" in image_workflow and "upload-artifact" in image_workflow, "no retained SBOM"
    assert "trivy image" in image_workflow, "no image scan"
    assert "--exit-code 1" in image_workflow, (
        "the image scan reports and never fails, which is a badge rather than a gate"
    )
    assert "deps-audit:" in (DEPLOY.parent / "Makefile").read_text(), (
        "CI runs a scan a developer cannot run locally"
    )


def test_the_licence_decision_is_a_build_flag_rather_than_an_edit() -> None:
    """Shipping crest (GPL-3.0) is the product owner's call, not this file's.

    The Containerfile's instruction was "drop the crest layer", which means editing a `RUN` block —
    so taking the decision looked like writing a patch, and it was therefore never taken. It is now
    `--build-arg INCLUDE_CREST=false`, and `calc.crest_cli` already reports unavailable rather than
    failing, so the image loses conformer sampling and nothing else.
    """
    containerfile = (DEPLOY / "Containerfile").read_text()
    assert "ARG INCLUDE_CREST" in containerfile
    assert 'if [ "${INCLUDE_CREST}" = "true" ]' in containerfile, (
        "the crest layer is unconditional, so declining to ship GPL-3.0 needs a code change"
    )
    assert "ARG BASE_IMAGE" in containerfile and "FROM ${BASE_IMAGE}" in containerfile, (
        "the base image cannot be pinned by digest without editing the Containerfile"
    )


def test_egress_destinations_are_declarable() -> None:
    """`to: []` in a NetworkPolicy means *any destination*, not none.

    The egress rule shipped as `to: []` on the HTTPS/LLM/Temporal/Postgres ports, so TCP/443 to the
    whole internet was permitted from every pod — while `tests/test_no_egress.py` enforced D-089
    ("this system takes no external sources") by scanning source code for host literals. A source
    scan catches a developer adding a data source and catches nothing at runtime.

    The addresses are deployment-specific, so the chart cannot default them; what it can do is
    make the choice visible and available rather than silent.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    assert ".Values.networkPolicy.egressDestinations" in policy
    assert _values()["networkPolicy"]["egressDestinations"] == []


def test_dns_egress_survives_narrowing_the_destinations() -> None:
    """DNS is its own rule, so scoping the destinations cannot take name resolution with it.

    Folded into the destination-scoped rule, setting `egressDestinations` to the database CIDR
    would silently stop DNS — which presents as every dependency being unreachable at once, the
    hardest possible symptom to trace back to a values change.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    egress = policy.split("policyTypes:")[1].split("---")[0]
    dns_rule, scoped_rule = egress.split("- to:")[1], egress.split("- to:")[2]
    assert "port: 53" in dns_rule and "egressDestinations" not in dns_rule
    assert "egressDestinations" in scoped_rule and "port: 53" not in scoped_rule


def test_the_metrics_that_were_designed_to_alert_actually_alert() -> None:
    """Collected-and-un-alerted is the same failure REV-2 fixed one level down.

    The ServiceMonitor made the metrics visible; not one of them fired anything, because no
    PrometheusRule existed anywhere in the repo. The clearest case is the audit-sink failure
    counter, whose emitter logs a stable `audit_sink_failure` marker at ERROR with a comment
    saying a lost GxP audit record "must be ALERTABLE" — and nothing was watching.

    Pinned by metric name rather than by rule count so renaming a metric without moving its alert
    fails here, which is the drift that makes an alerting stack quietly stop covering anything.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    assert "kind: PrometheusRule" in rule
    for metric in [
        "chemclaw_audit_sink_failures_total",
        "chemclaw_notes_publish_failures_total",
        "chemclaw_turn_claim_refresh_failures_total",
        "chemclaw_rollback_watermark_unavailable_total",
        "chemclaw_turns_failed_total",
        "chemclaw_turns_shed_total",
        "chemclaw_connectors_unhealthy",
        "chemclaw_db_unavailable_total",
        "chemclaw_tokens_total",
    ]:
        assert metric in rule, f"{metric} has no alert"


def test_every_alerted_metric_is_a_metric_the_app_declares() -> None:
    """The other direction: an alert on a metric that does not exist never fires and looks fine.

    A PromQL expression naming a typo'd or deleted series is silently always-empty — the alert is
    green forever, which reads exactly like "the condition never occurred".
    """
    from chemclaw.api.metrics import _COUNTERS, _GAUGES, _HISTOGRAMS

    declared = {*_COUNTERS, *_GAUGES, *_HISTOGRAMS}
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    # Only the PromQL, not the prose: the annotations legitimately name metrics in explanations.
    expressions = " ".join(re.findall(r"expr:\s*(?:>-\s*)?((?:.|\n)*?)\n\s*for:", rule))
    referenced = set(re.findall(r"\b(chemclaw_[a-z_]+)\b", expressions))
    assert referenced, "no PromQL expressions were parsed — the extraction is broken, not the rules"
    unknown = referenced - declared
    assert not unknown, f"alerts reference metrics the app never emits: {sorted(unknown)}"
