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

import os
import re
import shutil
import subprocess
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
_RUNTIME_DATA = ("data", "skills", "knowledge", "infra", "schema")


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
    rather than grew. Five entries now: `data/` is every corpus the code reads; `skills/`,
    `knowledge/` and `infra/` are the three that are not configuration (layers 3 and 4, and this
    system's own SQL); and `schema/` is the DDL for stores it does *not* own, which was missing from
    the image while `publish/drivers/sql.py` told operators to generate it from inside one.
    """
    root = DEPLOY.parent
    for required in _RUNTIME_DATA:
        assert (root / required).is_dir(), (
            f"Containerfile COPYs {required}/ but no such directory exists at the repository root"
        )


def test_the_ignore_file_sits_where_every_builder_that_ships_here_reads_it() -> None:
    """`deploy/.dockerignore` was inert: nothing reads an ignore file from there.

    Docker, buildah, podman and kaniko all read the ignore file from the **root of the build
    context** — and every call site (`.github/workflows/image.yml`, `deploy/jenkins/lib/image.sh`)
    passes the repository root as the context. BuildKit alone also honours
    `<dockerfile-path>.dockerignore`, which is why `deploy/Containerfile.dockerignore` looks like
    the tidy answer and is not: `IMAGE_BUILDER` offers four builders and three of them would go on
    ignoring it.

    Inert, the context is the whole tree — 6.9 GB of it, `.venv` and `.git` included, plus any root
    `.env`, `*.pem` or `*.key` — sent to the daemon or, under `IMAGE_BUILDER=kaniko`, uploaded to a
    shared builder. Nothing lands *in* the image (the `COPY` set is explicit), so what this costs is
    exposure of the context and the transfer, not a contaminated image. The file's own header says
    "keep the build context lean and secret-free", which is the claim being restored rather than
    made.
    """
    root = DEPLOY.parent
    ignore = root / ".dockerignore"
    assert ignore.is_file(), (
        "no .dockerignore at the repository root, which is the only placement all four supported "
        "builders read; an ignore file anywhere else is a file nothing opens"
    )
    assert not (DEPLOY / ".dockerignore").exists(), (
        "deploy/.dockerignore is back, and no builder reads an ignore file from there"
    )
    # The context every builder is given, read from the call sites rather than assumed: an ignore
    # file at the root is only the right placement while the root is the context.
    workflow = (root / ".github" / "workflows" / "image.yml").read_text()
    assert re.search(
        r"docker build -f deploy/Containerfile[^\n]*(\\\n[^\n]*)*\s\.\s*$", workflow, re.M
    ), "the CI build no longer passes the repository root as its context"
    entries = {
        line.strip()
        for line in ignore.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    # The four that carry the cost or the secret. Everything else in the file is housekeeping.
    assert {".git", ".venv", ".env", "*.pem"} <= entries, (
        f"the ignore file no longer excludes the context's expensive or secret entries: {entries}"
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

    `core/config/` has always said the F6 image build injects the revision, and until REV-17
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
        assert "enabled" in entries[name]
        # A count for every half this release actually pods, and none for a half it does not. A
        # `url:` entry renders no Deployment and no Service by design
        # (`deployment-connectors.yaml` gates on `and $cfg.server (not $cfg.url)`), so a server
        # count on one would declare a number of pods that never exist — the kind of value a
        # reader would later try to tune.
        #
        # **The worker half is asked for separately, and that is the hole this used to leave.**
        # The worker block is deliberately *not* conditioned on `url` — durable jobs run on our own
        # Temporal queue whoever hosts the tools — while the only count this test demanded was
        # skipped entirely for a `url:` bundle. One such bundle owning durable work would have
        # rendered an empty `replicas` (Kubernetes reads that as 1) and contributed `nil | int` = 0
        # to `chemclaw.fleetPools`, so the connection budget would have been short by exactly
        # the pods that were running. No shipped bundle is that shape today, which is why it was
        # latent; the shape is legal, which is why it is checked.
        entry = entries[name]
        if entry.get("server") and not entry.get("url"):
            assert "serverReplicas" in entry or "replicas" in entry, (
                f"{name}: server pods with no count to render"
            )
        if entry.get("worker"):
            assert "workerReplicas" in entry or "replicas" in entry, (
                f"{name}: worker pods with no count to render"
            )


def test_a_connectors_two_deployments_are_sized_separately() -> None:
    """One knob drove two differently-shaped Deployments, and each cost connections.

    `calc` serves MCP requests from one Deployment and polls a Temporal queue from another. Both
    read `$cfg.replicas`, so scaling the server to 4 for request load also ran four queue pollers
    and spent four more of `postgres.maxConnections` — a change nobody asked for, made by the
    knob's shape rather than by anyone's intent.

    Asserted on the template text because rendering needs `helm`, which this offline suite does
    not have. Both halves must fall back to `replicas`: every bundle shipped here sizes its two
    halves the same, and the split must not turn that into two values to keep in step.
    """
    template = (CHART / "templates" / "deployment-connectors.yaml").read_text()
    app, worker = (
        template.split("app.kubernetes.io/component: connector-worker-")[0],
        (template.split("kind: Deployment")[-1]),
    )
    assert "$cfg.serverReplicas | default $cfg.replicas" in app
    assert "$cfg.workerReplicas | default $cfg.replicas" in worker
    assert "$cfg.serverReplicas" not in worker, "the worker Deployment reads the server's knob"
    assert "$cfg.workerReplicas" not in app, "the app Deployment reads the worker's knob"
    # And neither may render empty: `replicas:` with no value is 1 to Kubernetes and 0 to the
    # connection budget, which is the pair of wrong answers this is here to prevent.
    assert template.count("| required (printf ") == 2


def test_an_externally_hosted_connector_gets_no_pods_and_no_service() -> None:
    """`url` on a bundle means somebody else runs its server, so the app half must not render.

    Such a bundle still declares an `endpoint:` and therefore still carries `server: true` — that
    flag mirrors the manifest and nothing else (the test above). What it must not get is a
    Deployment running `uvicorn connectors.<name>.server.app:app` from our image against a module
    that does not exist, plus a Service selecting pods that will never appear.

    Pinned as the *absence of an unguarded conditional* rather than the presence of a guarded one:
    the failure this prevents is a future edit reverting a block to a bare `if $cfg.server`, and
    only counting both forms can see that. The rendered proof is `make helm-validate`, which runs
    `helm` — this suite is the offline half and has no renderer.
    """
    template = (CHART / "templates" / "deployment-connectors.yaml").read_text()
    assert template.count("{{- if and $cfg.server (not $cfg.url) }}") == 2, (
        "the app Deployment and the Service must both be conditioned on `url` being unset"
    )
    assert "{{- if $cfg.server }}" not in template, (
        "a `server` block is rendered without checking `url`, so a connector this release does "
        "not run would get a crash-looping pod and a Service selecting nothing"
    )
    # The worker is deliberately *not* guarded: a bundle's durable jobs run on our own Temporal
    # queue whoever hosts its MCP tools, so an external endpoint must not take its worker away.
    assert "{{- if $cfg.worker }}" in template


def test_an_externally_hosted_connector_is_dialled_where_the_operator_says() -> None:
    """The address map must follow the same `url` the pods do, or it names a Service that is absent.

    This is the half that fails silently. `connectors/registry.py::_endpoint_url` lets the computed
    override beat the manifest's own URL, so a chart that kept computing an in-cluster address for
    an externally hosted bundle would point the front door at a name resolving to nothing — and the
    connector degrades rather than erroring (`connectors/transport.py`), so the symptom is a
    capability that is quietly missing from every turn.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    _, _, definition = helpers.partition('define "chemclaw.connectorUrls"')
    definition, _, _ = definition.partition("{{- end -}}\n\n")
    assert "$cfg.url" in definition, (
        "chemclaw.connectorUrls still computes a Service address for every enabled server, "
        "including bundles this release does not run"
    )


def test_an_externally_hosted_connector_is_not_counted_against_the_connection_ceiling() -> None:
    """A pod that does not exist may not spend the fleet's Postgres budget.

    `chemclaw.fleetPools` multiplies into the ceiling `Settings` refuses to exceed, so an
    over-count is not cosmetic: it shrinks the pool every real pod is allowed, or trips the refusal
    outright and CrashLoops the release.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    _, _, definition = helpers.partition('define "chemclaw.fleetPools"')
    assert "if and $cfg.server (not $cfg.url)" in definition, (
        "chemclaw.fleetPools counts a server pod for an externally hosted bundle"
    )


def test_a_connector_url_is_only_declared_beside_a_server() -> None:
    """`url` on a bundle with no `server` would silently do nothing.

    `chemclaw.connectorUrls` only visits `enabled && server` entries, so an operator who set `url`
    on a jobs-only bundle (`qm`) would get no error and no effect — the front door would keep the
    manifest's own address. Vacuous over the shipped values by design: every bundle here is ours.
    It exists so the first entry that sets `url` is checked against the one shape it works in.
    """
    for name, cfg in _values()["connectors"].items():
        if cfg.get("url"):
            assert cfg.get("server"), (
                f"connector {name!r} sets `url` without `server: true`; the address map ignores "
                "it, so the front door would keep dialling the manifest's dev default"
            )


def test_knowledge_volume_is_mounted_on_every_reading_component() -> None:
    """Readers resolve the graph as a local directory, so each needs the synced volume (DEP-1)."""
    for template in ("deployment-service.yaml", "deployment-workers.yaml"):
        text = (CHART / "templates" / template).read_text()
        assert 'include "chemclaw.knowledgeMounts"' in text, template
        assert 'include "chemclaw.knowledgeInit"' in text, template


def test_note_repo_clone_exists_wherever_notes_are_submitted() -> None:
    """The front door and the background worker both call `record_note`, so both need a clone."""
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
    the live `TurnSession` handle. Land the follow-up request on a sibling pod and the agent
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


def test_disabling_a_connector_takes_its_tools_off_the_agent_too() -> None:
    """`enabled: false` removed the pods and left the tool advertised, for as long as it existed.

    `values.yaml` said `CHEMCLAW_CONNECTORS_ENABLED` lived "in `config` below" and it was in none
    of the entries there — so the agent ran the setting's default, which is the empty string, which
    `registry.enabled()` reads as *every discovered bundle* ("discovery is enablement until you say
    otherwise"). A disabled `qm` therefore still advertised its jobs: the launcher started the
    wrapper on the polled queue and its child on `connector-qm`, which nobody polls, and the
    chemist was told "running" until the 25 h ceiling. Latent only because all seven shipped
    entries are `enabled: true`; it fires the first time anyone uses the switch the file documents.

    Derived rather than hand-written, for the same reason `CHEMCLAW_CONNECTOR_URLS` is: a second
    list of the same topology goes stale the first time a bundle is toggled, and this is the copy
    whose staleness is invisible.
    """
    config = (CHART / "templates" / "config.yaml").read_text()
    assert (
        'CHEMCLAW_CONNECTORS_ENABLED: {{ include "chemclaw.connectorsEnabled" . | quote }}'
        in config
    )
    assert "CHEMCLAW_CONNECTORS_ENABLED" not in _values()["config"], (
        "the enable list must be derived from .Values.connectors, not hand-written beside it"
    )
    helper = (CHART / "templates" / "_helpers.tpl").read_text()
    _, _, definition = helper.partition('define "chemclaw.connectorsEnabled"')
    definition = definition.split('define "chemclaw.connectorUrls"')[0]
    assert "range $name, $cfg := .Values.connectors" in definition
    assert "$cfg.enabled" in definition

    # The separator is the one `Settings.connectors_enabled_list` splits on, and it is read from
    # the code rather than retyped: a chart that joined on the wrong character would render a
    # single unknown bundle name, which `registry.enabled()` raises on at startup.
    from chemclaw.core.config import settings

    with_two = settings.model_copy(update={"connectors_enabled": "alpha:beta"})
    assert with_two.connectors_enabled_list == ["alpha", "beta"]
    assert 'join ":" $names' in definition


def test_a_release_that_enables_no_connector_is_refused_rather_than_inverted() -> None:
    """The one intent this variable cannot express, so it must not be rendered by accident.

    An empty `CHEMCLAW_CONNECTORS_ENABLED` means "every bundle the image ships". So deriving it
    from a connectors block where an operator has disabled *everything* would render the empty
    string and load all of them — the exact opposite of what was asked for, and a worse failure
    than the one this derivation fixes, because it would arrive by way of the fix.
    """
    helper = (CHART / "templates" / "_helpers.tpl").read_text()
    _, _, definition = helper.partition('define "chemclaw.connectorsEnabled"')
    definition = definition.split('define "chemclaw.connectorUrls"')[0]
    assert "{{- fail " in definition, "an all-disabled release would render as all-enabled"

    from chemclaw.core.config import settings

    assert settings.model_copy(update={"connectors_enabled": ""}).connectors_enabled_list == [], (
        "the premise of the guard — that empty is not 'none' — no longer holds"
    )


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

# Kinds the chart *can* render but does not on the shipped values, so they never reach kubeconform
# in the validation render and cannot appear in its `Skipped` count.
#
# `AlertmanagerConfig` is gated on `monitoring.alertmanager.enabled`, which is off because the chart
# cannot invent a receiver — a Slack webhook or a PagerDuty key is a deployment fact. It would be
# skipped rather than validated if it did render (the datreeio catalog carries a `v1alpha1` schema
# for it and no `v1beta1`), which is why it is recorded here rather than quietly left out: the point
# of these three sets is that every kind in the template text is accounted for by *someone*.
_UNRENDERED_BY_DEFAULT_KINDS = frozenset({"AlertmanagerConfig"})

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
    unexpected = (
        rendered
        - core_kinds
        - _CATALOG_VALIDATED_KINDS
        - _UNVALIDATED_KINDS
        - _UNRENDERED_BY_DEFAULT_KINDS
    )
    assert not unexpected, (
        f"the chart renders kind(s) {sorted(unexpected)} that kubeconform may silently skip — "
        "add a schema location, or add them to _UNVALIDATED_KINDS with the reason"
    )
    # Both exemptions must stay earned: a kind the chart stopped rendering is stale bookkeeping,
    # and — the failure this test itself had — an exemption nobody ever checked against the tool.
    stale = (
        _UNVALIDATED_KINDS | _CATALOG_VALIDATED_KINDS | _UNRENDERED_BY_DEFAULT_KINDS
    ) - rendered
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
    # The pre-upgrade DDL Job and the post-upgrade stored-message conversion
    # (D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step).
    "migrate-job.yaml": 2,
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
    # 7 main containers (one per pod spec, two each in the connectors and migrate files) + 3
    # helper-defined containers: the knowledge-sync init, the refresh sidecar, the note-repo init.
    assert containers == 10, f"{containers} containers declare a security context, expected 10"


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
    """Something must bound who may open a connection to the front door.

    The chart's only policy declared `policyTypes: [Egress]`, so no ingress rule existed and any pod
    in any namespace could reach `chemclaw-service:8080`. This is the rule that closes that.

    It is *not* what makes `/metrics` safe, and this docstring used to say it was — the fourth of
    four places asserting a control that does not hold. A NetworkPolicy selects peers, not paths;
    the peer it must allow is the router; and the Route declares no `spec.path`, so every path the
    front door serves is published on the external host. What bounds the exposition is D-152's
    declared-label allowlist, asserted in `tests/test_metrics.py`, and `route.ipWhitelist` is the
    only control at this layer (`tests/test_helm_chart.py`).
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
    turn resumable (attachments, harness todos, the live `TurnSession`) lives in the pod's memory
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
    # `[\s)]*` rather than one `\)`: the key is wrapped in `required` so its absence refuses the
    # render instead of silently rendering `int nil` = 0, which closes the parenthesis twice.
    margin = re.search(r"CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS[\s)]*(\d+)", helpers)
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
    # Comments stripped first, for the same reason the `minAvailable` check above parses keys: this
    # template's whole argument is *about* the background worker, and one of the ADRs it now cites
    # has that name inside its own slug. A substring check over the prose reads the explanation as
    # the thing it warns against — the trap this test already documents, met a third time.
    rendered_body = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", budget, flags=re.DOTALL)
    assert "PodDisruptionBudget" not in workers and "-background-worker" not in rendered_body, (
        "a PDB over a replicas:1 worker either blocks every node drain in the cluster or permits "
        "exactly what no PDB permits — neither is worth rendering"
    )


def test_the_shipped_fleet_ceiling_matches_the_fleet_the_chart_renders() -> None:
    """The chart may not declare a ceiling its own autoscaling shape exceeds.

    The admission cap is per-process by design (SCALE-1 rejected a fleet-wide counter as a durable
    write and a heartbeat per turn to bound a resource). Its consequence was left unstated: the load
    the shared LLM endpoint sees is `maxReplicas × uvicorn workers × the cap`, and with the shipped
    values that is 48 while the only number anyone reads is 8.

    `Settings` now refuses a configuration whose product exceeds the declared ceiling — but a pod
    only validates the values it was *given*, so raising `maxReplicas` here would ship a chart that
    CrashLoops every front-door pod on first deploy. This is the check that catches it before then,
    against the same arithmetic the validator performs.
    """
    values = _values()
    autoscaling = values["service"]["autoscaling"]
    replicas = (
        autoscaling["maxReplicas"] if autoscaling["enabled"] else values["service"]["replicas"]
    )
    workers = int(values["config"].get("CHEMCLAW_SERVICE_UVICORN_WORKERS", 1))
    per_process = int(values["config"].get("CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS", 8))
    declared = int(values["config"]["CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS"])

    assert replicas * workers * per_process <= declared, (
        f"the chart scales to {replicas} replicas × {workers} worker(s) × {per_process} turns = "
        f"{replicas * workers * per_process} concurrent turns against a declared ceiling of "
        f"{declared}; every front-door pod would refuse to start"
    )

    # The fleet size must be *derived* from the autoscaling block, not written beside it. A second
    # copy of `maxReplicas` in `config:` goes stale the first time someone scales the front door —
    # which is precisely the silent multiplication the ceiling exists to catch, reintroduced by the
    # mechanism meant to catch it.
    assert "CHEMCLAW_SERVICE_FLEET_REPLICAS" not in values["config"], (
        "the fleet size must be derived from service.autoscaling in templates/config.yaml, not "
        "hand-written as a second copy of maxReplicas"
    )
    config_template = (CHART / "templates" / "config.yaml").read_text()
    assert re.search(
        r"^\s*CHEMCLAW_SERVICE_FLEET_REPLICAS:.*\.Values\.service\.autoscaling\.maxReplicas",
        config_template,
        flags=re.MULTILINE,
    ), "CHEMCLAW_SERVICE_FLEET_REPLICAS does not come from the number the HPA obeys"


def test_the_fleet_ceiling_has_a_runtime_check_config_validation_cannot_do() -> None:
    """Startup validation sees the rendered shape once; a cluster keeps changing after that.

    `kubectl scale`, an HPA edited in place, or a rollout that leaves both generations up all push
    the fleet past its ceiling while every individual pod's configuration stays perfectly valid.
    Only summing the live per-pod capacity against the declared ceiling can see that, which is why
    the ceiling is exported as a gauge and not merely validated.
    """
    rules = (CHART / "templates" / "prometheusrule.yaml").read_text()
    assert "ChemclawFleetAboveItsTurnCeiling" in rules
    assert "sum(chemclaw_turn_capacity) > max(chemclaw_fleet_turn_ceiling)" in rules
    # Self-disabling, or every deployment that declares no ceiling alerts forever.
    assert "max(chemclaw_fleet_turn_ceiling) > 0" in rules

    from chemclaw.core.metrics import METRICS

    assert "chemclaw_fleet_turn_ceiling" in METRICS.render(), (
        "the alert compares against a gauge the app never exposes"
    )


POOLS_PER_FRONT_DOOR = 3
"""How many Postgres pools one front-door process holds.

The stores' pool, the `/readyz` probe's (`api/routes/ops.py` borrows with its own statement
timeout, and `core/db` keys a pool on `(dsn, libpq options, requested max_size)`) and the LangGraph
checkpointer's registered autocommit pool. Every other role holds one. Measured rather than
assumed: `tests/test_fleet_pools.py` drives each role's real composition root and counts.

They are not the same *width*: the `/readyz` one asks for a single connection, so a count of pools
is not a count of connections and `Settings.fleet_connections_per_server` is what converts one to
the other. This constant stays a pool count because that is what `chemclaw.fleetPools` renders.
"""


def _fleet_pools(values: dict[str, Any]) -> int:
    """The Postgres pools this chart renders — the helper's arithmetic.

    **Pools, not pods**, which is the defect this file used to share with the validator: both
    multiplied `pg_pool_max_size` by a process count, so a front door measured at three pools and
    48 connections was charged 16 and the shipped chart declared 136 against a real floor of 208.

    Kept here rather than read out of the template because the point of the test is to check the
    template against the topology *independently*; reading its own answer back would assert
    nothing.
    """
    autoscaling = values["service"]["autoscaling"]
    front_door = (
        autoscaling["maxReplicas"] if autoscaling["enabled"] else values["service"]["replicas"]
    )
    total = front_door * POOLS_PER_FRONT_DOOR
    total += values["workers"]["background"]["replicas"]
    # The face serves the same in-process read-only tools over MCP and opens the same pool. Off by
    # default, so this term is zero for the shipped values and the point of it is the release that
    # turns the switch on.
    if values["mcpFace"]["enabled"]:
        total += values["mcpFace"]["replicas"]
    for bundle in values["connectors"].values():
        if not bundle["enabled"]:
            continue
        # An externally hosted bundle (`url`) pods no server here, so it pools nothing here.
        if bundle.get("server") and not bundle.get("url"):
            total += bundle.get("serverReplicas", bundle.get("replicas"))
        # Each half at its own count: two Deployments, two knobs, and a `url:` bundle's worker
        # still pods here even though its server does not.
        if bundle.get("worker"):
            total += bundle.get("workerReplicas", bundle.get("replicas"))
    return int(total)


def test_the_shipped_connection_ceiling_matches_the_fleet_the_chart_renders() -> None:
    """The chart's own numbers must clear the validator every pod runs at startup.

    `core/config/store.py` stated "the deployment total is max_size × processes, which must stay
    under the server's max_connections" and nothing computed it, so the chart set no pool key at
    all: every pod ran the code default of 16, and seventeen pooled processes made the fleet's
    ceiling ~272 against the `max_connections=100` D-119 measured against. `Settings` now refuses
    the product — which means shipping a chart whose own values exceed it would CrashLoop every
    pod on first deploy. This is the check that catches that here instead.

    **Checked by constructing a real `Settings`, not by re-implementing the comparison.** This test
    used to assert `processes * per_pool <= declared` itself, so when the validator's own
    arithmetic counted *processes* where the front door holds three pools, this test agreed with it
    and both were wrong together — a declared 136 against a measured floor of 208. Feeding the
    rendered numbers to the validator leaves exactly one arithmetic in the repository, and any
    future correction to it lands here on the same commit.
    """
    from chemclaw.core.config import Settings

    values = _values()
    pools = _fleet_pools(values)
    per_pool = int(values["config"]["CHEMCLAW_PG_POOL_MAX_SIZE"])
    declared = int(values["postgres"]["maxConnections"])
    # The front-door replica ceiling is a *second* input to this budget, not only to the turn one:
    # one pool per front door is the `/readyz` probe's and one connection wide. Omitting it here
    # would leave the test constructing a `Settings` at the code default of one replica — passing
    # on arithmetic no rendered pod runs.
    autoscaling = values["service"]["autoscaling"]
    replicas = (
        autoscaling["maxReplicas"] if autoscaling["enabled"] else values["service"]["replicas"]
    )

    try:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_fleet_pools=pools,
            pg_pool_max_size=per_pool,
            pg_fleet_max_connections=declared,
            service_fleet_replicas=replicas,
        )
    except ValueError as exc:  # pragma: no cover - the failure this test exists to report
        pytest.fail(
            f"the shipped chart renders {pools} pools at {per_pool} connections each (bar "
            f"{replicas} readiness pools of one) against a declared ceiling of {declared}; every "
            f"pod would refuse to start with: {exc}"
        )
    # No split in the shipped chart: `sessionStoreDsn` is a Secret key nothing populates, so every
    # pool lands on one server and the second figure must be zero. A release that started
    # declaring a split here without declaring its ceiling would warn on every pod's startup.
    assert settings.fleet_connections_per_server()[1] == 0

    # Derived from the topology, never hand-written beside it — a second copy of the replica counts
    # goes stale the first time a connector is enabled, which is exactly the silent multiplication
    # the ceiling exists to catch, reintroduced by the mechanism meant to catch it.
    assert "CHEMCLAW_PG_FLEET_POOLS" not in values["config"], (
        "the fleet pool count must be derived in templates/_helpers.tpl, not hand-written"
    )
    # And not restated in prose either, which is how it went wrong: two comments in this values file
    # said "17 pooled processes" and "seventeen of them" while the helper rendered
    # 14, so the arithmetic beside `maxConnections` produced a number the chart does not render.
    # `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` is the rule; this is its pin.
    # It covers "pools" as well as "pooled processes" since 2026-09-05, because that is what the
    # helper counts now and a rename is a way for a pin to stop pinning anything.
    prose = (CHART / "values.yaml").read_text()
    restated = re.findall(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
        r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b[^.\n]{0,20}"
        r"(?:pooled process(?:es)?|pools|pool count)\b",
        prose,
        flags=re.IGNORECASE,
    )
    assert not restated, (
        f"values.yaml writes down a fleet pool count ({restated}); it is rendered by "
        "chemclaw.fleetPools and goes stale here the first time a replica count moves"
    )
    config_template = (CHART / "templates" / "config.yaml").read_text()
    assert re.search(
        r"^\s*CHEMCLAW_PG_FLEET_POOLS:.*chemclaw\.fleetPools",
        config_template,
        flags=re.MULTILINE,
    ), "CHEMCLAW_PG_FLEET_POOLS does not come from the rendered topology"

    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    _, _, definition = helpers.partition('define "chemclaw.fleetPools"')
    assert definition, "_helpers.tpl defines no chemclaw.fleetPools"
    # The front door counts at its HPA ceiling, not its floor: a budget that only holds at
    # minReplicas is a budget the fleet breaks by scaling up, which is what it is for.
    assert ".Values.service.autoscaling.maxReplicas" in definition
    # And it counts POOLS: the front-door term is multiplied by what one such process holds. This
    # is the line whose absence declared 136 for a fleet that opens 208.
    assert f"mul $frontDoor {POOLS_PER_FRONT_DOOR}" in definition, (
        "chemclaw.fleetPools counts front-door pods rather than the pools each one holds"
    )
    # And every other pooled process comes from the same blocks the Deployments do.
    assert ".Values.workers.background.replicas" in definition
    assert "range $name, $cfg := .Values.connectors" in definition
    # Each connector half at its own count, or the budget is wrong for any bundle that sizes them
    # differently — which is the whole reason the two knobs exist.
    assert "$cfg.serverReplicas | default $cfg.replicas" in definition
    assert "$cfg.workerReplicas | default $cfg.replicas" in definition


def test_the_connection_ceiling_has_a_runtime_check_config_validation_cannot_do() -> None:
    """The same blind spot the turn ceiling has, for the same reason, needing the same pair.

    Startup validation sees the shape the chart rendered, once. A `kubectl scale`, an HPA edited in
    the cluster, or a rollout leaving both generations up all push the live sum past the server's
    ceiling while every pod's own configuration stays valid.

    The saturation alert is the other half and is the older gap: `requests_waiting` has existed
    since D-119 as *the* reading that separates an undersized pool from an unreachable database,
    and nothing consumed it — so the signal was collected and never watched.
    """
    rules = (CHART / "templates" / "prometheusrule.yaml").read_text()
    assert "ChemclawFleetAboveItsConnectionCeiling" in rules
    # Each server against its own ceiling, and *not* a sum against a sum: enumerated over 200,000
    # random draws, `sum(pools) > primary + session` never fired with both servers inside their
    # ceilings and stayed silent in 49,993 where one was over — it can only miss. It also paged a
    # healthy split whose second ceiling was undeclared, pointing remediation at the wrong server.
    # The primary's side is the total minus the split store's; with no split the subtrahend is 0 in
    # every pod and both branches are exactly the comparison this shipped with.
    assert (
        "sum(chemclaw_pg_pool_max_size) - sum(chemclaw_pg_session_pool_max_size)" in rules
        and "> max(chemclaw_pg_fleet_max_connections)" in rules
    )
    assert (
        "sum(chemclaw_pg_session_pool_max_size)" in rules
        and "> max(chemclaw_pg_session_fleet_max_connections)" in rules
    )
    # Self-disabling on the second ceiling too, or a split with none declared alerts forever.
    assert "max(chemclaw_pg_session_fleet_max_connections) > 0" in rules
    assert (
        "max(chemclaw_pg_fleet_max_connections) + max(chemclaw_pg_session_fleet_max_connections)"
        not in rules
    ), "the summed comparison is back; it can only miss (see this test's docstring)"
    # Self-disabling, or every deployment that declares no ceiling alerts forever.
    assert "max(chemclaw_pg_fleet_max_connections) > 0" in rules
    assert "ChemclawPgPoolSaturated" in rules
    assert "max(chemclaw_pg_pool_requests_waiting) > 0" in rules

    from chemclaw.core.db import bind_pool_metrics
    from chemclaw.core.metrics import METRICS

    # Bound explicitly: an unbound gauge is omitted from the exposition, so asserting on the shared
    # registry without this would depend on whether some earlier test opened a pool.
    bind_pool_metrics()
    rendered = METRICS.render()
    for gauge in (
        "chemclaw_pg_pool_max_size",
        "chemclaw_pg_session_pool_max_size",
        "chemclaw_pg_fleet_max_connections",
        "chemclaw_pg_session_fleet_max_connections",
    ):
        assert gauge in rendered, f"the alert compares against {gauge}, which the app never exposes"


def test_the_singleton_worker_is_a_singleton_across_a_rollout_too() -> None:
    """`replicas: 1` is not one process; it is one process *at steady state*.

    No Deployment in this chart declared a `strategy`, so all of them take the Kubernetes default
    `RollingUpdate` with `maxSurge: 25%` / `maxUnavailable: 25%` — which for a single replica rounds
    to `maxSurge: 1, maxUnavailable: 0`: the new pod is started and becomes Ready *before* the old
    one is told to stop, and the old one then has up to its `terminationGracePeriodSeconds` (150) to
    finish. Two background workers poll `background-jobs` for that whole window.

    That is exactly the interleaving `values.yaml` and
    `D-2026-08-27-what-a-second-background-worker-would-race-on` pin the replica count to prevent.
    `NoteReindexWorkflow` retires `note_index` rows for notes missing from *this pod's* knowledge
    checkout — an `emptyDir` its own sidecar refreshes on an interval — so during the overlap the
    new pod's clone is fresh and the old pod's is up to an interval stale, and a merge-webhook
    reindex landing on the old one deletes the freshly merged notes' rows while logging that it
    retired notes that exist. The ADR's "one pod's clone only ever moves forward" is true at steady
    state and false during a rollout, which is the gap this closes.

    `Recreate` rather than `maxSurge: 0`: a singleton worker has no availability to protect —
    Temporal redelivers an activity whose worker vanished — so the honest statement is that the old
    process is gone before the new one starts.
    """
    text = (CHART / "templates" / "deployment-workers.yaml").read_text()
    assert _values()["workers"]["background"]["replicas"] == 1, (
        "the background worker is no longer pinned to one replica; this test's premise is gone"
    )
    strategy = re.search(r"^  strategy:\n\s+type: (\w+)", text, flags=re.MULTILINE)
    assert strategy and strategy.group(1) == "Recreate", (
        "the background worker takes the default RollingUpdate, which starts the second pod before "
        "stopping the first — two workers on `background-jobs` for a whole grace period"
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


def _jenkins_render_flags() -> list[str]:
    """Every `--set` the release pipeline's render stage can emit, with all its postures stated.

    Read out of the `Jenkinsfile` rather than restated here: the point of the test below is that
    the *pipeline's own* flags are enough to render this chart, so a copy of them would be a second
    answer to the question and would stay green while the pipeline broke. The interpolated pair
    (`image.digest`/`image.repository`) is dropped — a validation render has no published digest,
    and neither is a posture the chart refuses to render without.
    """
    # Split on the stage declarations at their own indentation, not on the bare string: a stage
    # name quoted inside a comment in the body would otherwise truncate the block being read.
    blocks = re.split(r"\n    stage\('", (DEPLOY.parent / "Jenkinsfile").read_text())
    stage = next(block for block in blocks if block.startswith("Render the chart')"))
    flags: list[str] = []
    for match in re.finditer(r"--set ([A-Za-z0-9_.]+)=([A-Za-z0-9_.:/-]+)", stage):
        if "$" in match.group(0) or match.group(1).startswith("image."):
            continue
        flags += ["--set", f"{match.group(1)}={match.group(2)}"]
    return flags


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_release_pipeline_can_state_every_posture_the_chart_demands() -> None:
    """The chart refuses to render until a release states a posture, and there were two of them.

    `templates/networkpolicy.yaml` refuses without an egress posture and `templates/config.yaml`
    refuses without a retention posture — both deliberate
    (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`). The pipeline grew a parameter and an
    `egress_flags()` helper for the first and **nothing at all** for the second, and
    `deploy/jenkins/environments/` ships empty by design, so `fileExists(VALUES_FILE)` is false and
    no `--values` supplies it either. Every `DEPLOY_TARGET=openshift` run therefore died in
    `stage('Render the chart')`: the system could not be deployed by its own delivery pipeline.

    Rendered with the pipeline's flags rather than asserted as strings, because "a parameter named
    `ACCEPT_UNBOUNDED_GROWTH` exists" is not the claim — the claim is that what the pipeline can say
    is enough for the chart to render, and only helm answers that. A third posture guard added to
    the chart later fails this test with the message the operator would have got in the namespace.
    """
    flags = _jenkins_render_flags()
    result = subprocess.run(
        ["helm", "template", "chemclaw", str(CHART), *flags],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the release pipeline cannot render this chart with every posture parameter turned on; "
        f"it passes {flags} and helm says:\n{result.stderr}"
    )

    # And the deploy half must be able to say the same things, or the pipeline renders one release
    # and applies another. `openshift.sh` builds its own flags because it runs from the descriptor,
    # not from the render stage's shell.
    script = (DEPLOY / "jenkins" / "targets" / "openshift.sh").read_text()
    for index in range(0, len(flags), 2):
        key = flags[index + 1].split("=", 1)[0]
        assert key in script, (
            f"the render stage states {key} and `openshift.sh` cannot, so `helm upgrade` applies a "
            "release the pipeline never validated"
        )


def test_no_delivery_script_deploys_this_chart_atomically() -> None:
    """`--atomic` turns the `post-upgrade` convert Job back into a release gate it was moved out of.

    `chemclaw-convert` is a `post-upgrade` hook for one measured reason
    (D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step): it rewrites
    `session_messages` rows into a shape the *previous* release's reader raises on, so a rollback
    after it has run leaves a converted table behind a reader that cannot read it. Helm neither
    undoes a data conversion nor re-runs the hook. `--atomic` rolls back on any failed hook, so a
    backfill that merely hits its `activeDeadlineSeconds` takes a healthy release with it.

    `migrate-job.yaml` says this at the point of the annotation — "so do not run this chart with
    `--atomic`" — and the shipped delivery script did exactly that. A sentence in a template is not
    a control over a script in another directory, which is what this test is.

    The scanned set is every file that runs `helm` against this chart, not every `*.sh` under
    `deploy/jenkins/`: the `Jenkinsfile` is the other file in another directory that invokes helm
    (it only renders today, and "today" is what a control is for), and the `Makefile` runs the two
    validation renders.
    """
    scripts = [
        *sorted((DEPLOY / "jenkins").rglob("*.sh")),
        DEPLOY.parent / "Jenkinsfile",
        DEPLOY.parent / "Makefile",
    ]
    assert all(script.is_file() for script in scripts), "a scanned delivery file has moved"
    offenders = [
        f"{script.relative_to(DEPLOY.parent)}:{number}"
        for script in scripts
        for number, line in enumerate(script.read_text().splitlines(), start=1)
        if "--atomic" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"a delivery script runs helm with --atomic, which the chart forbids: {offenders}"
    )


_OPENSHIFT_SH = DEPLOY / "jenkins" / "targets" / "openshift.sh"

# Each case is (values-file body, does it state the egress posture, does it state the retention
# posture). A *mentioned* key is not a stated posture, which is what the shipped greps could not
# tell: `^\s*(windows|unboundedGrowthAccepted):` matched an operator writing down what they did not
# want, suppressed the `--set`, and left the deploy to die inside `templates/config.yaml` with a
# Go-template `fail` instead of the script's own sentence.
_POSTURE_CASES: dict[str, tuple[str, bool, bool]] = {
    "declined": (
        "networkPolicy:\n  allowAnyDestination: false\nretention:\n"
        "  unboundedGrowthAccepted: false\n",
        False,
        False,
    ),
    "accepted": (
        "networkPolicy:\n  allowAnyDestination: true\nretention:\n"
        "  unboundedGrowthAccepted: true\n",
        True,
        True,
    ),
    "listed": (
        "networkPolicy:\n  egressDestinations:\n    - ipBlock: {cidr: 10.0.0.0/8}\n"
        "retention:\n  windows:\n    CHEMCLAW_RETENTION_AUDIT_DAYS: 30\n",
        True,
        True,
    ),
    "empty-lists": (
        "networkPolicy:\n  egressDestinations: []\nretention:\n  windows:\n",
        False,
        False,
    ),
    "only-in-a-comment": (
        "# networkPolicy:\n#   allowAnyDestination: true\n# retention:\n"
        "#   unboundedGrowthAccepted: true\nservice: {}\n",
        False,
        False,
    ),
    "the-chart-defaults": ((CHART / "values.yaml").read_text(), False, False),
}


def _posture_verdict(helper: str, values_file: Path) -> bool:
    """Run one of `openshift.sh`'s posture helpers and report whether it read a stated posture.

    Sourced rather than executed, which is what the file's `main` guard is for: the helpers are the
    unit under test and a deploy is not. `set +e` afterwards because sourcing a `set -euo pipefail`
    script arms the calling shell too, and a helper *refusing* is one of the two answers.
    """
    probe = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_OPENSHIFT_SH}"; set +e; {helper} "{values_file}" >/dev/null 2>&1; echo $?',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return probe.stdout.strip() == "0"


@pytest.mark.parametrize("case", _POSTURE_CASES, ids=_POSTURE_CASES)
def test_the_deploy_script_reads_a_stated_posture_and_not_a_mentioned_key(
    case: str, tmp_path: Path
) -> None:
    """`unboundedGrowthAccepted: false` is a posture declined, and it was read as one stated.

    With neither opt-in environment variable set, a helper returns 0 only when the values file
    already states the posture — so `false` returning 0 is the defect: no `--set`, no message, and
    the operator gets the chart's `fail` instead of the sentence naming the two ways out. Driven
    against the real functions rather than asserted about their source, because the difference
    between "matches a key" and "reads a value" is only visible in an execution.

    The chart's own `values.yaml` is the last case and states neither, which is exactly why every
    caller of this chart passes both flags.
    """
    body, states_egress, states_retention = _POSTURE_CASES[case]
    values_file = tmp_path / "values.yaml"
    values_file.write_text(body)
    assert _posture_verdict("egress_flags", values_file) is states_egress
    assert _posture_verdict("retention_flags", values_file) is states_retention


def test_the_release_path_adopts_the_two_objects_the_previous_chart_left_unowned() -> None:
    """Every release installed before this chart is stuck at `helm upgrade` until two are adopted.

    On the previous chart `chemclaw-config` and the runtime ServiceAccount were `pre-install,
    pre-upgrade` hooks with `hook-delete-policy: before-hook-creation`, so they persist between
    releases — and Helm creates hook resources with a plain `Create`, no metadata visitor, so they
    carry no `meta.helm.sh/release-name`/`-namespace`. This chart claims the same two names as
    tracked resources, and the ownership check refuses to import them: measured on k3s v1.29.9,
    `helm upgrade` fails at prepare time with "exists and cannot be imported into the current
    release", and `--dry-run` fails identically — so nothing is half-applied, and `DRY_RUN=true` is
    this script's default.

    A grep over a script is the weakest shape of assertion in this file, and it is what is available
    here: the act is `oc annotate` against a live namespace, which no offline test performs. The
    behaviour itself was measured — dry run reports and changes nothing, the real run adopts both,
    a foreign ConfigMap in the same namespace is untouched, a second run is a no-op, and the upgrade
    that failed then succeeds. What this pins is that the release path still carries the step at
    all, and that the two documents an operator reads for the hand-run path still carry the
    annotation keys.
    """
    script = _OPENSHIFT_SH.read_text()
    assert "adopt_leftover_hook_objects" in script, (
        "the release path no longer adopts the objects the previous chart created as hooks, so "
        "every existing release is stuck at `helm upgrade` with no automated way out"
    )
    assert "meta.helm.sh/release-name" in script and "meta.helm.sh/release-namespace" in script
    # Only an object provably created by *this release's own* previous chart may be taken over —
    # adopting whatever happens to collide is a decision, not a mechanic.
    assert "app.kubernetes.io/instance=" in script and "helm.sh/hook" in script

    for document in (DEPLOY / "README.md", DEPLOY.parent / "docs" / "guides" / "runbook.md"):
        text = document.read_text()
        assert "meta.helm.sh/release-name" in text, (
            f"{document.name} does not carry the adoption step, so an operator upgrading by hand "
            "meets Helm's refusal with nothing saying whether it is safe to fix"
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

    The bash fallback values (the `:-N` defaults in the entrypoint) must match the Python `Settings`
    field defaults so that neither side can drift from the other. They currently agree; this test
    is a regression guard, not a discovery (verify by changing one side and watching it fail).
    """
    entrypoint = (DEPLOY / "entrypoint.sh").read_text()
    # Mapping: env var name → (flag name, Settings field name)
    checks = [
        ("CHEMCLAW_SERVICE_PORT", "--port", "service_port"),
        ("CHEMCLAW_SERVICE_MAX_CONNECTIONS", "--limit-concurrency", "service_max_connections"),
        ("CHEMCLAW_SERVICE_KEEPALIVE_SECONDS", "--timeout-keep-alive", "service_keepalive_seconds"),
        (
            "CHEMCLAW_SERVICE_MAX_HEADER_BYTES",
            "--h11-max-incomplete-event-size",
            "service_max_header_bytes",
        ),
    ]

    from chemclaw.core.config import settings

    for env_var, flag, field_name in checks:
        assert flag in entrypoint, f"uvicorn is launched without {flag}"
        assert env_var in entrypoint, f"{flag} is a literal rather than reading {env_var}"

        # Extract the bash fallback value: find `${ENV_VAR:-N}` and extract N
        pattern = rf"\$\{{{env_var}:-(\d+)\}}"
        match = re.search(pattern, entrypoint)
        assert match, f"{env_var} fallback not found in entrypoint.sh"
        bash_value = int(match.group(1))

        # Compare with the Python default
        python_value = getattr(settings, field_name)
        assert bash_value == python_value, (
            f"{env_var} bash fallback {bash_value} disagrees with "
            f"settings.{field_name} {python_value}"
        )

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
    assert "make deps-audit" in image_workflow, "no dependency scan in workflow"
    assert "syft" in image_workflow and "upload-artifact" in image_workflow, "no retained SBOM"
    assert "pip-audit" in (DEPLOY.parent / "Makefile").read_text(), (
        "deps-audit target must invoke pip-audit"
    )
    # The *image* scan is deliberately not asserted here, and the reason is in `BACKLOG.md`: it
    # ran, it found three real classes of problem now fixed in `deploy/Containerfile`, and it then
    # reported two packages the build's own exhaustive filesystem listing says are not present.
    # Shipping a gate whose last word contradicts the artifact it scanned would make every future
    # red build ambiguous, so it goes back on with its own change rather than riding along here.


def test_the_dependency_audit_gates_every_branch_push_and_the_local_gate() -> None:
    """A gate only `image.yml` runs is not a gate on the thing developers do all day.

    The test above proves the audit exists and is blocking; it proved that of the *image* workflow,
    which triggers on `push: main` and `pull_request` only. So every branch push went green against
    the lockfile, and so did `make ci` — the target CLAUDE.md calls "the full pre-push gate" and
    whose contract is "a green `make` locally means a green CI". Measured on the tree that found
    this, that lockfile carried two known CVEs in `pypdf`. Both wirings are asserted here because
    each is one word in a list, which is exactly the kind of thing a later edit drops silently.
    """
    ci_workflow = (DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text()
    assert "make deps-audit" in ci_workflow, (
        "ci.yml does not run the dependency audit, so a branch push audits nothing"
    )
    assert 'branches: ["**"]' in ci_workflow, (
        "ci.yml no longer runs on every branch, which is what made putting the audit here worth it"
    )
    ci_target = next(
        line
        for line in (DEPLOY.parent / "Makefile").read_text().splitlines()
        if line.startswith("ci:")
    )
    assert "deps-audit" in ci_target, f"`make ci` does not depend on deps-audit: {ci_target}"


def test_every_gate_make_ci_runs_is_a_step_ci_yml_runs() -> None:
    """Two hand-maintained lists whose whole contract is that they agree, and nothing checked it.

    CLAUDE.md's claim is "CI runs exactly these, so a green `make` locally means a green CI". That
    is one word in a `Makefile` prerequisite list and one step in a workflow, kept in step by
    memory — which is exactly how `deps-audit` came to be in neither. That instance was fixed and
    pinned; the *mechanism* was not, so the next gate to be added to one list and forgotten in the
    other fails nothing. This closes the class instead of the instance.

    `helm-validate` is the one gate deliberately in a job of its own: it needs `helm` and
    `kubeconform` and no Python, so it runs in `chart` in parallel rather than lengthening `check`.
    The split is asserted rather than tolerated — a gate quietly moving between jobs is a change to
    what blocks a merge.

    **Both directions, and the second one is why this test was rewritten.** It used to slice the
    file in two on a literal newline-plus-`  chart:` and call the halves `check_job` and
    `chart_job`, which stopped being true the moment a third job (`static`, holding lint and
    type) was added:
    the first "half" then silently contained two jobs. It still passed, because a union of two
    buckets does not care how the text was cut — a test that survives the change it should have
    noticed. Parsing the jobs is what makes the claim checkable. And a step in `ci.yml` that
    `make ci` does *not* run breaks the same contract from the other side: CLAUDE.md promises "a
    green `make` locally means a green CI", so CI running one extra gate makes the local gate a
    weaker answer than it says it is.
    """
    ci_target = next(
        line
        for line in (DEPLOY.parent / "Makefile").read_text().splitlines()
        if line.startswith("ci:")
    )
    gates = ci_target.split(":", 1)[1].split("##")[0].split()
    assert len(gates) > 10, f"the `make ci` prerequisite list did not parse: {gates}"

    workflow = (DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text()
    jobs: dict[str, Any] = yaml.safe_load(workflow)["jobs"]

    def targets(job: str) -> set[str]:
        """Every make target a job's steps invoke, `make lint type` counting as two."""
        return {
            target
            for step in jobs[job].get("steps", [])
            for command in re.findall(r"^make (.+)", str(step.get("run", "")), re.MULTILINE)
            for target in command.split()
        }

    assert "helm-validate" in targets("chart"), "the chart gate left the job that has helm"

    everywhere: set[str] = set()
    for job in jobs:
        everywhere |= targets(job)

    missing = [gate for gate in gates if gate not in everywhere]
    assert not missing, (
        f"`make ci` runs {missing} and no step in ci.yml does, so a green local gate is not a "
        "green CI — the exact drift that let deps-audit sit in neither list"
    )
    # `db-migrate` is the one target in the workflow that is not a gate: it builds the database the
    # Postgres-backed tests then run against, which is why `make ci` does not depend on it (a
    # developer's database already exists; a fresh runner's does not). Named rather than pattern-
    # matched, so a *second* non-gate step has to be argued for here instead of slipping in.
    setup = {"db-migrate"}
    extra = [target for target in everywhere if target not in set(gates) | setup]
    assert not extra, (
        f"ci.yml runs {extra} and `make ci` does not, so CI gates on something the documented "
        "pre-push gate never checks — the same drift running the other way"
    )


def test_the_default_branch_is_never_cancelled_mid_gate() -> None:
    """A cancelled run on `main` is not a superseded answer, it is a missing one.

    Both workflows key their concurrency group on the branch and cancel the loser, which is right
    for a topic branch: the newer push has the answer the older one was computing. On the default
    branch the two runs are about *different commits*, and nothing ever re-runs the cancelled one.
    Measured over the 30 `ci` runs to 2026-08-26, three commits that are ancestors of `origin/main`
    today have no completed run of that workflow at all — `548266233b`, cancelled 9.4 minutes in,
    plus `9dfb02a5f6` and `3937fe568d`. The gate's entire claim is that what is on `main` passed
    it.

    Asserted as an expression rather than by name so the fix cannot be reverted to a bare `true`
    while the comment above it still explains why it must not be.
    """
    for name in ("ci.yml", "image.yml"):
        workflow = (DEPLOY.parent / ".github" / "workflows" / name).read_text()
        document: Any = yaml.safe_load(workflow)
        cancel = document["concurrency"]["cancel-in-progress"]
        assert cancel is not True, (
            f"{name} cancels in-progress runs unconditionally, so two merges landing inside one "
            "run's duration leave the earlier commit on the default branch with no gate"
        )
        assert "main" in str(cancel), (
            f"{name}'s cancel-in-progress no longer exempts the default branch: {cancel!r}"
        )


def test_every_action_is_pinned_to_a_commit_not_a_tag() -> None:
    """The pipeline pins its Python closure with `--locked` and audits it; its actions floated.

    `actions/checkout@v4` is a *mutable* reference — the tag is repointed on every v4 release, and
    a compromised or retagged action runs with this workflow's token in the job that builds the
    shipped image. That is the same threat class `deps-audit` and the SBOM exist to cover, left
    open in the one place the repository executes third-party code on every push.

    The readable version is kept as a trailing `# vX.Y.Z` comment, which is also the form
    Dependabot rewrites — `.github/dependabot.yml` has a `github-actions` entry precisely because a
    pin with no updater trades a supply-chain risk for a staleness one.
    """
    unpinned: list[str] = []
    for workflow in sorted((DEPLOY.parent / ".github" / "workflows").glob("*.yml")):
        for line in workflow.read_text().splitlines():
            match = re.search(r"uses:\s*(\S+)", line)
            if match is None or match.group(1).startswith("./"):
                continue
            reference = match.group(1)
            if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference):
                unpinned.append(f"{workflow.name}: {reference}")
            elif "#" not in line:
                unpinned.append(f"{workflow.name}: {reference} (pinned, but no `# vX.Y.Z` comment)")
    assert not unpinned, f"actions referenced by a mutable tag: {unpinned}"


def test_the_mutation_run_is_scheduled_and_has_a_database_to_run_against() -> None:
    """The two properties of `mutants.yml` whose loss is silent, and one of them manufactures a lie.

    The schedule is the whole point: `make mutants` sat in the `Makefile` for months with nothing
    running it, so the seven invariant-bearing modules had a mutation control that had executed once
    (`D-2026-08-27-a-survivor-is-not-a-failing-build`).

    The Postgres service is the subtler half. Six of the eighteen files in
    `pytest_add_cli_args_test_selection` gate on `tests/pg.py::migrated_db_or_skip`, so without a
    database they skip and still report green — and every mutant in `science/calc/store.py` and
    `agent/audit_store.py` is then scored SURVIVED for a reason that has nothing to do with the
    mutation. Dropping the service would not break the job; it would make it report invented
    survivors in two of the seven modules it exists for, which is worse than not running it.
    """
    document: Any = yaml.safe_load(
        (DEPLOY.parent / ".github" / "workflows" / "mutants.yml").read_text()
    )
    # `on` is YAML 1.1's boolean `True` once parsed, which is why this reads oddly.
    triggers = document[True]
    assert triggers.get("schedule"), "mutants.yml has no schedule; it is a target nobody runs again"

    job = document["jobs"]["mutants"]
    assert "postgres" in job.get("services", {}), (
        "the mutation job has no Postgres service, so the six database-backed test files in "
        "`pytest_add_cli_args_test_selection` skip and their mutants are scored as survivors"
    )
    assert "CHEMCLAW_POSTGRES_DSN" in job.get("env", {}), (
        "the mutation job provisions Postgres and does not point the suite at it"
    )
    assert any("db-migrate" in step.get("run", "") for step in job["steps"]), (
        "the mutation job never migrates the database it provisions"
    )


def test_every_downloaded_binary_is_checksummed_before_it_runs() -> None:
    """A release asset is mutable in a way a git tag is not, and both of these execute as root.

    `kubeconform` validates the chart that describes the deployment; `syft`'s installer runs on the
    runner that just built the shipped image. Neither was verified — the kubeconform tarball was
    taken on trust, and the syft installer was piped straight into `sh`, which additionally runs a
    truncated download because a half-transferred script is a valid prefix.

    The check is that a `curl` of one of these is accompanied by a `sha256sum -c`, not that any
    particular digest is correct — a digest goes stale by design when the pinned version moves, and
    the failure this guards against is somebody adding a third download with no verification at
    all.
    """
    for name, marker in (("ci.yml", "kubeconform"), ("image.yml", "syft")):
        workflow = (DEPLOY.parent / ".github" / "workflows" / name).read_text()
        step = next(
            block
            for block in workflow.split("      - name:")
            if marker in block and "curl" in block
        )
        assert "sha256sum -c" in step, (
            f"{name} downloads {marker} and runs it without verifying a digest"
        )
    image_workflow = (DEPLOY.parent / ".github" / "workflows" / "image.yml").read_text()
    assert "install.sh | sh" not in image_workflow and "| sh -s" not in image_workflow, (
        "image.yml pipes a downloaded installer straight into a shell"
    )


def _run_deps_audit(
    tmp_path: Path, stdout: str, exit_code: int, *, ci: str | None, stale_log: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `make deps-audit` against a stubbed `uvx pip-audit`, with `CI` set or unset.

    The tool is stubbed rather than the network blocked because the two events under test — a
    found vulnerability and an unreachable advisory database — are distinguished by `pip-audit`'s
    *output*, and only one of them can be produced by unplugging a cable. `pip-audit` also caches
    its responses, so an offline run after an online one legitimately succeeds; a stub is the only
    way to ask the question deterministically.

    `stale_log` sets up the one situation these stubs used to be blind to: a `tee` that cannot
    write, with bytes already sitting at the log path it was meant to overwrite. It stubs `tee`
    itself as a command that writes nothing and fails, seeds that text at the historical
    `AUDIT_LOG` path, and passes that path on the command line — everything the deleted
    file-reading recipe needed to classify the wrong bytes. Nothing under test may consult it.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "uv").write_text("#!/bin/sh\necho '# stub export'\n")
    (stub_dir / "uvx").write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {exit_code}\n")
    stubs = ["uv", "uvx"]
    overrides = []
    if stale_log is not None:
        log = tmp_path / "audit.log"
        log.write_text(stale_log)
        (stub_dir / "tee").write_text("#!/bin/sh\ncat > /dev/null\nexit 1\n")
        stubs.append("tee")
        overrides.append(f"AUDIT_LOG={log}")
    for stub in stubs:
        (stub_dir / stub).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "CI"}
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    if ci is not None:
        env["CI"] = ci
    return subprocess.run(
        ["make", "deps-audit", *overrides],
        cwd=DEPLOY.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


_UNREACHABLE = (
    "requests.exceptions.ConnectionError: HTTPSConnectionPool(host='pypi.org', port=443): "
    "Max retries exceeded"
)
_FOUND = "Found 2 known vulnerabilities in 1 package\nName  Version ID\npypdf 6.14.2   GHSA-xxxx"


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_an_unreachable_advisory_database_does_not_fail_a_developers_offline_gate(
    tmp_path: Path,
) -> None:
    """`make ci` runs `deps-audit`, and a laptop with no network must still get a usable gate.

    `pip-audit` exits 1 both when it finds a vulnerability and when it cannot reach the advisory
    database, so the exit status alone cannot separate them. Measured under `unshare -rn` before
    this classification existed: `uvx` failing to fetch the tool gave make error 2, and `pip-audit`
    dying inside `requests` gave make error 1 — the same 1 a real finding gives. The row this
    lane's commit deleted from `BACKLOG.md` said this cost was unpriced; this is the price.
    """
    result = _run_deps_audit(tmp_path, _UNREACHABLE, 1, ci=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED" in result.stdout, result.stdout
    assert "NOT audited" in result.stdout, "an unaudited lockfile must say so, loudly"


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_an_unreachable_advisory_database_fails_in_ci(tmp_path: Path) -> None:
    """The other half, and the half that makes the tolerance safe.

    In CI the network is a given, so "unreachable" is a real failure — tolerating it there would
    be a supply-chain hole that reads as a green build forever, which is the exact shape
    `deps-audit` was wired into `ci.yml` to close.
    """
    result = _run_deps_audit(tmp_path, _UNREACHABLE, 1, ci="true")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "cannot be skipped" in result.stdout, result.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_a_real_finding_fails_even_offline(tmp_path: Path) -> None:
    """A vulnerability is never excused, and never mistaken for an outage.

    Checked before the unreachable patterns precisely so an advisory whose text mentions a
    connection failure cannot buy an exemption — the failure mode of classifying output.
    """
    noisy = f"{_FOUND}\n{_UNREACHABLE}"
    result = _run_deps_audit(tmp_path, noisy, 1, ci=None)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "SKIPPED" not in result.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_the_audit_classifies_what_the_command_said_not_what_a_file_holds(
    tmp_path: Path,
) -> None:
    """The hole the three tests above could not see: the classified bytes came from a *file*.

    The recipe piped `pip-audit` into `tee $(AUDIT_LOG)`, took the status from `PIPESTATUS[0]` and
    then grepped the log. `tee`'s own failure is `PIPESTATUS[1]` and was never examined, so a `tee`
    that could not write left the greps reading whatever already sat at that fixed, world-writable
    path. Measured against the deleted recipe with a real (not stubbed) `tee` failure — an EROFS
    mount — a stale log holding a connection error, and `pip-audit` reporting two vulnerabilities:

        deps-audit: SKIPPED - the advisory database is unreachable and CI is unset.
        make exit=0

    A vulnerable lockfile, reported as an outage, on the target that exists to prevent exactly
    that. The three tests above all passed throughout, because none of them ever made `tee` fail.

    Both halves are asserted. The behavioural half hands the run every ingredient that used to
    flip it; the structural half is what keeps this from passing vacuously if the file ever comes
    back under another variable's name — the classification's input has to be the captured output.
    """
    result = _run_deps_audit(tmp_path, _FOUND, 1, ci=None, stale_log=_UNREACHABLE)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "SKIPPED" not in result.stdout, result.stdout
    assert "Found 2 known vulnerabilities" in result.stdout, (
        "the operator never even saw the finding"
    )
    recipe = (DEPLOY.parent / "Makefile").read_text().split("deps-audit:")[1].split("\nexplain:")[0]
    commands = [line for line in recipe.splitlines() if not line.lstrip().startswith(("@#", "#"))]
    assert not any("tee" in line for line in commands), (
        "deps-audit pipes into tee again, so its classification reads a file rather than the "
        f"command's output: {commands}"
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_a_clean_audit_passes(tmp_path: Path) -> None:
    """The control: exit 0 is exit 0, and the classification never sees it."""
    result = _run_deps_audit(tmp_path, "No known vulnerabilities found", 0, ci="true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED" not in result.stdout


def test_no_calculation_binary_ships_in_this_image() -> None:
    """The licence decision this repository no longer has to take, and why it stopped needing to.

    `xtb` (LGPL-3.0) and `crest` (GPL-3.0) were installed here for `calc.xtb_cli` and
    `calc.crest_cli`, and `--build-arg INCLUDE_CREST=false` existed so that declining to
    *distribute* the GPL binary was a flag rather than a patch (D-2026-08-01-a-tag-is-a-pointer).
    Both callers moved to `Chemclaw3-mcp` with the physics, so the layer was building ~200 MB into
    every image — front door, every worker, every connector pod — for no caller, and taking a
    redistribution decision on behalf of a product that no longer runs the programs.

    Asserted as an *absence*, so re-adding a binary here has to argue for itself: whatever needs
    one belongs in the repository whose code invokes it.
    """
    containerfile = (DEPLOY / "Containerfile").read_text()
    # The declarations and the download URLs, not the words: the comment above the removal explains
    # what left and why, and a test that forbade naming it would forbid the explanation.
    for marker in ("ARG INCLUDE_CREST", "grimme-lab/xtb/releases", "crest-lab/crest/releases"):
        assert marker not in containerfile, (
            f"{marker!r} is back in the image; no module in src/ invokes a calculation binary, and "
            "shipping one takes a redistribution decision this repository does not need to take"
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


def _makefile_renders() -> list[list[str]]:
    """Every `helm template` of this chart in the Makefile, as its whole (continued) command.

    A render is a backslash-continued block, so "does this one pass the flag" is a question about
    the block and not about the line the command starts on. Returned as a list of lines per render
    so a caller can ask what each one carries.

    Replaces a pair of `len(...) == 2` assertions. The count was the *point* of those tests — every
    render must pay the escape hatch — and pinning it as a literal meant that adding a third render
    failed them for the one reason that is not a defect. The invariant is "each", not "two".
    """
    lines = (DEPLOY.parent / "Makefile").read_text().splitlines()
    renders: list[list[str]] = []
    for index, line in enumerate(lines):
        if "helm template chemclaw" not in line or line.lstrip().startswith("@#"):
            continue
        block = [line]
        cursor = index
        while lines[cursor].rstrip().endswith("\\"):
            cursor += 1
            block.append(lines[cursor])
        renders.append(block)
    return renders


def test_an_unstated_egress_posture_refuses_to_render() -> None:
    """Available and visible was not enough: the chart must not render a posture nobody chose.

    A declarable knob left empty is still `to: []` in the cluster — every destination on five
    ports — behind an object an operator reads as "egress is restricted". The comment saying so
    lived in `values.yaml`, which nobody re-reads after the first install. So the render now fails
    unless exactly one of the two is stated: a destination list, or `allowAnyDestination: true`.

    Asserted on the template text and the shipped values rather than by rendering, because `helm`
    is a live-edge dependency this suite does not have — the same reason every other check here
    parses. What that cannot see is the *logic* of the condition, so it is written to be readable
    as one line: `empty` on both sides, failing when the two agree.

    The escape hatch defaults to `false`, which means `helm template` on these defaults needs one
    `--set`. That is the cost of the guard and it is paid in the three places that render.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    guard = (
        "eq (empty .Values.networkPolicy.egressDestinations)"
        " (empty .Values.networkPolicy.allowAnyDestination)"
    )
    assert guard in policy, "the egress posture can be left unstated"
    assert "{{- fail " in policy, "the guard warns rather than refusing"
    # A quoted boolean is the failure the emptiness check alone could not see: Go templates treat a
    # non-empty string as truthy and `empty` treats it as non-empty, so `--set-string
    # allowAnyDestination=false` rendered the allow-any policy while reading as off. The type guard
    # refuses a string outright so the emptiness logic only ever sees a real bool.
    assert 'kindIs "string" .Values.networkPolicy.allowAnyDestination' in policy, (
        "a quoted allowAnyDestination (--set-string) would render allow-any while reading as off"
    )
    assert _values()["networkPolicy"]["allowAnyDestination"] is False, (
        "the shipped default grants a permission the release never wrote down"
    )
    # Every render of the shipped defaults must carry the escape hatch, or it cannot render at all.
    renders = _makefile_renders()
    assert renders, "no `helm template` found in the Makefile — the extraction is broken"
    unflagged = [
        block[0].strip()
        for block in renders
        if not any("--set networkPolicy.allowAnyDestination=true" in line for line in block)
    ]
    assert not unflagged, (
        f"a shipped-defaults render is missing the flag it cannot render without: {unflagged}"
    )


def test_an_unstated_retention_posture_refuses_to_render() -> None:
    """The sibling of the egress guard, for the same reason: a knob nobody set is a wrong default.

    Every `CHEMCLAW_RETENTION_*` window in `Settings` defaults to `0` (disabled) — a deliberate
    policy stated in `core/config/memory.py`, not a code default this chart should silently ship —
    so a release that never states its retention posture would run with every durable table
    growing forever under a `values.yaml` comment nobody re-reads. The render now fails unless
    exactly one of `retention.windows` or `retention.unboundedGrowthAccepted: true` is stated.

    Asserted on the template text, exactly like the egress guard above and for the same reason:
    `helm` is a live-edge dependency this suite does not have.
    """
    config = (CHART / "templates" / "config.yaml").read_text()
    guard = "eq (empty .Values.retention.windows) (empty .Values.retention.unboundedGrowthAccepted)"
    assert guard in config, "the retention posture can be left unstated"
    assert "{{- fail " in config, "the guard warns rather than refusing"
    assert _values()["retention"]["windows"] == {}, (
        "the shipped default states a retention policy the release never wrote down"
    )
    assert _values()["retention"]["unboundedGrowthAccepted"] is False, (
        "the shipped default grants a permission the release never wrote down"
    )
    # Every render of the shipped defaults must carry the escape hatch, or it cannot render at all
    # — the same renders the egress test walks, now each paying both flags.
    unflagged = [
        block[0].strip()
        for block in _makefile_renders()
        if not any("--set retention.unboundedGrowthAccepted=true" in line for line in block)
    ]
    assert not unflagged, (
        f"a shipped-defaults render is missing the flag it cannot render without: {unflagged}"
    )


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


def _alert_expressions() -> str:
    """Every rule's PromQL, and nothing else.

    The annotations legitimately name metrics in prose — `ChemclawDurableJobsFailing`'s description
    tells an operator to break the ratio down with three other series — so any check that reads the
    file as text will call a metric "alerted" because a sentence mentioned it. That is the exact
    shape of false coverage these tests exist to prevent, so the expressions are extracted first.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    return " ".join(re.findall(r"expr:\s*(?:>-\s*)?((?:.|\n)*?)\n\s*for:", rule))


def _series_referenced(text: str) -> set[str]:
    """Metric names in some PromQL, with Prometheus's derived histogram suffixes folded away."""
    return {
        re.sub(r"_(bucket|sum|count)$", "", name)
        for name in re.findall(r"\b(chemclaw_[a-z_]+)\b", text)
    }


def _dashboard_expressions() -> str:
    """Every panel query the chart's dashboards carry."""
    import json

    return " ".join(
        target["expr"]
        for path in sorted((CHART / "dashboards").glob("*.json"))
        for panel in json.loads(path.read_text())["panels"]
        for target in panel["targets"]
    )


# Counters ending `_failures_total` or `_dropped_total` that deliberately have no alert, each with
# the reason it is not one. The set is small on purpose: it is what stops the rule below from being
# satisfied by adding a name to a list.
#
# Both entries share one property — the increment is *caused by the caller* and its steady-state
# rate is not zero. A rule on either would page on an expired token or a malformed request body,
# which is the failure mode that trains people to ignore an alert channel. They are dashboard
# series (`Chemclaw front door` -> "Refused before the handler"), and a rise in either is a security
# or client question rather than a system one.
_COUNTERS_WITH_NO_ALERT: dict[str, str] = {
    "chemclaw_auth_failures_total": (
        "a rejected credential is a caller's mistake with a non-zero steady state; alerting on the "
        "first would page on every expired token"
    ),
    "chemclaw_request_validation_failures_total": (
        "a 422 is the caller's malformed request, not this system failing; the route breakdown "
        "lives on the front-door dashboard"
    ),
}


def test_every_counter_that_can_fail_silently_has_an_alert() -> None:
    """The coverage rule, stated so that a counter added tomorrow is covered by it.

    This test used to name eight metrics and check they appeared somewhere in the rule file. That
    direction is worth keeping (it is the test below, which catches a rename that leaves its alert
    behind) but it proves nothing about *coverage*: a ninth counter shipped with no rule passed it,
    and several did — `chemclaw_pushback_dropped_total`,
    `chemclaw_fan_out_children_dropped_total`, `chemclaw_result_publish_failures_total` and
    `chemclaw_result_projection_failures_total` were all unalerted while the exactly-analogous
    `chemclaw_notes_publish_failures_total` was alerted, which is one failure class with two
    different answers.

    So it is inverted: the *registry* is the list, and every counter whose name says it counts a
    silent failure must either appear in an alert expression or be exempted here with a reason.
    `_failures_total` and `_dropped_total` are the two suffixes this codebase uses for "something
    was swallowed", which is what makes the selection mechanical rather than a judgement call.
    """
    from chemclaw.core.metrics import _COUNTERS

    alerted = _series_referenced(_alert_expressions())
    silent = {name for name in _COUNTERS if name.endswith(("_failures_total", "_dropped_total"))}
    assert silent, "no silent-failure counters found — the suffix convention moved, not the alerts"
    uncovered = sorted(silent - alerted - set(_COUNTERS_WITH_NO_ALERT))
    assert not uncovered, (
        f"counters that record a swallowed failure and fire nothing: {uncovered}. Add a rule to "
        "templates/prometheusrule.yaml, or an entry to _COUNTERS_WITH_NO_ALERT saying why the "
        "steady-state rate is not zero."
    )
    # The exemptions must stay earned in both directions: one for a counter that no longer exists is
    # stale bookkeeping, and one for a counter that has since been alerted is a note nobody reads.
    stale = sorted(set(_COUNTERS_WITH_NO_ALERT) - silent)
    assert not stale, f"exemptions for counters that are gone or renamed: {stale}"
    redundant = sorted(set(_COUNTERS_WITH_NO_ALERT) & alerted)
    assert not redundant, f"exempted counters that do have an alert: {redundant}"


def _degraded_sites_with_their_own_counter() -> dict[str, set[str]]:
    """Subsystem name -> the counters incremented within a few lines of its `degraded()` call.

    `metrics_bridge.degraded` increments `chemclaw_degraded_total{subsystem=...}` on every call, so
    a site that *also* increments a counter of its own puts one event on two series. If both series
    are alerted, one failure raises two alerts — which is the duplication this reads for. Derived
    from the source in the same spirit as `tests/test_degraded.py`, which reads the subsystem
    literals out of the tree rather than keeping a list beside them.
    """
    sites: dict[str, set[str]] = {}
    for path in sorted(Path("src").rglob("*.py")):
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if "def degraded(" in line or not re.search(r"(?<![\w.])degraded\(|\.degraded\(", line):
                continue
            window = "\n".join(lines[max(0, index - 12) : index + 14])
            subsystem = re.search(r'degraded\(\s*\n?\s*[\w.]+,\s*\n?\s*"([a-z_]+)"', window)
            if subsystem is None:
                continue
            counters = set(re.findall(r'increment\(\s*\n?\s*"(chemclaw_[a-z0-9_]+)"', window))
            sites.setdefault(subsystem.group(1), set()).update(counters)
    return sites


def test_no_two_alerts_fire_on_one_event() -> None:
    """One failed retrieval leg raised two alerts, and the umbrella's was the less useful one.

    `retrieval/fanout.py`'s failure arm calls `degraded(logger, "evidence_source", ...)` and
    increments `chemclaw_evidence_source_failures_total` on the same exception. Both series were
    alerted, both at `warning`, both over a 15-minute window and both at `for: 0m` — so a single
    broken leg produced `ChemclawSubsystemDegraded{subsystem="evidence_source"}` *and*
    `ChemclawEvidenceSourceFailing{source=...}`, of which only the second says which leg.

    The fix is an exclusion in the umbrella's selector, and this is what stops that exclusion from
    becoming a hand-maintained list: the set is derived from the `degraded()` call sites, so a
    second site that grows its own alerted counter fails here until it is either excluded from the
    umbrella or left with one rule.
    """
    alerted = _series_referenced(_alert_expressions())
    duplicated = {
        subsystem
        for subsystem, counters in _degraded_sites_with_their_own_counter().items()
        if counters & alerted
    }
    umbrella = re.search(r"chemclaw_degraded_total\{([^}]*)\}", _alert_expressions()) or re.search(
        r"(chemclaw_degraded_total)", _alert_expressions()
    )
    assert umbrella is not None, "the degradation umbrella no longer reads chemclaw_degraded_total"
    excluded = set(re.findall(r'subsystem!="([a-z_]+)"', umbrella.group(0)))
    assert duplicated == excluded, (
        f"subsystems whose own counter is alerted: {sorted(duplicated)}; excluded from the "
        f"umbrella: {sorted(excluded)}. Each side of that difference is a duplicate alert or a "
        "gap — a subsystem excluded here with no rule of its own is not alerted at all."
    )


def test_severity_is_monotonic_in_how_final_the_loss_is() -> None:
    """The retryable half outranked the terminal half, which is the wrong way round.

    `publish/outbox.py` spends one attempt and leaves the row `pending`, so
    `chemclaw_result_publish_failures_total` is "we are still trying" —
    `chemclaw_results_dead_lettered_total` is that same publication after its retries are gone, and
    nothing will attempt it again. The first carried `critical` and the second `warning`, so the
    channel that meant "we have stopped" was the quieter one. A projection failure is terminal by
    construction (the rule's own description says retrying will not help) and belongs with it.

    Pinned as an ordering between named alerts rather than as three literals, because the claim is
    the ordering: a later edit that lowers a terminal alert or raises the retryable one fails here.
    """
    severity = dict(
        re.findall(
            r"- alert: (\w+)(?:.|\n)*?severity: (\w+)",
            (CHART / "templates" / "prometheusrule.yaml").read_text(),
        )
    )
    rank = {"warning": 1, "critical": 2}
    retryable = "ChemclawResultPublishFailing"
    for terminal in ("ChemclawResultsDeadLettered", "ChemclawResultProjectionFailing"):
        assert rank[severity[terminal]] > rank[severity[retryable]], (
            f"{terminal} is a permanent loss of a computed result and {retryable} is an attempt "
            f"that will be retried; {terminal} may not be the quieter of the two "
            f"({severity[terminal]} against {severity[retryable]})"
        )


def test_every_ratio_alert_has_a_traffic_floor() -> None:
    """`rate(errors) / rate(total)` is 100% on a single error in an otherwise idle window.

    Both ratio alerts shipped with `clamp_min(denominator, 0.001)`, which is a division-by-zero
    guard and reads as a floor. It is not one — it makes the empty window *worse*, turning "no
    sample" into a large finite ratio. Measured with `promtool test rules` on the shipped
    expressions: one failed turn and one started turn in a ten-minute window evaluated to `1.0`
    against a 0.1 threshold, and one failed durable job in an idle half hour to `0.56` against
    0.2. Both now require their own denominator to clear an absolute rate first.

    Derived rather than listed: any expression that divides one `rate()` by another is a ratio, so
    a third one added tomorrow is covered on the day it is added.
    """
    rules = re.split(r"\n\s*- alert: ", (CHART / "templates" / "prometheusrule.yaml").read_text())
    ratios = []
    for block in rules[1:]:
        name = block.splitlines()[0].strip()
        expr = " ".join(block.split("expr:")[1].split("for:")[0].split())
        if re.search(r"rate\([^)]*\)\)?\s*/\s*", expr):
            ratios.append((name, expr))
    assert ratios, "no ratio alerts found — the extraction is broken, not the rules"
    for name, expr in ratios:
        assert "clamp_min" not in expr, (
            f"{name} still guards its denominator with clamp_min, which converts an idle window "
            "into a large finite ratio instead of no sample"
        )
        assert re.search(r"\band\s+sum\(rate\(", expr), (
            f"{name} divides two rates with no absolute floor on the denominator, so one event in "
            "an idle window is a 100% failure rate"
        )


def test_every_declared_metric_has_a_consumer() -> None:
    """A metric with no panel and no rule is a number nobody has ever seen.

    This is the failure the ServiceMonitor fixed one level down, one level up. Before the dashboards
    existed, sixteen of the registry's series had an alert and the other eighty-eight had no reader
    of *any* kind — computed on a hot path, exposed, scraped, retained, and read by nobody.
    `deploy/README.md` said so outright.

    Asserted against the registry rather than against a list here, so the obligation lands on
    whoever declares the metric: a series added tomorrow with no panel and no rule fails here, which
    is the only moment anyone is in a position to say what question it answers.
    """
    from chemclaw.core.metrics import _COUNTERS, _GAUGE_FAMILIES, _GAUGES, _HISTOGRAMS

    declared = {*_COUNTERS, *_GAUGES, *_HISTOGRAMS, *_GAUGE_FAMILIES}
    consumed = _series_referenced(_alert_expressions() + " " + _dashboard_expressions())
    orphans = sorted(declared - consumed)
    assert not orphans, (
        f"declared metrics with no alert and no dashboard panel: {orphans}. Put each on a panel in "
        "deploy/helm/chemclaw/dashboards/ or give it a rule; a series nobody reads is a cost with "
        "no benefit."
    )
    # And the other direction, which is how a dashboard rots: a panel querying a series the app
    # stopped emitting renders as an empty graph, which looks exactly like "nothing happened".
    unknown = sorted(_series_referenced(_dashboard_expressions()) - declared)
    assert not unknown, f"dashboard panels query series the app never emits: {unknown}"


def test_no_dashboard_panel_queries_a_series_nothing_produces() -> None:
    """A panel over a series no producer emits is a graph that can never draw.

    Two shipped this way and they failed differently, which is why this reads *every* identifier
    rather than only the `chemclaw_`-prefixed ones. "Temporal worker task slots" and "Temporal
    pollers" queried `temporal_worker_task_slots_*` and `temporal_num_pollers` — the Temporal SDK's
    own exporter, which `monitoring.temporalSdkMetrics` ships **off** and which
    `durable/serve.py` does not bind at all, so nothing anywhere emits those series. The rule that
    reads them (`ChemclawWorkerNotPolling`) is rendered only under that flag; the panels were
    unconditional, which is the whole defect. They are deleted rather than flag-gated, by this
    repository's own "no 'for later' stubs" rule — the change that adds the bind is the change that
    adds the panels back, and `values.yaml` says so where the flag lives.

    "Connector reachability" failed the other way: `chemclaw_connector_unhealthy` *is* declared, so
    the existing declared-vs-queried check passed it, and the gauge family was bound by nothing —
    caught by `tests/test_service.py::test_the_per_connector_health_gauge_actually_renders_a_series`
    instead, which reads the exposition.

    `up` and `absent()` are Prometheus's own and stay allowed; nothing else foreign is.
    """
    from chemclaw.core.metrics import declared_histogram_names, declared_metric_names

    declared = set(declared_metric_names())
    queryable = declared | {
        f"{name}{suffix}"
        for name in declared_histogram_names()
        for suffix in ("_bucket", "_sum", "_count")
    }
    # Reduced to just the metric references: label selectors, grouping clauses, quoted strings,
    # durations and function names are all PromQL grammar rather than series, and a check that read
    # them would report every `by (source)` as a missing metric.
    expressions = _dashboard_expressions()
    expressions = re.sub(r"\{[^}]*\}", " ", expressions)
    grouping = r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)"
    expressions = re.sub(grouping, " ", expressions)
    expressions = re.sub(r"\[[^\]]*\]", " ", expressions)
    expressions = re.sub(r"\b[a-z_]+\s*\(", " ", expressions)
    identifiers = set(re.findall(r"\b([a-z][a-z0-9_]*)\b", expressions))
    foreign = sorted(identifiers - queryable - {"up"})
    assert not foreign, (
        f"dashboard panels query series nothing in this system emits: {foreign}. Wire the producer "
        "or delete the panel; a panel that cannot draw reads as 'nothing happened'."
    )


def test_the_metrics_that_were_designed_to_alert_actually_alert() -> None:
    """Collected-and-un-alerted is the same failure REV-2 fixed one level down.

    The ServiceMonitor made the metrics visible; not one of them fired anything, because no
    PrometheusRule existed anywhere in the repo. The clearest case is the audit-sink failure
    counter, whose emitter logs a stable `audit_sink_failure` marker at ERROR with a comment
    saying a lost audit record "must be ALERTABLE" — and nothing was watching.

    Pinned by metric name rather than by rule count so renaming a metric without moving its alert
    fails here, which is the drift that makes an alerting stack quietly stop covering anything.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    assert "kind: PrometheusRule" in rule
    for metric in [
        "chemclaw_audit_sink_failures_total",
        "chemclaw_notes_publish_failures_total",
        "chemclaw_turn_claim_refresh_failures_total",
        "chemclaw_turns_failed_total",
        "chemclaw_turns_shed_total",
        "chemclaw_connectors_unhealthy",
        "chemclaw_db_unavailable_total",
        "chemclaw_tokens_total",
    ]:
        assert metric in rule, f"{metric} has no alert"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_no_alert_pages_for_a_pod_that_is_merely_still_starting() -> None:
    """`ChemclawTargetDown` is `critical`, and it fired on an ordinary rollout.

    `up` says a target did not answer; it says nothing about readiness, and neither discovery
    mechanism this chart uses waits for it — a PodMonitor's `role: pod` has no readiness filter and
    an Endpoints object carries `notReadyAddresses`. So a pod is a target from the moment it
    exists, `up` is 0 for the whole cold start, and every `probes.*.startup` block here allows
    300 s of cold start on purpose (RDKit, the agent stack, the connector registry). A `for: 5m`
    sat inside that budget.

    Asserted as the relation and not the number, because the number is the derivation: the alert
    must outlast the *largest* startup budget the chart grants, so raising a budget cannot leave it
    behind.
    """
    budgets = {
        component: int(probe["startup"]["periodSeconds"])
        * int(probe["startup"]["failureThreshold"])
        for component, probe in _values()["probes"].items()
    }
    assert budgets, "no startup budgets found — probes moved, not the alert"
    result = _render()
    assert result.returncode == 0, result.stderr
    held = re.search(r"- alert: ChemclawTargetDown(?:.|\n)*?for: (\d+)([ms])", result.stdout)
    assert held is not None, "ChemclawTargetDown no longer renders a for: clause"
    seconds = int(held.group(1)) * (60 if held.group(2) == "m" else 1)
    assert seconds > max(budgets.values()), (
        f"ChemclawTargetDown pages after {seconds}s while the chart grants "
        f"{max(budgets.values())}s of cold start ({budgets}); a normal deploy on a slow node is a "
        "critical page for a process that is starting as designed"
    )


def test_only_the_fleet_group_alerts_on_a_series_this_system_does_not_emit() -> None:
    """The claim the rule file makes about itself, checked instead of written down.

    Its header argued that every rule reads an application counter — "so a process that is gone
    emits silence" — which is what makes `up` and `absent()` necessary. The sentence carried a
    count ("all sixteen") that was thirty-seven by the time anyone read it, and the count was never
    the interesting half: the *split* is. Two rules read Prometheus's own synthesised `up` and
    every other rule reads a series this registry declares, and that is what the header now says
    and this asserts.

    A third rule written against a series nothing here emits would be green forever, which reads
    exactly like the condition never occurring — the same failure
    `test_no_dashboard_panel_queries_a_series_nothing_produces` catches one surface over.
    `temporal_num_pollers` is the sanctioned exception and is rendered only behind its own flag.
    """
    from chemclaw.core.metrics import declared_histogram_names, declared_metric_names

    declared = set(declared_metric_names()) | {
        f"{name}{suffix}"
        for name in declared_histogram_names()
        for suffix in ("_bucket", "_sum", "_count")
    }
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    on_up = set()
    for block in re.split(r"\n\s*- alert: ", rule)[1:]:
        name = block.splitlines()[0].strip()
        expr = " ".join(block.split("expr:")[1].split("for:")[0].split())
        if not re.search(r"\bchemclaw_[a-z0-9_]+\b", expr):
            on_up.add(name)
            assert re.search(r"\bup\{|\btemporal_", expr), (
                f"{name} reads neither a declared series nor `up`, so it can never fire: {expr}"
            )
        for series in re.findall(r"\bchemclaw_[a-z0-9_]+\b", expr):
            assert series in declared, f"{name} reads {series}, which this registry never declares"
    # `ChemclawWorkerNotPolling` is the sanctioned third, and its difference from the two deleted
    # dashboard panels is the whole reason it survives: it is rendered only under
    # `monitoring.temporalSdkMetrics.enabled`, which is the same flag that renders the port the
    # exporter would bind, so it is absent from every shipped configuration rather than green
    # forever in one. The panels were unconditional.
    assert on_up == {
        "ChemclawTargetDown",
        "ChemclawNoWorkerIsScraped",
        "ChemclawWorkerNotPolling",
    }, (
        f"the rules that read something other than a first-party series are {sorted(on_up)}; the "
        "file's header says the fleet group plus the flag-gated SDK rule is the whole of that set"
    )


def test_no_alert_asks_for_to_suppress_what_only_a_threshold_can() -> None:
    """`for:` cannot mean "sustained" over an `increase(...) > 0`, and two rules claimed it did.

    `increase(c[10m]) > 0` returns a sample continuously for ~10 minutes after a *single*
    increment, so a `for:` shorter than the range window is satisfied by one blip: the alert is not
    suppressed, only late. `ChemclawDurableUnreachable` was annotated "`for: 5m` because a single
    broker blip … needs no page" while paging on exactly that, five minutes afterwards;
    `ChemclawRollbackWatermarkUnavailable` had the same shape with a 5m window and a 5m `for:`.
    Both now put the judgement in the count, where it can actually be made.

    The rule is general because the mistake is: a threshold of `0` over a range window admits no
    `for:` short enough to filter anything. `for: 0m` on a `> 0` is correct and stays — three
    alerts here mean the first increment and say so.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    for block in re.split(r"\n\s*- alert: ", rule)[1:]:
        name = block.splitlines()[0].strip()
        expr = block.split("expr:")[1].split("for:")[0]
        window = re.search(
            r"increase\(chemclaw_[a-z_]+\[(\d+)m\]\)\s*>\s*0\b", " ".join(expr.split())
        )
        held = re.search(r"for:\s*(\d+)m", block)
        if window is None or held is None:
            continue
        assert int(held.group(1)) == 0, (
            f"{name} counts every increment over {window.group(1)}m and then waits "
            f"{held.group(1)}m, which suppresses nothing — the expression stays true for the whole "
            "window after one increment. Put the judgement in the threshold instead."
        )


def test_the_corpus_alert_can_fire_for_the_case_it_calls_the_sharper_one() -> None:
    """A sentinel below every legal threshold is a case a `>` comparison can never reach.

    `chemclaw_knowledge_sync_age_seconds` reports `kg/graph.py::NO_NOTES` (-1) for a tree holding no
    note at all — negative on purpose, so an unpopulated volume could never be misread as a corpus
    that had just refreshed. `ChemclawKnowledgeCorpusStale` was `age > {{ threshold }}` and rendered
    only when the threshold is positive, so -1 could not satisfy it under any configuration: the
    alert's own description named that case as the sharper failure while being structurally unable
    to fire for it. The dashboard panel showed it to whoever was looking; nothing paged.

    The opt-in is asserted in the same test because it is the constraint the fix had to respect. An
    empty tree needs no site-specific budget to interpret, which is a real argument for alerting on
    it unconditionally — but the gauge is bound in every process that imports `kg.graph`, and a
    deployment not using the knowledge graph has an empty tree by design. Both arms stay behind the
    one threshold, so opting out is still one number.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    block = re.split(r"\n\s*- alert: ", rule)[1:]
    stale = [item for item in block if item.startswith("ChemclawKnowledgeCorpusStale")]
    assert len(stale) == 1, "the alert this test is about is not in the chart under that name"
    expr = " ".join(stale[0].split("expr:")[1].split("for:")[0].split()).removeprefix(">- ")

    assert "max by (pod) (chemclaw_knowledge_sync_age_seconds) < 0" in expr, (
        "the rule cannot reach the no-notes sentinel: -1 is not greater than a positive threshold, "
        f"so an unpopulated knowledge volume never alerts. Expression is {expr!r}"
    )
    assert "> {{ .Values.monitoring.alerts.knowledgeCorpusStaleSeconds }}" in expr, (
        "the staleness arm is gone — the sentinel arm is an addition to it, not a replacement"
    )

    # And the whole rule is still opt-in: the alert must sit inside the guard, not beside it.
    guard = "{{- if gt (int .Values.monitoring.alerts.knowledgeCorpusStaleSeconds) 0 }}"
    assert guard in rule, "the opt-in guard was renamed or removed"
    guarded = rule.split(guard, 1)[1].split("{{- end }}", 1)[0]
    assert "ChemclawKnowledgeCorpusStale" in guarded, (
        "the alert escaped its opt-in guard, so a deployment with an intentionally empty knowledge "
        "tree is now paged for it and cannot turn it off with `knowledgeCorpusStaleSeconds: 0`"
    )


def test_every_alerted_metric_is_a_metric_the_app_declares() -> None:
    """The other direction: an alert on a metric that does not exist never fires and looks fine.

    A PromQL expression naming a typo'd or deleted series is silently always-empty — the alert is
    green forever, which reads exactly like "the condition never occurred".
    """
    from chemclaw.core.metrics import _COUNTERS, _GAUGE_FAMILIES, _GAUGES, _HISTOGRAMS

    declared = {*_COUNTERS, *_GAUGES, *_HISTOGRAMS, *_GAUGE_FAMILIES}
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    # Only the PromQL, not the prose: the annotations legitimately name metrics in explanations.
    expressions = " ".join(re.findall(r"expr:\s*(?:>-\s*)?((?:.|\n)*?)\n\s*for:", rule))
    # A histogram is queried through its derived series — `_bucket` for `histogram_quantile`, and
    # `_sum`/`_count` for an average — and none of those three is a name the registry declares. The
    # suffix is Prometheus's, not this system's, so stripping it is what makes the comparison a
    # comparison about metric *names*. Gauge families are queried by their bare name and need no
    # such treatment; they are in `declared` below for the first time here.
    referenced = {
        re.sub(r"_(bucket|sum|count)$", "", name)
        for name in re.findall(r"\b(chemclaw_[a-z_]+)\b", expressions)
    }
    assert referenced, "no PromQL expressions were parsed — the extraction is broken, not the rules"
    unknown = referenced - declared
    assert not unknown, f"alerts reference metrics the app never emits: {sorted(unknown)}"


# Supply-chain tooling the runbook's gate section is allowed to name, and the only vocabulary this
# check knows. A named watch-list rather than every backticked token in the section, for the reason
# `tests/test_third_party_layering.py` gives its `_STACKS`: the prose around the table legitimately
# backticks `uv lock`, `BACKLOG.md`, an ADR id and two CVE'd package names, and a check that read
# all of them as gate claims would fail on the next paragraph anyone writes. That is a real limit —
# a scanner nobody named cannot be policed — so a tool that enters this conversation belongs here in
# the same commit that documents it.
_SUPPLY_CHAIN_TOOLS = frozenset(
    {
        "trivy",
        "grype",
        "syft",
        "pip-audit",
        "osv-scanner",
        "snyk",
        "cosign",
        "gitleaks",
        "trufflehog",
        "semgrep",
        "bandit",
    }
)

# What lets a sentence name a tool *without* claiming it runs. The runbook's own vocabulary for a
# control it does not have, kept deliberately short: every marker here is an exemption, so a loose
# one (a bare "no", a bare "not") would let a phantom claim back in through the sentence beside it.
_ABSENCE_MARKERS = ("nowhere", "there is no", "used to say", "is a real gap", "does not run")


def _executable_workflow_text(workflow: str) -> str:
    """Every string in `image.yml` a runner would actually execute: each step's `uses` and `run`.

    Parsed as YAML rather than read as one blob, because a comment is not a control. Comments are
    the largest thing in this workflow — the file is more rationale than command — so a substring
    search over its text answers "is this word written down here", which is the question the runbook
    already answers and not the one worth asking of CI.

    Shell comments inside a `run:` block survive YAML parsing, so they are stripped too: a `#` line
    in a script is exactly as inert as a `#` line in the YAML around it. Only `#` at a line start or
    after whitespace is treated as one, which is the shell's own rule closely enough for scripts
    that quote their arguments.
    """
    document: Any = yaml.safe_load(workflow)
    executed: list[str] = []
    for job in (document.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if isinstance(uses, str):
                executed.append(uses)
            run = step.get("run")
            if isinstance(run, str):
                executed.append(re.sub(r"(?m)(^|\s)#.*$", r"\1", run))
    return "\n".join(executed)


def test_every_supply_chain_gate_the_runbook_names_actually_runs() -> None:
    """A documented control that does not run is worse than a missing one.

    The runbook's supply-chain section described **three** blocking gates and explained how the
    middle one was tuned, in the present tense. `trivy` appeared nowhere in the workflow, the
    Makefile, or anything else that executes — so an operator reading that page believed the image's
    base OS layers were scanned and that a red build would tell them. Prose is not covered by any
    gate, which is exactly why this assertion exists rather than a fourth careful sentence.

    **Both halves of it were defeatable, and an audit defeated both while it stayed green.**

    1. *A comment satisfied it.* `gate in workflow` was a substring over the whole file, and this
       workflow is mostly rationale — so `# NOTE: a trivy image scan is deliberately not run here
       yet.` made the phantom row pass, which is the same phantom control with a second document
       now agreeing with it. Fixed by asking whether the gate *runs*: the workflow is parsed as
       YAML and only each step's `uses`/`run` counts, with shell comments inside a `run:` block
       stripped for the same reason.
    2. *Prose was invisible.* Only rows starting with ``| ` `` were read, so the sentence beside
       the table — which is how this section describes `trivy` today — claimed whatever it liked.
       Fixed by reading the section's sentences too: a sentence naming a supply-chain tool claims
       it runs unless it carries one of `_ABSENCE_MARKERS`, which is how the current text says the
       scan is a gap rather than a gate.

    Still keyed on the gate *names*, not on a count: adding a real scan should make this pass by
    making the claim true, and re-adding a phantom one — in the table or in a sentence — should
    make it fail.
    """
    runbook = (DEPLOY.parent / "docs" / "guides" / "runbook.md").read_text()
    workflow = (DEPLOY.parent / ".github" / "workflows" / "image.yml").read_text()

    section = runbook.split("### When a supply-chain gate goes red", 1)[1].split("\n## ", 1)[0]
    table = [line for line in section.splitlines() if line.startswith("| `")]
    assert table, "the gate table was not found — this check is reading the wrong section"

    named = {re.match(r"\|\s*`([^`]+)`", line).group(1) for line in table}  # type: ignore[union-attr]

    # The prose half. Lines are joined before sentences are split because the section wraps mid
    # sentence, and the table lines are dropped because they are claims already counted above.
    prose = " ".join(line for line in section.splitlines() if not line.startswith("|"))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", prose) if s.strip()]
    assert sentences, "no prose was parsed — this check is reading the wrong section"
    for sentence in sentences:
        lowered = sentence.lower()
        if any(marker in lowered for marker in _ABSENCE_MARKERS):
            continue
        named |= {
            tool for tool in _SUPPLY_CHAIN_TOOLS if re.search(rf"\b{re.escape(tool)}\b", lowered)
        }

    executed = _executable_workflow_text(workflow)
    # `make deps-audit` is how the workflow spells pip-audit; the Makefile target is the real name.
    runs = {
        gate
        for gate in named
        if gate in executed or gate.replace("pip-audit", "deps-audit") in executed
    }
    assert named == runs, (
        f"the runbook names supply-chain gate(s) that nothing runs: {sorted(named - runs)}. "
        "Either merge the gate or stop documenting it as one — a comment in the workflow is not "
        "a gate, and neither is a sentence next to the table."
    )


# --- Rendered-chart assertions (need the `helm` binary) ----------------------------------------
#
# Everything above reads the template *source*. That cannot see what a value's *absence* renders
# to, and absence is where this chart's derivations fail silently: `int nil` is `0` and
# `{{ .Values.config.X }}` on a missing key is the empty string, so a derived number degrades to a
# plausible wrong one rather than refusing. Skipped where `helm` is not installed — the same split
# `tests/test_helm_chart.py`'s docstring describes, with `make helm-validate` as the CI half.


def _render(*overrides: str) -> subprocess.CompletedProcess[str]:
    """`helm template` on the chart, with the egress and retention postures stated.

    `networkPolicy.allowAnyDestination=true` is the same flag the Makefile's two renders, the
    runbook and `deploy/README.md` all pass: the chart refuses to render until a release states
    where its pods may talk, and a validation render has no destinations to enumerate.
    `retention.unboundedGrowthAccepted=true` is the same shape for the sibling guard
    (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`'s retention half): a validation
    render states no disposal policy either, and both flags are what every other caller of this
    chart pays too — see `test_the_shipped_defaults_still_render`.
    """
    return subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "retention.unboundedGrowthAccepted=true",
            *overrides,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize(
    ("key", "helper"),
    [
        ("CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS", "deployment-service.yaml"),
        ("CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS", "chemclaw.workerGracePeriod"),
        ("CHEMCLAW_KNOWLEDGE_DIR", "chemclaw.knowledgePublishPath"),
        ("CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS", "deployment-service.yaml"),
        ("CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS", "deployment-service.yaml"),
    ],
)
def test_a_derived_value_refuses_rather_than_rendering_a_plausible_wrong_one(
    key: str, helper: str
) -> None:
    """Three templates derive a value from a `config` key. Removing the key must stop the render.

    Deriving is the right instinct — `_helpers.tpl`'s own comment argues it at length: "a path that
    only has to *agree* with another path eventually does not, so this one is derived rather than
    declared". What the derivations lacked was the other half, which `deployment-connectors.yaml`
    already had: `required`. Without it the degradation is silent and each one is worse than a
    crash —

    * `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` absent rendered `terminationGracePeriodSeconds: 15`
      on the front door (`0 + drainSeconds`) while `Settings` still ran turns to 600 s, so every
      rolling update and node drain SIGKILLed in-flight conversations — the exact regression
      `deployment-service.yaml`'s comment says it fixed;
    * `CHEMCLAW_WORKER_GRACEFUL_SHUTDOWN_SECONDS` absent rendered 30 s against a 120 s drain;
    * `CHEMCLAW_KNOWLEDGE_DIR` absent published to `<noteRepoPath>/` while every reader resolves
      `note_repo_dir / knowledge_dir` — the silent empty-knowledge-tree failure the same helper's
      comment narrates and claims to have made impossible;
    * either half of the `/readyz` budget absent renders a readiness `timeoutSeconds` a whole
      budget short, so the kubelet drains a front door that is still inside the time the app was
      configured to take.

    An operator reaches this by moving one key into an ExternalSecret, a sidecar-injected env, or
    simply `--set config.<KEY>=null` after deciding the code default is fine.
    """
    result = _render("--set", f"config.{key}=null")
    assert result.returncode != 0, (
        f"{helper} still rendered with {key} absent:\n{result.stdout[:2000]}"
    )
    assert key in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(("--set", "connectors=null"), id="block-removed"),
        pytest.param(
            # Derived from the values file rather than typed out. It *was* typed out, as the seven
            # bundles that existed the day it was written, and the eighth (`rxnpredict`) turned
            # "all disabled" into "all but one disabled" — so the release rendered, correctly, and
            # this arm failed reporting a guard that had not broken. A list of the whole set is a
            # thing only the whole set can supply.
            tuple(
                arg
                for name in sorted(_values()["connectors"])
                for arg in ("--set", f"connectors.{name}.enabled=false")
            ),
            id="all-disabled",
        ),
    ],
)
def test_a_release_that_enables_no_connector_does_not_render(overrides: tuple[str, ...]) -> None:
    """Both spellings of "no connectors", because the `fail` only ever caught one of them.

    `chemclaw.connectorsEnabled` refused the all-disabled release and told the operator to *remove
    the connectors block entirely* instead — a remedy that skipped the guard's own
    `and .Values.connectors` condition and rendered `CHEMCLAW_CONNECTORS_ENABLED: ""`, which
    `connectors_enabled_list` reads as **every discovered bundle**. Together with
    `CHEMCLAW_CONNECTOR_URLS: "{}"` every bundle then fell back to its manifest's loopback dev
    address, so the front door advertised all seven bundles' tools while dialling its own pod: the
    "pods gone, tools advertised" regression that helper exists to close, reached through the door
    the message left open.

    It also rendered `matchExpressions … values: null` on the connector-ingress NetworkPolicy,
    which the Kubernetes API rejects and `kubeconform -strict` passes — so `--atomic` rolled the
    release back with an error naming neither connectors nor the values file. That selector is
    unreachable once this render refuses, which is why there is no separate guard on it.
    """
    result = _render(*overrides)
    assert result.returncode != 0, f"a connector-less release rendered:\n{result.stdout[:2000]}"
    assert "CHEMCLAW_CONNECTORS_ENABLED" in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_shipped_defaults_still_render() -> None:
    """The control the refusals above are worthless without: `required` on a key that *is* set."""
    result = _render()
    assert result.returncode == 0, result.stderr
    assert "terminationGracePeriodSeconds: 615" in result.stdout
    assert "terminationGracePeriodSeconds: 150" in result.stdout


# What a switch needs *besides itself* to render the branch it gates. The only literal here, and it
# is a statement about prerequisites rather than a list of switches: `monitoring.alertmanager`
# refuses to render with no receivers (deliberately — `templates/alertmanagerconfig.yaml`), and
# `mcpFace.route` renders nothing at all without the Deployment it publishes. A switch absent from
# this map needs nothing.
_SWITCH_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "monitoring.alertmanager.enabled": (
        "--set-json",
        'monitoring.alertmanager.receivers=[{"name":"chemclaw-oncall"}]',
        "--set",
        "monitoring.alertmanager.defaultReceiver=chemclaw-oncall",
    ),
    "mcpFace.route.enabled": ("--set", "mcpFace.enabled=true"),
}


def _off_by_default_switches() -> list[str]:
    """Every `enabled`/`create` boolean `values.yaml` ships **false** that a template reads.

    Derived, because the set this shares with the `Makefile` is exactly the set no gate has ever
    rendered — `make helm-validate`, the CI `chart` job and every `_render()` above take the shipped
    defaults, so a template behind one of these flags is validated by nobody until an operator turns
    it on in their own cluster. Two of them rendered objects the API server rejects.

    It shipped as a hand-written dict of three under a comment claiming "a flag added next year is
    covered the day it is added rather than the day someone remembers to widen a test", which is the
    one thing a literal cannot do: `secrets.create`, `mcpFace.route.enabled` and
    `monitoring.alertmanager.enabled` were already missing from it, and the first two were rendered
    by nothing anywhere in `tests/`, the `Makefile` or `.github/`.

    A *switch* is a key named `enabled` or `create`, which is this chart's own convention and the
    line that keeps the two posture flags (`allowAnyDestination`, `unboundedGrowthAccepted`) out —
    those are not features, they are the statements `_render` already makes on every call.
    """
    templates = "\n".join(_template_text().values())
    switches: list[str] = []

    def walk(node: object, path: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key in ("enabled", "create") and value is False and f".Values.{here}" in templates:
                switches.append(here)
            walk(value, here)

    walk(_values(), "")
    assert switches, "no off-by-default switch found — the derivation is broken, not the chart"
    return sorted(switches)


def _off_by_default_renders() -> dict[str, tuple[str, ...]]:
    """The shipped defaults, each off-by-default switch on its own, and all of them at once."""
    renders: dict[str, tuple[str, ...]] = {"defaults": ()}
    everything: list[str] = []
    for switch in _off_by_default_switches():
        flags = ("--set", f"{switch}=true", *_SWITCH_PREREQUISITES.get(switch, ()))
        renders[switch] = flags
        everything += list(flags)
    renders["every-switch-on"] = tuple(everything)
    return renders


_OFF_BY_DEFAULT_RENDERS = _off_by_default_renders()


def test_the_union_render_covers_every_switch_this_chart_ships_off() -> None:
    """`make helm-validate`'s second arm claims "every switch this chart ships **off**"; check it.

    That arm is the only place an off-by-default template is put through `kubeconform` at all, and
    its flag list is a literal in a shell loop — so the claim above it goes stale the first time a
    switch is added, exactly as it already had (three of six). Read out of the `Makefile` rather
    than restated here, the same way `_jenkins_render_flags` reads the pipeline: a copy of the list
    would be a second answer to the question and would stay green while the render narrowed.
    """
    makefile = (DEPLOY.parent / "Makefile").read_text()
    arm = makefile.split("helm-validate:", 1)[1].split("\nupstream-check:", 1)[0]
    missing = [switch for switch in _off_by_default_switches() if f"{switch}=true" not in arm]
    assert not missing, (
        f"`make helm-validate` never renders {missing}, so those templates reach kubeconform for "
        "the first time in an operator's cluster. Add them to the union render's flag list."
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_every_waited_on_hook_job_carries_a_deadline(overrides: tuple[str, ...]) -> None:
    """The same argument as the test above, applied to every hook rather than the one that made it.

    Helm waits for each hook Job it creates, so *any* of them with no `activeDeadlineSeconds` can
    hold the release in `pending-install`/`pending-upgrade` indefinitely — and `backoffLimit` does
    not help, because it bounds failures and a hang is not a failure. `migrate` and `convert` each
    carry one and argue for it; the Schedules Job carried neither a deadline nor a value to set one.

    Its hang is a real shape rather than a theoretical one: `chemclaw.cli.schedules` connects to
    Temporal and calls `list_schedules`/`create_schedule`, and temporalio's `DEFAULT_RPC_TIMEOUT`
    is `None` — a frontend that completes the gRPC handshake and then stalls leaves the pod running
    forever. (A *refused* frontend does terminate, which is why this went unnoticed.)

    Over the **rendered** manifests rather than the templates matching `*job*.yaml`, because the
    thing being asserted is a property of hook Jobs and the glob was a property of filenames: a hook
    Job in `templates/backfill.yaml` is outside it, and so is one a `define` emits. Parametrised
    over the off-by-default variants for the same reason — a hook Job behind a feature flag is a
    hook Job Helm waits for.
    """
    for document in yaml.safe_load_all(_render(*overrides).stdout):
        if not document or document.get("kind") != "Job":
            continue
        annotations = document["metadata"].get("annotations") or {}
        if "helm.sh/hook" not in annotations:
            continue
        assert document["spec"].get("activeDeadlineSeconds"), (
            f"the {document['metadata']['name']} hook Job has no deadline, so a hang — not a "
            "failure, which `backoffLimit` covers — pins the release in `pending-upgrade` and "
            "blocks every later `helm upgrade`"
        )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_no_http_served_container_starts_without_a_head_start_or_a_drain(
    overrides: tuple[str, ...],
) -> None:
    """The two guards `values.yaml`'s `probes:` block argues for, asserted where they can be seen.

    `test_a_connector_server_is_not_sigkilled_before_it_finishes_starting` makes the case in full
    and reads exactly one template, so the component that needed it most was outside it: `mcp-face`
    declared `initialDelaySeconds`/`periodSeconds` and left `timeoutSeconds: 1` and
    `failureThreshold: 3` to Kubernetes, with no startup probe at all — first liveness kill about
    100 s after start — while importing strictly *more* than the connector server that probe was
    written for (measured 2800 ms against 1646 ms; `agent.tool_modules` seeds the whole tool
    registry). It also had no `terminationGracePeriodSeconds`, so it took the 30 s default where the
    front door has 615 and a connector 3610.

    Rendered rather than read, so a probe supplied by a helper (`chemclaw.workerProbes`) counts the
    same as one written into a template, and every future component is covered without being named.
    Containers whose only probe is an `exec` — the knowledge-sync sidecar — are out of scope: they
    serve nothing, and a startup probe on a loop that has no first response is meaningless.
    """
    result = _render(*overrides)
    assert result.returncode == 0, result.stderr
    for name, spec in _pod_specs(result.stdout):
        serves_http = False
        for container in spec.get("containers") or []:
            probes = {
                kind: container[kind]
                for kind in ("startupProbe", "readinessProbe", "livenessProbe")
                if container.get(kind)
            }
            if not any("httpGet" in probe for probe in probes.values()):
                continue
            serves_http = True
            assert "startupProbe" in probes, (
                f"{name}/{container['name']} serves HTTP probes with no startup probe, so liveness "
                "runs during the import that delays its first response"
            )
            for kind, probe in probes.items():
                missing = {"periodSeconds", "timeoutSeconds", "failureThreshold"} - probe.keys()
                # Only the startup probe's thresholds are asserted across the board: the workers'
                # readiness and liveness leave `timeoutSeconds` to the default deliberately, and
                # tightening them is a separate decision from giving a cold start room to finish.
                if kind == "startupProbe":
                    assert not missing, (
                        f"{name}/{container['name']}: {kind} leaves {sorted(missing)} to a "
                        "Kubernetes default"
                    )
        # A hook Job serves nothing and drains nothing — it is bounded by `activeDeadlineSeconds`
        # instead — so the drain is asked of the pods that are behind a Service.
        if serves_http:
            assert spec.get("terminationGracePeriodSeconds"), (
                f"{name} states no terminationGracePeriodSeconds, so it takes the 30 s default and "
                "is SIGKILLed through whatever it was holding"
            )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_every_pod_a_service_routes_to_stops_being_chosen_before_it_stops_accepting(
    overrides: tuple[str, ...],
) -> None:
    """The other half of the drain, and `mcp-face` shipped with only the half that cannot do it.

    Kubernetes removes a terminating pod's Endpoint and sends SIGTERM **concurrently**, so a router
    keeps choosing a pod that has already stopped accepting. `terminationGracePeriodSeconds` bounds
    how long the kubelet waits *after* SIGTERM and so touches none of that; the `preStop` sleep is
    what closes it, which is what `deploy/README.md` states and what the front door and every
    connector server render. `mcp-face` was given the front door's 615 s grace period under a
    comment claiming the Endpoint race was the thing being fixed, and no sleep — so its rollouts,
    node drains and scale-downs still reset connections to the calling agent.

    Scoped to pods a **Service** selects, derived from the same render rather than named: an HTTP
    probe is a kubelet asking, not traffic arriving, and the Temporal workers serve `/healthz` on
    their metrics port with no Service in front of them. Nothing routes to them, so there is nothing
    to stop routing.
    """
    rendered = _render(*overrides)
    assert rendered.returncode == 0, rendered.stderr
    documents = [document for document in yaml.safe_load_all(rendered.stdout) if document]
    selectors = [
        document["spec"]["selector"]
        for document in documents
        if document["kind"] == "Service" and document["spec"].get("selector")
    ]
    assert selectors, "the render declares no Service — the derivation is broken, not the chart"

    for name, spec in _pod_specs(rendered.stdout):
        labels = _pod_labels(documents, name)
        if not any(labels.items() >= selector.items() for selector in selectors):
            continue
        for container in spec.get("containers") or []:
            if not any(
                "httpGet" in (container.get(kind) or {})
                for kind in ("startupProbe", "readinessProbe", "livenessProbe")
            ):
                continue
            assert (container.get("lifecycle") or {}).get("preStop"), (
                f"{name}/{container['name']} is behind a Service and has no preStop drain, so "
                "every rollout resets the requests the router sends between SIGTERM and the "
                "Endpoint's removal"
            )


def _pod_labels(documents: list[dict[str, Any]], workload_name: str) -> dict[str, str]:
    """The pod-template labels of the rendered workload named `workload_name`."""
    for document in documents:
        if document["kind"] in ("Deployment", "StatefulSet", "Job") and (
            document["metadata"]["name"] == workload_name
        ):
            labels = document["spec"]["template"]["metadata"].get("labels") or {}
            assert isinstance(labels, dict)
            return labels
    return {}


def _render_manifest_only(*overrides: str) -> str:
    """The render Helm actually *tracks* as the release: `--no-hooks`.

    Helm keeps hook resources out of the release manifest entirely, so `--no-hooks` is exactly the
    set that `helm rollback` restores and `helm uninstall` removes. That makes it the honest way to
    ask "is this object part of the release" without a cluster.
    """
    result = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "retention.unboundedGrowthAccepted=true",
            "--no-hooks",
            *overrides,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_configuration_the_pods_read_is_part_of_the_release() -> None:
    """`helm rollback` restored the pods and left the new release's configuration live.

    The entire non-secret configuration was a `pre-install,pre-upgrade` hook. Helm does not record
    hook resources in the release manifest, and `helm rollback` runs only `pre-rollback`/
    `post-rollback` hooks — of which this chart declares none — so a rollback reverted every
    Deployment (including its `checksum/config` annotation, restarting every pod) while the
    ConfigMap those pods read still held the *new* release's values. Measured against a real API
    server, rolling back a release that had set `connectors.bo.enabled=false` restored
    `chemclaw-connector-bo`'s Deployment and Service while `CHEMCLAW_CONNECTORS_ENABLED` still
    omitted `bo`: the pods run and the capability stays dark. `helm uninstall` left both objects
    behind for the same reason.

    The rename is half of the fix and this docstring shipped calling it the whole of it ("the fix is
    not an annotation but a rename"): two objects cannot share a name across the hook/manifest
    boundary, so the *hook* copies the pre-install migrate Job needs took new names and the names
    the running pods reference became ordinary tracked resources — and that leaves the tracked pair
    claiming two names every live release already holds. What the boundary costs in both directions,
    and what pays it, is
    `test_the_pair_the_previous_chart_hooked_is_not_deleted_by_a_rollback_across_the_boundary`
    below.

    Asserted through `--no-hooks`, which is precisely the set Helm tracks.
    """
    tracked = {
        (doc["kind"], doc["metadata"]["name"])
        for doc in yaml.safe_load_all(_render_manifest_only())
        if doc
    }
    assert ("ConfigMap", "chemclaw-config") in tracked, (
        "the ConfigMap every pod reads is a Helm hook, so `helm rollback` cannot restore it and "
        "`helm uninstall` cannot remove it"
    )
    assert ("ServiceAccount", _values()["serviceAccount"]["name"]) in tracked, (
        "the ServiceAccount every pod runs as is a Helm hook, so the release does not own it"
    )
    # And the hook copies the pre-install Job needs must not collide with them: Helm refuses to
    # adopt an object that exists as a hook into the manifest, so a shared name is not a smaller
    # version of this fix, it is a release that stops installing.
    hooked = {
        (doc["kind"], doc["metadata"]["name"])
        for doc in yaml.safe_load_all(_render().stdout)
        if doc and (doc["metadata"].get("annotations") or {}).get("helm.sh/hook")
    }
    assert not hooked & tracked, (
        f"an object is both a hook and part of the manifest: {hooked & tracked}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_pair_the_previous_chart_hooked_is_not_deleted_by_a_rollback_across_the_boundary() -> (
    None
):
    """Moving two objects into the manifest made `helm rollback` across that move an outage.

    `helm rollback` deletes anything in the current manifest that the target revision's manifest
    lacks — and a revision installed by the previous chart has no ConfigMap and no ServiceAccount in
    its manifest at all, because both were hooks. So rolling back across the boundary deletes the
    configuration and the identity that the very Deployments it is restoring name in a non-optional
    `envFrom` and a `serviceAccountName`, and prints "Rollback was a success!".

    Measured against a real API server (k3s v1.29.9), release installed from `d247224`'s chart and
    upgraded to this one: after `helm rollback <rel> 1`, `configmaps "chemclaw-config" not found`
    and `serviceaccounts "chemclaw" not found`. With the annotation this test pins, the same
    sequence leaves both standing. **That is what actually proves it, and this assertion is not
    that** — a render cannot execute a rollback. What a render *can* pin is the one input Helm reads
    at deletion time, so the annotation cannot be dropped by someone who has not met the cluster.

    The cost is stated in `templates/config.yaml` and in `deploy/README.md`: `helm uninstall` leaves
    these two behind. `keep` skips deletion only, so a rollback inside this chart's own lineage
    still restores the previous revision's `data:` — which is the whole point of having moved them.
    """
    tracked = {
        (document["kind"], document["metadata"]["name"]): document["metadata"].get("annotations")
        or {}
        for document in yaml.safe_load_all(_render_manifest_only())
        if document
    }
    for key in (
        ("ConfigMap", "chemclaw-config"),
        ("ServiceAccount", _values()["serviceAccount"]["name"]),
    ):
        assert tracked[key].get("helm.sh/resource-policy") == "keep", (
            f"{key[0]}/{key[1]} is tracked without `helm.sh/resource-policy: keep`, so a rollback "
            "to a revision installed before this chart deletes it while restoring Deployments that "
            "cannot start without it — and Helm reports success"
        )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_pre_install_hook_reads_a_configuration_that_exists_when_it_runs() -> None:
    """The other half, and the reason the hook copies exist at all.

    Helm runs `pre-install` hooks *before* any ordinary resource is applied, so a migrate Job that
    referenced the now-tracked `chemclaw-config`/ServiceAccount would fail on a fresh install
    against objects that do not exist yet — trading a rollback defect for an install defect. It
    therefore reads hook-scoped copies, rendered from the same values in the same release. On an
    *upgrade* that is also the more correct source: at `pre-upgrade` the tracked ConfigMap still
    holds the previous release's values, and the hook copy holds this one's.

    The `post-install`/`post-upgrade` Jobs read the tracked objects deliberately — by then the
    manifest is applied, and `convert` runs as the runtime role against the release that is now
    live, so the configuration it should see is the one the pods see.
    """
    hooks = {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(_render().stdout)
        if doc and (doc["metadata"].get("annotations") or {}).get("helm.sh/hook")
    }
    for name, expected in (
        ("chemclaw-migrate", "pre-install,pre-upgrade"),
        ("chemclaw-convert", "post-install,post-upgrade"),
        ("chemclaw-schedules", "post-install,post-upgrade"),
    ):
        assert hooks[name]["metadata"]["annotations"]["helm.sh/hook"] == expected

    def sources(job: dict[str, Any]) -> tuple[str, str]:
        spec = job["spec"]["template"]["spec"]
        container = spec["containers"][0]
        return spec["serviceAccountName"], container["envFrom"][0]["configMapRef"]["name"]

    pre_sa, pre_config = sources(hooks["chemclaw-migrate"])
    assert (pre_sa, pre_config) != (
        _values()["serviceAccount"]["name"],
        "chemclaw-config",
    ), (
        "the pre-install migrate Job reads objects the manifest creates after it runs, so a fresh "
        "`helm install` has no ConfigMap or ServiceAccount for it"
    )
    for name in (pre_sa, pre_config):
        assert name in hooks, f"the migrate Job reads {name!r}, which is neither a hook nor tracked"
        assert hooks[name]["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
        weight = int(hooks[name]["metadata"]["annotations"]["helm.sh/hook-weight"])
        assert weight < int(
            hooks["chemclaw-migrate"]["metadata"]["annotations"]["helm.sh/hook-weight"]
        ), f"{name} is created at the same weight as the Job that needs it, so the order is luck"

    for job in ("chemclaw-convert", "chemclaw-schedules"):
        assert sources(hooks[job]) == (_values()["serviceAccount"]["name"], "chemclaw-config"), (
            f"{job} runs after the manifest is applied and should read the release's own objects"
        )


def _declared_fleet_pools(*overrides: str) -> int:
    """`CHEMCLAW_PG_FLEET_POOLS` as this render puts it in the ConfigMap."""
    result = _render(*overrides)
    assert result.returncode == 0, result.stderr
    return int(_rendered_config(result.stdout)["CHEMCLAW_PG_FLEET_POOLS"])


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_turning_on_a_pooled_component_moves_the_declared_connection_budget() -> None:
    """`mcp-face` opens a Postgres pool like every other pooled process and was counted by nobody.

    It runs `connectors/server.py` over the in-process read-only tool set — knowledge search,
    fingerprint search, precedent lookup — so it holds up to `CHEMCLAW_PG_POOL_MAX_SIZE`
    connections per replica. `chemclaw.fleetPools` summed the front door (or its HPA maximum),
    the background worker and each connector half, and never visited `.Values.mcpFace`. The startup
    guard in `core/config` checks the *declared* number against `postgres.maxConnections`, so an
    undercount cannot make it fire: at ten face replicas the fleet opens 192 connections against a
    declared ceiling of 136 and every pod's `Settings` validation passes. The only thing left is the
    runtime `ChemclawFleetAboveItsConnectionCeiling` alert — a failure found after the pods are up.

    Asserted as the *difference* between two renders rather than against a modelled total: that
    isolates the term this test is about, needs no second copy of the helper's arithmetic here, and
    keeps saying the same thing when a replica default moves. One pool per face replica, not the
    front door's three: it serves read-only tools and takes no turn, so it holds neither a
    checkpointer pool nor a readiness probe's.
    """
    baseline = _declared_fleet_pools()
    for replicas in (1, 10):
        with_face = _declared_fleet_pools(
            "--set", "mcpFace.enabled=true", "--set", f"mcpFace.replicas={replicas}"
        )
        assert with_face - baseline == replicas, (
            f"enabling mcp-face at {replicas} replicas moved the declared fleet pool count by "
            f"{with_face - baseline}; every one of those pods opens a pool, so the fleet's "
            "connection ceiling is understated by the difference and the guard cannot fire"
        )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_scaling_the_front_door_moves_the_budget_by_the_pools_a_front_door_holds() -> None:
    """One more front-door replica is three more pools, and the chart used to say one.

    The measured shape (`tests/test_fleet_pools.py`): a front-door process holds the stores' pool,
    the `/readyz` probe's own key and the checkpointer's registered pool — 3 × `pg_pool_max_size`
    connections, not one pool's worth. Counting pods is what let the shipped chart declare 136 for
    a fleet whose floor is 208, and it is the front door that scales, so the error grew with the
    HPA rather than staying a fixed offset.

    A difference between two renders for the same reason the face test takes one: it isolates the
    term and survives a change to any other replica default.
    """
    baseline = _declared_fleet_pools()
    ceiling = int(_values()["service"]["autoscaling"]["maxReplicas"])
    for extra in (1, 4):
        scaled = _declared_fleet_pools(
            "--set", f"service.autoscaling.maxReplicas={ceiling + extra}"
        )
        assert scaled - baseline == extra * POOLS_PER_FRONT_DOOR, (
            f"{extra} more front-door replica(s) moved the declared fleet pool count by "
            f"{scaled - baseline}, not {extra * POOLS_PER_FRONT_DOOR}; each opens "
            f"{POOLS_PER_FRONT_DOOR} pools, so the ceiling is understated by the difference and "
            "the startup guard cannot fire"
        )


def _pod_specs(rendered: str) -> list[tuple[str, dict[str, Any]]]:
    """Every pod spec in a render, named by its owner — Deployments and Jobs alike."""
    specs: list[tuple[str, dict[str, Any]]] = []
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in {"Deployment", "Job", "StatefulSet"}:
            continue
        specs.append((doc["metadata"]["name"], doc["spec"]["template"]["spec"]))
    return specs


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_every_mounted_volume_is_a_volume_the_pod_declares(overrides: tuple[str, ...]) -> None:
    """A `volumeMounts` entry naming no volume is rejected at apply, and by nothing before it.

    `kubeconform` validates each object against its OpenAPI schema, and "this mount names a volume
    in the same pod" is a cross-field invariant no schema expresses — so the whole render gate says
    `Valid` and the API server says `spec.template.spec.containers[0].volumeMounts[1].name: Not
    found: "note-repo"`. Under `--atomic` that failure rolls a whole release back.

    That is what `mcpFace.enabled=true` shipped: the face includes `chemclaw.knowledgeMounts` (which
    carries `chemclaw.noteRepoMount`) exactly as `deployment-service.yaml` and
    `deployment-workers.yaml` do, and was the only one of the three that did not also include
    `chemclaw.noteRepoVolume` beside `chemclaw.volumes`. Three containers — the server, the
    knowledge-sync sidecar and its init container — mounted a volume the pod never declared.

    Asserted over every pod spec rather than that one template, because the defect is a *pairing*
    between two helpers and any future template can get the pairing wrong the same way.
    """
    result = _render(*overrides)
    assert result.returncode == 0, result.stderr
    for name, spec in _pod_specs(result.stdout):
        declared = {volume["name"] for volume in spec.get("volumes") or []}
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for mount in container.get("volumeMounts") or []:
                assert mount["name"] in declared, (
                    f"{name}/{container['name']} mounts {mount['name']!r}, which the pod does not "
                    f"declare (it declares {sorted(declared)}); the API server rejects this"
                )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_every_container_port_name_is_one_kubernetes_accepts(overrides: tuple[str, ...]) -> None:
    """A container port name is an `IANA_SVC_NAME`: at most 15 characters.

    `kubeconform` agrees with any length, so nothing in the render gate sees it.

    `monitoring.temporalSdkMetrics.enabled=true` named the port `temporal-metrics` — 16 characters
    — in `chemclaw.workerProbes`, which every worker Deployment in the chart includes, so one
    supported switch made all four invalid at apply time at once. The switch is fully built out
    around that name (a PodMonitor endpoint, a NetworkPolicy port, the `ChemclawWorkerNotPolling`
    alert), which is what made a render nobody ran the only thing between it and a cluster.

    The length is the rule that bit; the character class is asserted with it because the same
    validator enforces both and a name like `Temporal_SDK` fails for the other half.
    """
    result = _render(*overrides)
    assert result.returncode == 0, result.stderr
    for name, spec in _pod_specs(result.stdout):
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for port in container.get("ports") or []:
                port_name = port.get("name")
                if port_name is None:
                    continue
                assert len(port_name) <= 15, (
                    f"{name}/{container['name']}: port name {port_name!r} is "
                    f"{len(port_name)} characters; Kubernetes rejects anything over 15"
                )
                assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", port_name), (
                    f"{name}/{container['name']}: port name {port_name!r} is not an IANA_SVC_NAME"
                )


def test_a_connector_server_is_not_sigkilled_before_it_finishes_starting() -> None:
    """The one latent outage in this pass, and it was an *absence* of numbers rather than bad ones.

    `deployment-connectors.yaml` declared `readinessProbe` and `livenessProbe` with no
    `initialDelaySeconds`, `periodSeconds`, `timeoutSeconds` or `failureThreshold` at all.
    Kubernetes' defaults then apply — liveness from t=0, a 10 s period, a 1 s timeout and three
    failures — so the kubelet SIGKILLs the container about thirty seconds after start. `calc` and
    `molfp` import RDKit and open a Postgres pool during FastAPI lifespan and uvicorn accepts
    nothing until lifespan returns, so a cold start on a throttled node crash-loops forever with
    nothing wrong anywhere in it. The workers were never exposed to it because
    `define "chemclaw.workerProbes"` states its thresholds and argues for them.

    Pinned as "a startup probe exists and buys more than a minute", not as the exact numbers: the
    budget is a deployment's to tune and the invariant is that a cold start is not a restart.
    """
    text = (CHART / "templates" / "deployment-connectors.yaml").read_text()
    assert "startupProbe:" in text, (
        "the connector server has no startup probe, so liveness runs during the import that "
        "delays its first response"
    )
    probes = _values()["probes"]["connector"]
    budget = int(probes["startup"]["periodSeconds"]) * int(probes["startup"]["failureThreshold"])
    assert budget >= 60, f"a {budget}s cold-start budget is inside RDKit's import time"
    # Every probe on this container states its own periods, or a default fills the gap silently.
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        body = text.split(f"{probe}:", 1)[1].split("Probe:", 1)[0]
        assert "periodSeconds:" in body and "failureThreshold:" in body, (
            f"{probe} leaves a threshold to a Kubernetes default"
        )
    # Liveness must be the slower of the two, or an unhealthy connector is restarted before it is
    # taken out of its Service — the reverse of what an in-flight MCP tool call wants.
    liveness = int(probes["liveness"]["periodSeconds"]) * int(
        probes["liveness"]["failureThreshold"]
    )
    readiness = int(probes["readiness"]["periodSeconds"]) * int(
        probes["readiness"]["failureThreshold"]
    )
    assert liveness > readiness, (
        "liveness reacts no slower than readiness, so a struggling connector is restarted rather "
        "than removed from its endpoints"
    )


def test_the_front_door_gets_the_same_head_start() -> None:
    """The same gap, one process bigger: langchain, deepagents and RDKit, then a connector sweep.

    `initialDelaySeconds: 10` with the default `failureThreshold: 3` over a 20 s period put the
    first liveness restart at ~70 s, which a cold or throttled node spends inside the import.
    """
    text = (CHART / "templates" / "deployment-service.yaml").read_text()
    assert "startupProbe:" in text, "the front door restarts itself mid-import on a slow node"
    startup = _values()["probes"]["service"]["startup"]
    budget = int(startup["periodSeconds"]) * int(startup["failureThreshold"])
    assert budget >= 60, f"a {budget}s cold-start budget is inside the front door's import time"


def test_the_readiness_probe_states_a_timeout_at_all() -> None:
    """The offline half: neither front-door probe may leave a bound to a Kubernetes default.

    `timeoutSeconds` defaults to 1 and `failureThreshold` to 3, and `/readyz` is the one route in
    this chart whose own answer has a budget — a connector sweep (for a jobs-only bundle, a
    `DescribeTaskQueue` RPC) and then Postgres. Under the default a blackholed broker took ~2 s per
    poll and three of those removed a perfectly serving front door from its Service after ~30 s of
    somebody else's outage. Text-only, so it runs where `helm` is absent; whether the number is
    *large enough* is a question about rendered values, and the test below renders them.
    """
    text = (CHART / "templates" / "deployment-service.yaml").read_text()
    for probe in ("readinessProbe", "livenessProbe"):
        body = text.split(f"{probe}:", 1)[1].split("Probe:", 1)[0]
        assert "timeoutSeconds:" in body and "failureThreshold:" in body, (
            f"the front door's {probe} leaves a threshold to a Kubernetes default"
        )


def _service_probes(rendered: str) -> dict[str, Any]:
    """The front-door container's probes, off the *rendered* Deployment rather than the template."""
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "Deployment":
            continue
        if doc["metadata"]["name"] != "chemclaw-service":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] == "service":
                probes = {k: v for k, v in container.items() if k.endswith("Probe")}
                assert isinstance(probes, dict)
                return probes
    raise AssertionError("no chemclaw-service Deployment with a `service` container was rendered")


def _rendered_config(rendered: str) -> dict[str, str]:
    """`.Values.config` as the pods actually receive it: the rendered ConfigMap's data.

    The ConfigMap rather than `values.yaml`, because that is the artefact an override reaches. A
    guard that parses the file on disk cannot see `--set config.X=…`, an ExternalSecret, or a
    values overlay — which is the whole class of drift this pair of tests exists for.
    """
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "chemclaw-config":
            data = doc["data"]
            assert isinstance(data, dict)
            return data
    raise AssertionError("no chemclaw-config ConfigMap was rendered")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_readiness_probe_outlasts_the_work_readyz_does() -> None:
    """The kubelet's patience and the app's own budgets, checked against each other as rendered.

    `/readyz` may spend `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS` sweeping the connectors and
    `CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS` asking Postgres. If the probe gives up first,
    the kubelet drains a front door that is answering correctly — the failure this whole block
    exists to prevent, arrived at from the other side.

    **The comparison is between two rendered numbers, and that is the fix.** This test used to
    derive its floor from the *test runner's* `Settings` object, so it compared the chart's probe
    against the code defaults — a pair that agree in CI no matter what a release does. An operator
    raising the connector budget through `.Values.config` (the honest response to a slow fleet)
    rendered an unchanged `timeoutSeconds: 5` and this test stayed green, which is precisely the
    drift it was written to catch. Both sides now come out of one `helm template`.
    """
    result = _render()
    assert result.returncode == 0, result.stderr
    config = _rendered_config(result.stdout)
    work = float(config["CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS"]) + float(
        config["CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS"]
    )
    timeout = float(_service_probes(result.stdout)["readinessProbe"]["timeoutSeconds"])
    assert timeout >= work, (
        f"/readyz may spend {work}s answering (the connector sweep plus the database probe) and "
        f"the probe gives up after {timeout}s"
    )
    assert timeout > work, "no margin is left for the request itself, only for the work inside it"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_raising_the_apps_readiness_budget_raises_the_probe_that_waits_for_it() -> None:
    """The drift itself, driven: move the app-side budget and the kubelet must move with it.

    A literal `timeoutSeconds: 5` beside two configurable budgets is a number that only has to
    *agree* with them, and the derivation is what makes it one number instead of two. 9 s is chosen
    to exceed the old literal on its own, so a template that kept it fails here rather than passing
    on a coincidence of margins.
    """
    result = _render("--set", "config.CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS=9")
    assert result.returncode == 0, result.stderr
    config = _rendered_config(result.stdout)
    assert config["CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS"] == "9"
    timeout = float(_service_probes(result.stdout)["readinessProbe"]["timeoutSeconds"])
    work = 9 + float(config["CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS"])
    assert timeout >= work, (
        f"the app was given a {work}s readiness budget and the kubelet still gives up after "
        f"{timeout}s, so raising the connector timeout drains the pod it was raised for"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_fractional_budget_rounds_the_probe_up_rather_than_down() -> None:
    """Both budgets are float seconds and `timeoutSeconds` is an integer, so rounding has a side.

    `int 2.5` truncates to 2, which would hand back a fraction of the gap the derivation closes —
    quietly, and only for deployments that tune in tenths.
    """
    result = _render("--set", "config.CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS=2.5")
    assert result.returncode == 0, result.stderr
    assert float(_service_probes(result.stdout)["readinessProbe"]["timeoutSeconds"]) == 6


def test_the_chart_states_the_readiness_budgets_the_code_defaults_to() -> None:
    """The third pair: what the chart declares and what `Settings` falls back to must not diverge.

    The two tests above hold the chart together internally — probe against rendered config — and
    would stay green with both numbers wrong in the same direction. This one is the other axis: a
    developer reading the code default and an operator reading `values.yaml` have to be looking at
    the same system — the shape the worker drain's own budget test already holds.
    """
    from chemclaw.core.config import settings

    config = _values()["config"]
    assert float(config["CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS"]) == float(
        settings.connector_health_timeout_seconds
    )
    assert float(config["CHEMCLAW_SERVICE_READINESS_DB_TIMEOUT_SECONDS"]) == float(
        settings.service_readiness_db_timeout_seconds
    )


def test_a_connector_pod_drains_before_it_dies() -> None:
    """The half of D-121's drain the connector pods never got.

    The front door has a `preStop` sleep and a derived grace period and the workers have a derived
    one; a connector pod had neither, so it took the 30 s default. That pod is the one holding an
    in-flight MCP tool call *and* an endpoint the front door is still routing to — Kubernetes
    removes the Endpoint and sends SIGTERM concurrently, so without the sleep a rolling update
    refuses calls the caller is still making.
    """
    text = (CHART / "templates" / "deployment-connectors.yaml").read_text()
    assert "preStop:" in text, "a connector pod stops accepting while the front door still dials it"
    assert 'include "chemclaw.connectorGracePeriod"' in text, (
        "a connector pod keeps the 30 s default, which SIGKILLs through its own drain"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_connector_pod_outlives_the_call_it_may_be_holding() -> None:
    """The grace period and the calc client's own bounds were two independent numbers.

    `connectorGracePeriodSeconds: 120` shipped beside `calc_server_timeout_seconds: 900` and
    `calc_atomic_timeout_seconds: 3600`, with a comment arguing that the heavy science "is not in
    process — it is `Chemclaw3-mcp`'s `servers/calc`, dialled over HTTP". That is the right
    description of the wait and the wrong conclusion about the number: the HTTP call *is* the
    in-process wait, so a synchronous `optimize_geometry` or `compute_atomic_descriptors` was
    SIGKILLed at 120 s into a call this repository is prepared to wait 900 s or 3600 s for. Worse
    than losing the answer: `cached_compute` stores a result only once the call returns, so the
    retry recomputed from zero instead of reading the D-011 cache.

    Asserted against `CalculatorSettings`' own defaults rather than against the numbers in
    `values.yaml`, so raising either bound in code fails here instead of silently outgrowing the
    pod's ceiling — which is the drift that produced the pair this test exists for.

    `calc_sampling_timeout_seconds` (14400 s) is excluded deliberately and the exclusion is checked:
    the two CREST searches it bounds are reachable only from `connectors/calc/activities.py`, which
    runs on a Temporal *worker* pod under the chart's workerGracePeriod helper, with the broker's
    retry behind it, never from a connector server's synchronous tool surface.
    """
    from chemclaw.core.config import settings

    result = _render()
    assert result.returncode == 0, result.stderr
    drain = int(_values()["connectorDrainSeconds"])
    grace = {
        document["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]
        for document in yaml.safe_load_all(result.stdout)
        if document
        and document.get("kind") == "Deployment"
        and document["metadata"]["labels"]["app.kubernetes.io/component"].startswith("connector-")
        and not document["metadata"]["labels"]["app.kubernetes.io/component"].startswith(
            "connector-worker-"
        )
    }
    assert grace, "no connector server Deployment rendered, so nothing was checked"
    for bound in (settings.calc_server_timeout_seconds, settings.calc_atomic_timeout_seconds):
        assert min(grace) >= int(bound) + drain, (
            f"a connector pod is SIGKILLed {int(bound) + drain - min(grace)} s into a call this "
            f"repository's own client waits {bound} s for; raise the derived grace period or the "
            "client bound, but they may not disagree"
        )
    tools = (Path("src/chemclaw/connectors/calc/server/tools.py")).read_text()
    assert "calc_sampling_timeout_seconds" not in tools, (
        "a synchronous tool now carries the CREST bound; it is outside the pod's ceiling and the "
        "exclusion in chemclaw.connectorGracePeriod no longer holds"
    )


def test_every_alert_carries_a_runbook_url_that_resolves() -> None:
    """An alert at 03:00 with no link is a name and a sentence.

    The runbook never mentioned a single alert by name (`grep -n "Chemclaw[A-Z]"` returned nothing)
    and no rule carried a `runbook_url`, so the two halves of the on-call story existed and did not
    know about each other. The descriptions were already good; this was a wiring problem.

    Both directions, because either alone rots: a rule whose link points at a heading that was
    renamed, and a heading nobody reaches because its alert lost its annotation.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    runbook = (DEPLOY.parent / "docs" / "guides" / "runbook.md").read_text()
    alerts = re.findall(r"- alert: (\w+)", rule)
    assert alerts, "no alerts found — the extraction is broken, not the rules"
    linked = set(
        re.findall(
            r"runbook_url: \{\{ \.Values\.monitoring\.alerts\.runbookBaseUrl \}\}#([a-z0-9]+)", rule
        )
    )
    unlinked = sorted(a for a in alerts if a.lower() not in linked)
    assert not unlinked, f"alerts with no runbook_url: {unlinked}"
    # GitHub renders `#### ChemclawFoo` as the anchor `#chemclawfoo`, so the alert name *is* the
    # link — no separate mapping to keep in step.
    headings = {h.lower() for h in re.findall(r"^#{2,4} (Chemclaw\w+)\s*$", runbook, re.MULTILINE)}
    missing = sorted(a for a in alerts if a.lower() not in headings)
    assert not missing, f"alerts whose runbook_url points at no heading: {missing}"
    stale = sorted(headings - {a.lower() for a in alerts})
    assert not stale, f"runbook sections for alerts that no longer exist: {stale}"


def test_the_liveness_alerts_read_the_port_the_monitors_actually_scrape() -> None:
    """`ChemclawNoWorkerIsScraped` matches on `endpoint`, which is the PodMonitor's port name.

    Nothing else in this file alerts on a process being *gone* — every other rule reads an
    application counter, and a pod that is not running emits none, which is what a healthy idle
    system also does. `up` and `absent()` are the only two shapes that invert that.

    `kube_pod_status_ready` would say more and is not available: kube-state-metrics is scraped by
    the *platform* Prometheus in `openshift-monitoring`, while a user-workload PrometheusRule is
    evaluated by the user-workload instance, which does not hold those series. A rule written
    against them would be permanently empty — green forever, which reads exactly like "the
    condition never occurred".

    The label the substitute leans on is the operator's, so this pins it against the monitor rather
    than against a memory of what the operator does.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    monitor = (CHART / "templates" / "podmonitor.yaml").read_text()
    assert "absent(up{" in rule, "no rule fires for a pod that never became a target at all"
    assert 'up{namespace="{{ .Release.Namespace }}"' in rule, (
        "the liveness alerts are not scoped to this release's namespace"
    )
    endpoint = re.search(r'endpoint="([a-z-]+)"', rule)
    assert endpoint is not None, "the absent() rule names no scrape endpoint"
    assert f"- port: {endpoint.group(1)}" in monitor, (
        f'the alert matches endpoint="{endpoint.group(1)}" and the PodMonitor scrapes no such port'
    )
    # The expressions, not the file: the comment above the rule explains *why* kube-state-metrics
    # is not read here, and a text search would call that explanation a violation of itself.
    assert "kube_pod_status_ready" not in _alert_expressions(), (
        "a user-workload rule cannot read kube-state-metrics; that series is scraped by the "
        "platform Prometheus and this rule would be empty forever"
    )


def test_the_chart_tells_an_operator_to_turn_user_workload_monitoring_on() -> None:
    """The prerequisite that makes the whole monitoring stack work, documented nowhere.

    On a stock OpenShift cluster user-workload monitoring is off, which makes every ServiceMonitor,
    PodMonitor and PrometheusRule this chart ships an inert custom resource: `oc get servicemonitor`
    lists them, nothing scrapes, no rule loads, and there is no error anywhere. A search across
    `deploy/`, the runbook and `.github/` for `enableUserWorkload` or `cluster-monitoring-config`
    returned zero hits — this is the single highest-probability way the stack ships and does
    nothing.

    Pinned on the exact ConfigMap and key rather than on prose, because "monitoring must be
    enabled" is advice and `openshift-monitoring/cluster-monitoring-config` is an instruction.
    """
    notes = (CHART / "templates" / "NOTES.txt").read_text()
    runbook = (DEPLOY.parent / "docs" / "guides" / "runbook.md").read_text()
    for name, text in (("NOTES.txt", notes), ("the runbook", runbook)):
        assert "cluster-monitoring-config" in text, f"{name} does not name the ConfigMap to edit"
        assert "enableUserWorkload" in text, f"{name} does not name the key to set"
    # The second switch, which decides whether the alerts reach anyone rather than whether the
    # metrics are collected. Both are off by default and they fail in different places.
    assert "enableUserAlertmanagerConfig" in runbook or "enableAlertmanagerConfig" in runbook, (
        "the runbook explains collection and not routing; alerts would fire into the platform "
        "Alertmanager and be dropped"
    )


def test_the_alertmanager_config_refuses_to_route_to_nothing() -> None:
    """The route that closes the second of the three absences the rule file's header names.

    Values-gated because a receiver is a deployment fact — a Slack webhook, a PagerDuty key — and
    *refusing* when enabled without one, by the same rule as the egress and retention postures: an
    object that exists and routes nowhere is worse than no object, because it reads as coverage.
    """
    text = (CHART / "templates" / "alertmanagerconfig.yaml").read_text()
    assert "kind: AlertmanagerConfig" in text
    assert "{{- fail " in text, "enabling the route with no receivers renders a no-op object"
    alertmanager = _values()["monitoring"]["alertmanager"]
    assert alertmanager["enabled"] is False, (
        "the chart ships a routing object built around receivers it cannot know"
    )
    assert alertmanager["receivers"] == [], "the chart ships a receiver it invented"
    assert "severity" in text, "the critical split does not read the label the rules carry"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_enabling_the_route_without_the_rules_refuses_rather_than_rendering_nothing() -> None:
    """Every guard in `alertmanagerconfig.yaml` was unreachable through one door.

    The file's own `{{ if }}` required `monitoring.enabled` *and* `monitoring.alerts.enabled`
    before any of its four `fail`s could run, so a release that turned the routing on while the
    rules were off got no object, no output and no error — measured, `helm template` exited 0 with
    no AlertmanagerConfig in the render. That is
    `D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` with the stakes raised: the switch the
    operator just moved is the one that decides whether an alert reaches a person, and silence
    reads as success.

    Both directions, because a refusal that also refuses the good case is worse than the no-op: the
    shipped defaults must still render, and a release with real receivers must still get its route.
    """
    silent = _render(
        "--set", "monitoring.alerts.enabled=false", "--set", "monitoring.alertmanager.enabled=true"
    )
    assert silent.returncode != 0, (
        "enabling the Alertmanager route with the rules off renders nothing and says nothing"
    )
    assert "monitoring.alertmanager.enabled" in silent.stderr, silent.stderr

    assert _render().returncode == 0, "the shipped defaults stopped rendering"

    routed = _render(
        "--set",
        "monitoring.alertmanager.enabled=true",
        "--set",
        "monitoring.alertmanager.receivers[0].name=oncall",
        "--set",
        "monitoring.alertmanager.receivers[0].webhookConfigs[0].urlSecret.name=am",
        "--set",
        "monitoring.alertmanager.receivers[0].webhookConfigs[0].urlSecret.key=url",
        "--set",
        "monitoring.alertmanager.defaultReceiver=oncall",
    )
    assert routed.returncode == 0, routed.stderr
    assert "kind: AlertmanagerConfig" in routed.stdout, (
        "a release with a declared receiver got no routing object"
    )


def test_the_dashboards_carry_the_label_their_reader_selects_on() -> None:
    """A dashboard is only a dashboard to something that reads it, and the readers disagree.

    The OpenShift console selects `console.openshift.io/dashboard: "true"`; a self-managed Grafana's
    sidecar selects `grafana_dashboard: "1"`. Shipping the JSON with neither would be five files
    nothing ever opens, which is the same "computed and read by nobody" failure the panels exist to
    end.
    """
    import json

    text = (CHART / "templates" / "configmap-dashboards.yaml").read_text()
    assert "console.openshift.io/dashboard" in _values()["monitoring"]["dashboards"]["labels"]
    assert '(.Files.Glob "dashboards/*.json").AsConfig' in text, (
        "the ConfigMap does not carry the dashboard files"
    )
    boards = sorted((CHART / "dashboards").glob("*.json"))
    assert boards, "the dashboards directory is empty"
    for board in boards:
        # Parsed rather than pattern-matched: a dashboard that is not JSON is a ConfigMap key the
        # console silently ignores, and `AsConfig` would embed it happily.
        parsed = json.loads(board.read_text())
        assert parsed["title"] and parsed["panels"], f"{board.name} has no title or no panels"
        for panel in parsed["panels"]:
            assert panel["targets"], f"{board.name}: panel {panel['title']!r} queries nothing"


def test_every_process_role_names_itself_in_its_traces() -> None:
    """All four roles reported `service.name=chemclaw`, so a span could not say who emitted it.

    `core/logging.py` argues for exactly this ("a deployment that wants the front door and each
    worker to appear as separate services sets `OTEL_SERVICE_NAME` per Deployment") and the chart
    set `CHEMCLAW_OTEL_ENABLED`, `_ENDPOINT`, `_LLM_SPANS`, `_INCLUDE_SENSITIVE_DATA` and nothing
    else — so the advice was in the source and unfollowed by the only thing that could follow it.

    The ordering assertion is the one that would fail silently: Kubernetes expands `$(VAR)` only
    against variables declared *earlier in the same container*, so a pod whose attribute string
    precedes `POD_NAME` exports the two literals with no error anywhere.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    body = helpers.split('define "chemclaw.otelResourceEnv"')[1].split("{{- end -}}")[0]
    assert body.index("POD_NAME") < body.index("OTEL_RESOURCE_ATTRIBUTES"), (
        "$(POD_NAME) is referenced before it is declared, so the pod exports the literal"
    )
    for template in (
        "deployment-service.yaml",
        "deployment-workers.yaml",
        "deployment-connectors.yaml",
    ):
        text = (CHART / "templates" / template).read_text()
        components = len(re.findall(r"name: CHEMCLAW_COMPONENT", text))
        tagged = len(re.findall(r'include "chemclaw.otelResourceEnv"', text))
        assert tagged == components, (
            f"{template}: {components} process roles and {tagged} of them name themselves in a "
            "trace; the rest report as the same service"
        )


def test_the_gate_parses_the_promql_rather_than_the_yaml() -> None:
    """`kubeconform` validates that `expr` is a string, not that the string is PromQL.

    So a syntax error passed `make helm-validate`, passed the API server, and was rejected by
    Prometheus at rule-group load — taking the **whole group** with it, silently, with the object
    still reading as `Valid` in the cluster. Nothing else in this repository parses PromQL, and the
    dashboards' panel queries had no gate of any kind.

    Both places are asserted, because a target CI does not run is not a gate.
    """
    makefile = (DEPLOY.parent / "Makefile").read_text()
    workflow = (DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text()
    target = makefile.split("helm-validate:", 1)[1].split("\n\n", 1)[0]
    assert "promtool check rules" in target, "make helm-validate does not parse the PromQL"
    assert "promtool" in workflow, "CI never installs promtool, so the target's check cannot run"
    # The extraction has to reach the dashboards too, or the panels stay unchecked while the rules
    # look covered.
    assert "-dashboards" in makefile, (
        "the gate unwraps the PrometheusRule and not the dashboard ConfigMap"
    )


def test_no_ingress_policy_reaches_another_release_s_pods() -> None:
    """A `podSelector` is namespace-scoped, and `component` alone is not a name this release owns.

    `connector-ingress` selected on `app.kubernetes.io/component` with no release labels, so in a
    namespace holding a second Chemclaw release — a staging copy beside production is the ordinary
    case — it applied to *their* connector pods as well, imposing an ingress rule naming our pods as
    the permitted peer and cutting theirs off from their own front door. Its three sibling policies
    in the same file all carry the `define "chemclaw.selectorLabels"` helper.
    """
    text = (CHART / "templates" / "networkpolicy.yaml").read_text()
    documents = [d for d in text.split("\n---\n") if "kind: NetworkPolicy" in d]
    assert len(documents) >= 3, "the NetworkPolicy split found fewer objects than the chart renders"
    for document in documents:
        name = re.search(r"name: \{\{ include \"chemclaw.name\" \. \}\}-?([a-z-]*)", document)
        selector = document.split("podSelector:", 1)[1].split("policyTypes:", 1)[0]
        assert 'include "chemclaw.selectorLabels"' in selector, (
            f"the {name.group(1) if name else '?'} policy selects pods by component alone, so it "
            "reaches another release's pods in the same namespace"
        )


def test_a_wedged_knowledge_sync_can_be_seen_from_outside_the_pod() -> None:
    """The sidecar catches a failing refresh on purpose, and that made a stuck one invisible.

    `loop` swallows a refresh failure so a dead git remote cannot kill the pod — correct — and the
    consequence was that an expired push credential left the container logging one WARNING per
    interval forever while serving a frozen corpus. No metric, no probe, no alert.
    `ChemclawKnowledgeNotesLost` covers notes going *out*; the graph coming *in* had nothing.

    The real fix is a last-success gauge the reading process exposes (`docs/planning/BACKLOG.md`).
    This is the half that lives in `deploy/`: a heartbeat file on each successful refresh, and a
    liveness probe reading its age, so a stopped loop becomes a restarting container instead of a
    quiet one. Liveness and *not* readiness deliberately — a sidecar's readiness is the pod's, and a
    three-hour-old corpus beats a connection error.
    """
    script = (DEPLOY / "knowledge-sync.sh").read_text()
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert "staleness" in script, "the sync script cannot report how old its last success is"
    assert "heartbeat=" in script and "${target}/" not in script.split("heartbeat=", 1)[1][:80], (
        "the heartbeat lives inside the checkout, which `git clean -fd` empties at the start of "
        "every refresh"
    )
    sidecar = helpers.split('define "chemclaw.knowledgeSidecar"')[1].split("{{- end -}}")[0]
    assert "livenessProbe:" in sidecar, "a wedged sync sidecar still looks healthy"
    assert "readinessProbe:" not in sidecar, (
        "a stale corpus takes the front door out of its Service, which is worse than the staleness"
    )


def test_service_account_does_not_automount_the_api_token() -> None:
    """The ServiceAccount refuses the projected API token no component uses.

    No code under `src/` calls the Kubernetes API, so the token every pod would otherwise mount is
    unused attack surface. The cluster default is to mount it, so the guard must be an explicit
    `false` in values and a rendered field on the ServiceAccount — an omission is the insecure
    posture. Entra workload identity uses a federated token, not this mount, so this is orthogonal
    to identity.
    """
    assert _values()["serviceAccount"]["automountServiceAccountToken"] is False
    config = (CHART / "templates" / "config.yaml").read_text()
    assert "kind: ServiceAccount" in config
    assert (
        "automountServiceAccountToken: {{ .Values.serviceAccount.automountServiceAccountToken }}"
        in config
    )
