"""Structural gate over the Helm chart, the image, and the entrypoint.

`helm template | kubeconform` is `make helm-validate`'s live-edge job; this is the offline half,
catching what breaks a deployment silently and is invisible to `mypy`/`pytest`:

- an `include` naming a `define` that does not exist (renders empty, drops a volume),
- a `.Values.x.y` path missing from `values.yaml` (renders empty),
- an unbalanced `{{ if }}` / `{{ end }}`,
- a `CHEMCLAW_COMPONENT` the entrypoint has no case for (crash loop),
- an image missing a directory the running components read.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from functools import cache
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
    """The reverse direction: no entrypoint case for a component nothing deploys.

    Otherwise a deleted component stays routable and the image keeps dispatching a second live copy
    of its tools. `*` is the unknown-component guard, and the two `<prefix>-*` cases are the generic
    connector dispatch, which by design match names no chart line spells out.
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


# Directories the image reads at runtime that are *data*, not code, so no package discovery finds
# them; a missing one fails silently (no skills, an empty graph, no migration SQL). `tests/` and
# `examples/` are deliberately not shipped.
_RUNTIME_DATA = ("data", "skills", "knowledge", "infra", "schema")


def _copied() -> set[str]:
    """Every path the Containerfile COPYs."""
    containerfile = (DEPLOY / "Containerfile").read_text()
    return set(re.findall(r"^COPY\s+(\S+)\s", containerfile, flags=re.MULTILINE))


def test_image_ships_the_first_party_source_tree() -> None:
    """All first-party code must be in the image.

    `src/` is COPYd whole; `tests/test_packaging.py` separately forbids a first-party package from
    appearing anywhere else, so together they keep the image complete.
    """
    assert "src" in _copied(), "Containerfile never COPYs src/ — the image would ship no code"


def test_image_ships_the_data_directories_read_at_runtime() -> None:
    """The data trees are not code, so nothing about them is caught by an import failure."""
    copied = _copied()
    for required in _RUNTIME_DATA:
        assert required in copied, f"Containerfile never COPYs {required}/"


def test_every_runtime_data_directory_actually_exists() -> None:
    """A COPY of a vanished directory fails the build; one that moved fails silently at start-up.

    So every declared runtime directory must exist: `data/` (every corpus), `skills/` and
    `knowledge/` (layers 3 and 4), `infra/` (this system's SQL) and `schema/` (DDL for stores it
    does not own).
    """
    root = DEPLOY.parent
    for required in _RUNTIME_DATA:
        assert (root / required).is_dir(), (
            f"Containerfile COPYs {required}/ but no such directory exists at the repository root"
        )


def test_the_ignore_file_sits_where_every_builder_that_ships_here_reads_it() -> None:
    """The ignore file must sit at the build-context root, where every supported builder reads it.

    Docker, buildah, podman and kaniko read it from the context root, and every call site passes the
    repository root as the context; only BuildKit honours `<dockerfile>.dockerignore`. Without it
    the whole tree (`.venv`, `.git`, any root `.env` or key) is sent to the daemon or a shared
    kaniko builder — exposure of the context, not a contaminated image, since the `COPY` set is
    explicit.
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

    A substring test on the install line is satisfied by `git` and `github-cli` alike.
    """
    text = (DEPLOY / "Containerfile").read_text()
    return {
        package
        for line in re.findall(r"dnf install -y ([^\n&|]+)", text)
        for package in line.split()
        if not package.startswith("-")
    }


def test_image_installs_the_binaries_the_knowledge_layer_shells_out_to() -> None:
    """The knowledge layer shells out to `git` and `rsync`, so the image must install both.

    A missing `rsync` makes the replica publish fail on every tick, which must never turn into
    deleting the tree the front door reads.
    """
    assert {"git", "rsync"} <= _dnf_installed_packages()


def _git(*args: str, cwd: Path) -> None:
    """One git command in `cwd`, with a committer identity and no interactive prompt."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }
    subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env, capture_output=True)


def _note(note_id: str) -> str:
    """A minimal parseable note body."""
    return f"---\nid: {note_id}\ntype: reaction\ncreated_by: agent\n---\n\nbody\n"


def test_a_locally_recorded_note_survives_the_sidecar(tmp_path: Path) -> None:
    """The sidecar must not delete a note this pod recorded but has not pushed.

    `kg/git_writer.py` commits in the writer's clone, and a push that fails still leaves the note
    committed and readable (`tests/test_knowledge.py`); an `rsync --delete` sync would then remove
    it permanently. Driven end to end against real git repositories, set up as the chart sets them
    up: the local note survives a sync and the remote's notes arrive. The diverged case is its own
    test.
    """
    if not shutil.which("flock"):  # pragma: no cover - present on every Linux CI image
        pytest.skip("flock is not installed")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "knowledge" / "reaction").mkdir(parents=True)
    (seed / "knowledge" / "reaction" / "from-the-remote.md").write_text(_note("from-the-remote"))
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "seed", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)

    note_repo = tmp_path / "note-repo"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(remote), str(note_repo)], check=True
    )
    # What `kg/git_writer.py` leaves behind when the push fails: committed here, not on the remote.
    (note_repo / "knowledge" / "reaction" / "stranded.md").write_text(_note("stranded"))
    _git("add", "-A", cwd=note_repo)
    _git("commit", "-qm", "Add reaction note: stranded", cwd=note_repo)

    result = _sync(tmp_path, remote, note_repo)
    assert result.returncode == 0, result.stderr

    served = {path.name for path in (note_repo / "knowledge" / "reaction").iterdir()}
    assert "stranded.md" in served, (
        "the sidecar deleted a note this pod recorded and had not pushed — the note is still in "
        f"the local HEAD, so nothing will ever restore it. Served: {sorted(served)}"
    )
    assert "from-the-remote.md" in served, "the refresh no longer delivers what the remote holds"


def test_a_diverged_checkout_warns_rather_than_crash_looping_the_pod(tmp_path: Path) -> None:
    """A stranded note *and* a moved remote is a warning, and the pod keeps serving.

    `once` is an init container, so failing on divergence would crash-loop the pod. Resolving it is
    `kg/git_writer.py`'s job (it replays unpushed commits on the next write); this script serves
    what the pod holds, says so, and never silently drops the local note.
    """
    if not shutil.which("flock"):  # pragma: no cover - present on every Linux CI image
        pytest.skip("flock is not installed")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "knowledge" / "reaction").mkdir(parents=True)
    (seed / "knowledge" / "reaction" / "from-the-remote.md").write_text(_note("from-the-remote"))
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "seed", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)

    note_repo = tmp_path / "note-repo"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(remote), str(note_repo)], check=True
    )
    (note_repo / "knowledge" / "reaction" / "stranded.md").write_text(_note("stranded"))
    _git("add", "-A", cwd=note_repo)
    _git("commit", "-qm", "Add reaction note: stranded", cwd=note_repo)
    # Somebody else pushes, so the pod's clone is now genuinely diverged rather than merely behind.
    (seed / "knowledge" / "reaction" / "pushed-elsewhere.md").write_text(_note("pushed-elsewhere"))
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "elsewhere", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)

    result = _sync(tmp_path, remote, note_repo)
    assert result.returncode == 0, f"a divergence crash-loops the pod:\n{result.stdout}"
    assert "WARNING" in result.stdout and "push failed" in result.stdout, result.stdout
    served = {path.name for path in (note_repo / "knowledge" / "reaction").iterdir()}
    assert "stranded.md" in served, f"the local note was dropped: {sorted(served)}"


def _sync(tmp_path: Path, remote: Path, note_repo: Path) -> "subprocess.CompletedProcess[str]":
    """One `knowledge-sync.sh once`, configured the way the chart configures the sidecar."""
    return subprocess.run(
        ["bash", str(DEPLOY / "knowledge-sync.sh"), "once"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CHEMCLAW_KNOWLEDGE_REPO_URL": str(remote),
            "CHEMCLAW_KNOWLEDGE_SYNC_DIR": str(tmp_path / "replica"),
            "CHEMCLAW_NOTE_REPO_DIR": str(note_repo),
            "CHEMCLAW_KNOWLEDGE_DIR": "knowledge",
            "CHEMCLAW_KNOWLEDGE_PUBLISH_DIR": str(note_repo / "knowledge"),
            "GIT_TERMINAL_PROMPT": "0",
        },
    )


def test_the_sync_never_deletes_what_it_is_about_to_replace() -> None:
    """The replica publish must fail loudly rather than empty the directory the app is reading.

    Guards the *shape*: any `rm -rf` of the publish directory is reachable on any rsync failure
    (dead remote, full disk, permissions). This covers the replica path, reached only by a pod with
    no writable clone; the clone path is asserted behaviourally above.
    """
    script = (DEPLOY / "knowledge-sync.sh").read_text()
    destructive = [
        line
        for line in script.splitlines()
        if "rm -rf" in line and "publish_dir" in line and not line.lstrip().startswith("#")
    ]
    assert not destructive, f"knowledge-sync.sh must never rm -rf the published tree: {destructive}"

    publish = script.split("A plain copy (not a symlink)")[1]
    assert "command -v rsync" in publish, "a missing rsync must be detected, not swallowed"
    assert "rsync -a --delete" in publish and "2>/dev/null" not in publish.split("rsync -a")[1], (
        "rsync must run with its stderr visible, or a missing binary looks like a transfer error"
    )


def test_the_image_carries_the_revision_it_was_built_from() -> None:
    """`deployment_revision` must be settable by a build, or every audit record reads `"unknown"`.

    The field ties a past result to the prompt/skill/config version that produced it. Pinned in
    three separately droppable parts: the ARG exists, it reaches the image environment under the
    name the settings prefix reads, and CI passes a value. The image workflow additionally runs the
    built image and compares.
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

    These flags are the chart's only hand-maintained mirror of a manifest. `jobs:` without
    `worker: true` leaves the queue unpolled, so every job waits forever; `server: true` without an
    `endpoint:` crash-loops on a missing module and adds a bogus address. Derived from the
    manifests, so a new bundle is covered the day it is created.
    """
    from chemclaw.connectors.registry import discovered

    entries = _values()["connectors"]
    for name, (_bundle, manifest) in discovered().items():
        cfg = entries[name]
        assert bool(cfg.get("server")) is (manifest.endpoint is not None), name
        assert bool(cfg.get("worker")) is bool(manifest.jobs), name


def test_every_shipped_connector_has_a_chart_entry() -> None:
    """A bundle with no `connectors` entry could never be given pods."""
    from chemclaw.connectors.registry import discovered

    entries = _values()["connectors"]
    for name in discovered():
        assert name in entries, f"connector bundle {name!r} has no entry in values.yaml connectors"
        assert "enabled" in entries[name]
        # A count for every half this release actually pods, and none for a half it does not. A
        # `url:` entry renders no app Deployment or Service (`and $cfg.server (not $cfg.url)`), but
        # its worker half is *not* conditioned on `url` — durable jobs run on our own Temporal queue
        # — so it still needs a count, or `replicas` renders empty and `chemclaw.fleetPools`
        # under-counts its pods.
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
    """A connector's server and worker Deployments are sized by separate knobs.

    One shared `replicas` meant scaling the server for request load also ran extra queue pollers and
    spent `postgres.maxConnections` nobody asked for. Both halves fall back to `replicas`, so the
    common case stays one value. Asserted on template text because this suite has no `helm`.
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

    Such a bundle still carries `server: true` (it mirrors the manifest's `endpoint:`); what it must
    not get is a Deployment against a module that does not exist and a Service selecting no pods.
    Pinned as the *absence of an unguarded* `if $cfg.server`, the edit this prevents; the rendered
    proof is `make helm-validate`.
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

    `_endpoint_url` lets the computed override beat the manifest's URL, and a connector that cannot
    be reached degrades rather than erroring, so the symptom would be a capability quietly missing
    from every turn.
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

    `chemclaw.fleetPools` feeds the ceiling `Settings` refuses to exceed, so an over-count shrinks
    every real pod's pool or crash-loops the release.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    _, _, definition = helpers.partition('define "chemclaw.fleetPools"')
    assert "if and $cfg.server (not $cfg.url)" in definition, (
        "chemclaw.fleetPools counts a server pod for an externally hosted bundle"
    )


def test_a_connector_url_is_only_declared_beside_a_server() -> None:
    """`url` on a bundle with no `server` would silently do nothing.

    `chemclaw.connectorUrls` only visits `enabled && server` entries. Vacuous over the shipped
    values by design; it checks the first entry that sets `url` against the one shape it works in.
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

    The turn guard and attachments are durable, but a running turn's event pump lives in the process
    that started it, so following or stopping it from a sibling pod answers 404. Asserted rather
    than left to the router's default, which could be flipped cluster-wide.
    """
    route = (CHART / "templates" / "service-route.yaml").read_text()
    assert 'haproxy.router.openshift.io/disable_cookies: "false"' in route


def test_push_credential_is_declared() -> None:
    """Every agent-authored note fails at push without a git credential in the chart (DEP-2)."""
    assert "knowledgeRepoToken" in _values()["secrets"]["keys"]


def test_connector_urls_are_computed_from_the_deployed_set() -> None:
    """The address the front door dials must come from the values block that creates the Service.

    A hand-written `CHEMCLAW_CONNECTOR_URLS` could name a connector with no pods or miss one, so the
    ConfigMap includes the helper that ranges over `.Values.connectors` and builds each URL from the
    Service name and `connectorPort`.
    """
    config = (CHART / "templates" / "config.yaml").read_text()
    assert 'CHEMCLAW_CONNECTOR_URLS: {{ include "chemclaw.connectorUrls" . | quote }}' in config
    helper = (CHART / "templates" / "_helpers.tpl").read_text()
    assert 'define "chemclaw.connectorUrls"' in helper
    assert "range $name, $cfg := .Values.connectors" in helper
    assert "$.Values.connectorPort" in helper


def test_disabling_a_connector_takes_its_tools_off_the_agent_too() -> None:
    """`enabled: false` must take the bundle's tools off the agent, not only its pods.

    An unset `CHEMCLAW_CONNECTORS_ENABLED` means *every discovered bundle*, so a disabled bundle
    would still be advertised and its jobs would wait on an unpolled queue. Derived from the
    connectors block, like `CHEMCLAW_CONNECTOR_URLS`, because a second hand-written list goes stale
    invisibly.
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

    An empty `CHEMCLAW_CONNECTORS_ENABLED` means "every bundle the image ships", so a release that
    disables everything would load all of them; it is refused instead.
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

    Harmless at the top of a document, fatal mid-document (`helm lint`: "did not find expected
    key"). A whitespace bug the structural checks cannot see, so: a comment closed with `-}}` must
    not be followed by an indented line.
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
# Checked as strictly as a core kind; listed apart only because the `Makefile` must supply the
# catalog `-schema-location` for them. Each entry's schema was confirmed to exist in the catalog.
_CATALOG_VALIDATED_KINDS = frozenset(
    {"ServiceMonitor", "PodMonitor", "PrometheusRule", "ScaledObject", "TriggerAuthentication"}
)

# The kinds kubeconform genuinely has no schema for, so `make helm-validate` runs with
# `-ignore-missing-schemas` and *skips* them. Keeping the set explicit stops that flag being a hole.
#
# `Route` is OpenShift's, absent from both schema sources; `AlertmanagerConfig` is rendered by the
# union arm and the catalog has no `v1beta1` schema for it. A kind is exempt because of what
# kubeconform can do with it, not because of which arm renders it.
_UNVALIDATED_KINDS = frozenset({"Route", "AlertmanagerConfig"})


def test_only_the_known_crds_are_unvalidated_by_kubeconform() -> None:
    """Pin which kinds the chart renders, so `-ignore-missing-schemas` cannot hide a new one.

    The flag is needed for the OpenShift `Route`; its cost is that an unknown kind is skipped rather
    than rejected. Every rendered kind must be a core kind, a catalog-covered CRD, or a named
    unvalidated kind. How many *resources* each arm skips is a different question, answered by
    `test_every_resource_kubeconform_skips_is_one_this_file_declared`.
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


def _kubeconform_arms() -> list[list[str]]:
    """The flag sets `make helm-validate` actually pipes through kubeconform.

    Read out of the `Makefile`'s own `for flags in …` loop rather than copied, so it cannot stay
    green while the gate's render narrows. Split on whitespace rather than `shlex`, because the
    `--set-json` values carry quotes helm needs.
    """
    makefile = (DEPLOY.parent / "Makefile").read_text()
    loop = next(line for line in makefile.splitlines() if line.lstrip().startswith("for flags in"))
    body = loop.split("for flags in", 1)[1].rsplit("; do", 1)[0]
    return [arm.split() for arm in shlex.split(body)]


@pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("kubeconform") is None,
    # "helm is not installed" verbatim, because that literal is what `tests/conftest.py`'s epilogue
    # counts; worded freshly, this skip was invisible to the count.
    reason="helm is not installed (or kubeconform is): both render and validate the chart",
)
def test_every_resource_kubeconform_skips_is_one_this_file_declared() -> None:
    """Take the skipped count off the tool, for every arm the gate validates.

    `_UNVALIDATED_KINDS` is a claim about what kubeconform does, so the expected number of skipped
    *resources* per arm is derived from the render and compared with kubeconform's own summary line.
    Kinds and resources are different quantities (the union arm renders two `Route`s), so a count of
    the set cannot stand in for it.
    """
    arms = _kubeconform_arms()
    assert len(arms) >= 2, (
        "`make helm-validate` no longer renders more than one arm through kubeconform, so the "
        "off-by-default templates reach it for the first time in an operator's cluster"
    )
    for arm in arms:
        render = _render(*arm).stdout
        declared = [
            f"{document.get('metadata', {}).get('name')} {document['kind']}"
            for document in yaml.safe_load_all(render)
            if document and document.get("kind") in _UNVALIDATED_KINDS
        ]
        result = subprocess.run(
            [
                "kubeconform",
                "-strict",
                "-summary",
                "-ignore-missing-schemas",
                "-kubernetes-version",
                _kube_version(),
                "-schema-location",
                "default",
                "-schema-location",
                "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
                "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
            ],
            input=render,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"kubeconform rejects the render for arm {arm or '(shipped defaults)'}:\n"
            f"{result.stdout}{result.stderr}"
        )
        reported = re.search(r"Skipped:\s*(\d+)", result.stdout)
        assert reported is not None, (
            f"kubeconform printed no `Skipped` count for arm {arm or '(shipped defaults)'}, so "
            f"this test cannot see what the flag hid:\n{result.stdout}"
        )
        assert int(reported.group(1)) == len(declared), (
            f"kubeconform skipped {reported.group(1)} resource(s) on arm "
            f"{arm or '(shipped defaults)'} and this file accounts for {len(declared)} "
            f"({sorted(declared)}). `-ignore-missing-schemas` is hiding a kind — name it in "
            "_UNVALIDATED_KINDS with the reason, or give kubeconform a schema location for it"
        )


def _kube_version() -> str:
    """The Kubernetes version the gate validates against, off the `Makefile`'s own default.

    Restating `1.29.0` here would be the defect this whole test exists to correct, one variable
    over: a second declaration of a number, checked by nothing against the first.
    """
    makefile = (DEPLOY.parent / "Makefile").read_text()
    match = re.search(r"^KUBE_VERSION \?= (\S+)$", makefile, flags=re.MULTILINE)
    assert match is not None, "the Makefile no longer declares KUBE_VERSION"
    return match.group(1)


def test_something_actually_scrapes_the_metrics_endpoint() -> None:
    """`/metrics` must be collected, not merely served.

    Without a ServiceMonitor, PodMonitor or scrape annotation every metric is exposed and
    uncollected, and no dashboard or alert ever has a data point.
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
    """It must select every Service that serves `/metrics`, on the port those Services name.

    By *name* rather than number, so a port change cannot orphan the scrape. Connectors as well as
    the front door: their counters land in a live registry too.
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

    A worker whose poll loop died keeps its process open, so without a probe and a scrape Kubernetes
    reports `Running` and no metric disagrees. Asserted through the shared helper, so a connector
    bundle enabled tomorrow is covered without an edit here.
    """
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert 'define "chemclaw.workerProbes"' in helpers

    def _includes(text: str, helper: str) -> bool:
        """Whether `text` *invokes* the helper, as opposed to mentioning it.

        Matched as a template action anchored to its line, because the worker templates also name
        these helpers in comments; a substring check would be satisfied by the comment.
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
    """The executed half: the path the chart scrapes has to exist on the real app.

    A wrong path renders, validates and collects nothing forever, and the chart cannot know the
    app's routes. `monitoring.path` reaches three kinds of process, so all three real apps are
    checked: the front door, a connector's MCP server, and a worker's probe surface.
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

    The image running as non-root is not enough: Pod Security Admission reads the pod's
    declaration, and a missing one fails only at admission, in someone else's cluster.
    """
    for filename, expected in _POD_SPECS.items():
        text = (CHART / "templates" / filename).read_text()
        found = text.count('include "chemclaw.podSecurityContext"')
        assert found == expected, (
            f"{filename}: {found} pod securityContext blocks, expected {expected}"
        )


def test_every_container_drops_its_capabilities() -> None:
    """The container half of the same profile, on main containers, init containers and sidecars.

    PSA evaluates *every* container, so one undeclared sidecar fails admission for the pod.
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

    A switch for these is a footgun, not a knob. Only `readOnlyRootFilesystem` — not part of the
    restricted profile, and not defaultable while workers shell out to xtb/crest — is configurable.
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

    This ingress rule does *not* make `/metrics` safe: a NetworkPolicy selects peers, not paths, the
    router must be allowed, and the Route publishes every path. The exposition is bounded by the
    declared-label allowlist (`tests/test_metrics.py`) and `route.ipWhitelist`
    (`tests/test_helm_chart.py`).
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    assert "-service-ingress" in policy, "the front door has no ingress NetworkPolicy"
    assert "app.kubernetes.io/component: service" in policy
    assert _values()["networkPolicy"]["serviceIngress"]["enabled"] is True


def test_a_new_listening_port_came_with_the_rule_that_bounds_it() -> None:
    """A listening port on the worker pods must ship with the ingress rule that bounds it.

    The scraper is granted through `monitoringNamespaces`, not `ingressNamespaces`: the front door
    needs the router *and* the scraper, and nothing else should be reachable from the router.
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
    """Grace periods must outlast the turn and the activity drain they interrupt.

    Otherwise every rollout, drain and scale-down SIGKILLs in-flight work: a front-door turn's state
    lives in pod memory, and a worker's activity is re-run only after its timeout. Both grace
    periods are *derived* from the budget they must outlast, so a kubelet timer cannot silently
    override it.
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
    """The chart's grace period must exceed the drain the code takes.

    `durable/serve.py` waits `worker_graceful_shutdown_seconds`; the kubelet SIGKILLs at
    `terminationGracePeriodSeconds`, which must be strictly larger. Executed against the real
    default, the number the worker actually reads.
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

    Anti-affinity and a PDB keep the replicas off one node and out of one eviction, for both roles
    that run more than one pod: the front door (the Route pins a browser to one pod) and the
    background worker (a drain must leave `background-jobs` a poller).
    """
    for template in ("deployment-service.yaml", "deployment-workers.yaml"):
        body = (CHART / "templates" / template).read_text()
        assert "chemclaw.spreadAcrossNodes" in body, f"{template}: replicas may land on one node"

    budget = (CHART / "templates" / "poddisruptionbudget.yaml").read_text()
    # As YAML keys, not as text: this template *discusses* `minAvailable` in the comment explaining
    # why the front door does not use it, and a substring check reads that explanation as the thing
    # it warns against. The front door's is the first document, the worker's the second.
    service_part, worker_part = budget.split("component: service\n{{- end }}", 1)
    service_keys = set(re.findall(r"^\s*(minAvailable|maxUnavailable):", service_part, re.M))
    worker_keys = set(re.findall(r"^\s*(minAvailable|maxUnavailable):", worker_part, re.M))
    assert service_keys == {"maxUnavailable"}, (
        "minAvailable would permit five of six pods to be evicted together once the HPA scales up; "
        "what needs bounding is how many conversations one drain can end"
    )
    assert worker_keys == {"minAvailable"}, (
        "the worker's PDB must say how many pollers a drain has to leave, not how many it may take"
    )
    assert "component: service" in budget, "the disruption budget does not select the front door"
    assert _values()["service"]["disruptionBudget"]["enabled"] is True
    worker_budget = _values()["workers"]["background"]["disruptionBudget"]
    assert worker_budget == {"enabled": True, "minAvailable": 1}
    # A PDB over one pod blocks every node drain; it is rendered only from two replicas.
    assert "gt (int .Values.workers.background.replicas) 1" in worker_part


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_background_worker_renders_two_replicas_and_a_pdb_that_leaves_one() -> None:
    """The render, not the template text: two pods, `minAvailable: 1`, and none from one replica."""

    def render(*extra: str) -> list[dict[str, Any]]:
        out = subprocess.run(
            [
                "helm",
                "template",
                "chemclaw",
                str(CHART),
                "--set",
                "networkPolicy.allowAnyDestination=true",
                "--set",
                "retention.unboundedGrowthAccepted=true",
                "--set",
                "temporal.namespace=chemclaw",
                *extra,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [doc for doc in yaml.safe_load_all(out) if doc]

    def worker_objects(docs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        deployment = next(
            d
            for d in docs
            if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-background-worker")
        )
        budgets = [
            d
            for d in docs
            if d["kind"] == "PodDisruptionBudget"
            and d["metadata"]["name"].endswith("-background-worker")
        ]
        return deployment, (budgets[0] if budgets else None)

    deployment, budget = worker_objects(render())
    assert deployment["spec"]["replicas"] == 2
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert budget is not None, "two background workers render no PodDisruptionBudget"
    assert budget["spec"]["minAvailable"] == 1
    selected = budget["spec"]["selector"]["matchLabels"]
    assert selected == deployment["spec"]["selector"]["matchLabels"]
    spread = deployment["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert spread[0]["topologyKey"] == "kubernetes.io/hostname"

    single, none = worker_objects(render("--set", "workers.background.replicas=1"))
    assert single["spec"]["replicas"] == 1
    assert none is None, "a PDB over one background worker would block every node drain"

    off = render("--set", "workers.background.disruptionBudget.enabled=false")
    _, disabled = worker_objects(off)
    assert disabled is None


def test_the_shipped_fleet_ceiling_matches_the_fleet_the_chart_renders() -> None:
    """The chart may not declare a ceiling its own autoscaling shape exceeds.

    The admission cap is per process, so the shared LLM endpoint sees
    `maxReplicas × uvicorn workers × the cap`. `Settings` refuses a product over the declared
    ceiling, but only for the values a pod was given, so a raised `maxReplicas` would crash-loop
    every front-door pod; this catches it first, with the validator's arithmetic.
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

    # The fleet size must be *derived* from the autoscaling block, not copied into `config:`, where
    # it would go stale the first time someone scales the front door.
    assert "CHEMCLAW_SERVICE_FLEET_REPLICAS" not in values["config"], (
        "the fleet size must be derived from service.autoscaling in templates/config.yaml, not "
        "hand-written as a second copy of maxReplicas"
    )
    config_template = (CHART / "templates" / "config.yaml").read_text()
    assert re.search(
        r"^\s*CHEMCLAW_SERVICE_FLEET_REPLICAS:.*chemclaw\.frontDoorProcesses",
        config_template,
        flags=re.MULTILINE,
    ), "CHEMCLAW_SERVICE_FLEET_REPLICAS does not come from the number the HPA obeys"
    # And that helper is where "the number the HPA obeys" is actually decided, for all three of its
    # callers at once (this ceiling, the Postgres front-door count, the pooled-process total).
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    _, _, front_door = helpers.partition('define "chemclaw.frontDoorProcesses"')
    assert ".Values.service.autoscaling.maxReplicas" in front_door.split("{{- end -}}")[0]


def test_the_autoscaler_scales_on_the_quantity_that_actually_runs_out() -> None:
    """CPU cannot see this service saturate, so an HPA that only watches CPU never scales it.

    A turn is mostly wall clock rather than CPU, so a pod with every admission permit held stays
    near or below a CPU target. The occupancy metric must exist, its target must be *derived* from
    the permit count the pods enforce, it must fire before the ceiling rather than at it, and CPU
    stays as a fallback for clusters without the custom-metrics API.
    """
    values = _values()
    hpa = (CHART / "templates" / "service-route.yaml").read_text()
    occupancy = values["service"]["autoscaling"]["occupancy"]

    assert "type: Pods" in hpa and occupancy["metricName"] == "chemclaw_turns_in_flight", (
        "the HPA does not scale on turns in flight, the work a pod is actually holding"
    )
    assert ".Values.service.autoscaling.occupancy.metricName" in hpa
    assert "CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS" in hpa, (
        "the occupancy target is not derived from the permit count the pods enforce, so the "
        "autoscaler's idea of full and the admission guard's can drift apart"
    )
    # The fallback, and the reason the CPU metric is not simply replaced: an HPA metric the
    # custom-metrics API cannot serve blocks scale-*down* only, so a second metric it can read keeps
    # the autoscaler working (degraded, and visible as FailedGetPodsMetric) instead of frozen.
    assert "name: cpu" in hpa

    permits = int(values["config"]["CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS"])
    target = max(1, permits * int(occupancy["targetPercent"]) // 100)
    assert 0 < target < permits, (
        f"the occupancy target renders {target} against {permits} permits; at or above the cap the "
        "HPA only reacts once turns are already being shed, which is the failure it exists to "
        "prevent"
    )
    # And the honesty this chart owes an operator: a Pods metric needs something in the cluster that
    # this chart does not install, and a chart that silently no-ops is worse than the CPU one.
    prose = (CHART / "values.yaml").read_text()
    assert "prometheus-adapter" in prose and "FailedGetPodsMetric" in prose, (
        "values.yaml does not say that the occupancy metric needs a custom-metrics API, nor what "
        "happens when there is none"
    )


def test_the_capacity_refusal_is_alertable_even_though_it_answers_200() -> None:
    """A platform refusing most of its chemists can report 100% availability.

    A shed is `HTTP 200` plus an SSE `at_capacity` frame, invisible to 5xx alarms and uptime probes.
    So: a *share* alert at `critical` beside the any-shedding one, and a leading indicator over the
    occupancy gauges that fires before anything is refused.
    """
    rules = (CHART / "templates" / "prometheusrule.yaml").read_text()
    severity = dict(re.findall(r"- alert: (\w+)(?:.|\n)*?severity: (\w+)", rules))
    assert severity.get("ChemclawTurnsShedHeavily") == "critical", (
        "the bulk-refusal alert is missing or is not louder than the any-shedding one"
    )
    assert severity.get("ChemclawTurnsShed") == "warning"
    assert severity.get("ChemclawFrontDoorAtItsPermitCeiling") == "warning"
    assert "chemclaw_turns_in_flight) / sum(chemclaw_turn_capacity)" in rules, (
        "the occupancy alert does not read the ratio the autoscaler scales on"
    )
    # The stale claim that started this: three shipped documents said the shed was a 503, so nobody
    # looked for a success-shaped refusal. Wherever the runbook and the rules describe it now, they
    # must not say 503 again.
    shed_block = rules.split("- alert: ChemclawTurnsShed")[1].split("- alert:")[0]
    assert "503" not in shed_block, "the shed alert still describes a 503; it is an HTTP 200"
    assert "HTTP 200" in shed_block


def test_the_fleet_ceiling_has_a_runtime_check_config_validation_cannot_do() -> None:
    """Startup validation sees the rendered shape once; a cluster keeps changing after that.

    `kubectl scale`, an HPA edited in place, or an overlapping rollout push the fleet past its
    ceiling while every pod stays valid, so the ceiling is exported as a gauge and summed live.
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

    Pools, not pods: a front door holds several pools. Kept here rather than read from the template
    so the template is checked against the topology independently.
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
        # No term for the interactive worker: it opens no pool (the helper says why, and
        # `tests/test_queued_tools.py::test_a_queued_call_touches_no_database` holds it).
    return int(total)


def _helper_body(name: str) -> str:
    """One `define` block out of `_helpers.tpl`, for checks a renderless suite can still make."""
    source = (CHART / "templates" / "_helpers.tpl").read_text()
    start = source.index(f'{{{{- define "{name}"')
    # To the next `define`, not the next `end`: the block nests `if`/`range`, so the first `end`
    # closes an inner one and the slice would stop before the arithmetic this reads.
    nxt = source.find("{{- define ", start + 1)
    block = source[start : nxt if nxt != -1 else len(source)]
    # Comments out: these blocks carry their whole argument in prose, dates and measured figures
    # included, and a scan for the arithmetic's own constants must not read them.
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)


def test_the_shipped_connection_ceiling_matches_the_fleet_the_chart_renders() -> None:
    """The chart's own numbers must clear the validator every pod runs at startup.

    `Settings` refuses a pool total over `max_connections`, so a chart whose values exceed it would
    crash-loop every pod. Checked by constructing a real `Settings` from the rendered numbers rather
    than re-implementing the comparison, so the repository has exactly one arithmetic.
    """
    from chemclaw.core.config import Settings

    values = _values()
    pools = _fleet_pools(values)
    per_pool = int(values["config"]["CHEMCLAW_PG_POOL_MAX_SIZE"])
    declared = int(values["postgres"]["maxConnections"])
    # The front-door replica ceiling is a second input to this budget (one `/readyz` pool per front
    # door); omitting it would construct `Settings` at the default of one replica.
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
            # The fourth fleet number the chart renders, and it was omitted. `Settings` refuses a
            # session ceiling declared without a split, so a release setting this non-zero fails to
            # construct in *every* pod — while this test, feeding three of four, stayed green.
            pg_session_fleet_max_connections=int(values["postgres"]["sessionStoreMaxConnections"]),
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
    # Every number the helper adds must come from the topology, not a literal. Unable to render,
    # this suite checks the helper's *shape*: the only bare integer is the pools a front door holds,
    # and every other term is a `.Values` path.
    body = _helper_body("chemclaw.fleetPools")
    literals = {int(n) for n in re.findall(r"\b(\d+)\b", body)}
    assert literals == {3}, (
        f"chemclaw.fleetPools adds bare numbers {sorted(literals)}; only the 3 pools a front door "
        "holds is a constant, and every other term has to come from a .Values path or the declared "
        "count stops meaning the topology. The rendered proof is `make helm-validate`."
    )

    # Derived from the topology, never hand-written beside it — a second copy of the replica counts
    # goes stale the first time a connector is enabled, which is exactly the silent multiplication
    # the ceiling exists to catch, reintroduced by the mechanism meant to catch it.
    assert "CHEMCLAW_PG_FLEET_POOLS" not in values["config"], (
        "the fleet pool count must be derived in templates/_helpers.tpl, not hand-written"
    )
    # And not restated as a count in prose either, where it goes stale against the helper.
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
    # minReplicas is a budget the fleet breaks by scaling up, which is what it is for. Through the
    # one helper that decides it, so this count and CHEMCLAW_SERVICE_FLEET_REPLICAS cannot disagree.
    assert 'include "chemclaw.frontDoorProcesses"' in definition
    _, _, front_door = helpers.partition('define "chemclaw.frontDoorProcesses"')
    assert ".Values.service.autoscaling.maxReplicas" in front_door.split("{{- end -}}")[0]
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


def _alert_expression(rules: str, name: str) -> str:
    """One alert's `expr:` block, out of the un-rendered template text.

    Sliced from `- alert: <name>` to the next `for:`, so assertions on PromQL fragments cannot be
    satisfied by the alert's own `description` or by Helm comments quoting them.
    """
    start = rules.index(f"- alert: {name}")
    return rules[start : rules.index("for:", start)]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_rollout_peak_leaves_a_connection_for_each_held_job_lock() -> None:
    """The declared ceiling covers the pools at a rollout's peak plus the lock connections.

    A single-instance job holds one connection outside the pools for its pass
    (`core/job_lock.py`), and the background workers are the processes that hold one, so the
    unaccounted spend is at most one per replica. Built from the rendered ConfigMap, the numbers
    every pod's `Settings` reads, so the margin is the release's and not the values file's.
    """
    from chemclaw.core.config import Settings

    render = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "retention.unboundedGrowthAccepted=true",
            "--set",
            "temporal.namespace=chemclaw",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    config = next(
        document["data"]
        for document in yaml.safe_load_all(render)
        if document
        and document.get("kind") == "ConfigMap"
        and document["metadata"]["name"] == "chemclaw-config"
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        pg_fleet_pools=int(config["CHEMCLAW_PG_FLEET_POOLS"]),
        pg_fleet_pools_at_rollout_peak=int(config["CHEMCLAW_PG_FLEET_POOLS_AT_ROLLOUT_PEAK"]),
        pg_pool_max_size=int(config["CHEMCLAW_PG_POOL_MAX_SIZE"]),
        service_fleet_replicas=int(config["CHEMCLAW_SERVICE_FLEET_REPLICAS"]),
        service_fleet_replicas_at_rollout_peak=int(
            config["CHEMCLAW_SERVICE_FLEET_REPLICAS_AT_ROLLOUT_PEAK"]
        ),
        pg_fleet_max_connections=int(config["CHEMCLAW_PG_FLEET_MAX_CONNECTIONS"]),
    )
    peak = settings.fleet_connections_per_server(at_rollout_peak=True)[0]
    locks = int(_values()["workers"]["background"]["replicas"])
    declared = int(config["CHEMCLAW_PG_FLEET_MAX_CONNECTIONS"])
    assert peak + locks <= declared, (
        f"the rollout peak is {peak} connections in pools plus {locks} held job-lock connections "
        f"(one per background worker) against a declared ceiling of {declared}; raise "
        "postgres.maxConnections together with the server's max_connections, or lower "
        "CHEMCLAW_PG_POOL_MAX_SIZE"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_shared_document_volume_is_required_before_a_second_worker_mounts_it() -> None:
    """A `ReadWriteOnce` claim would leave the second worker Pending, so the chart will not render.

    The claim is the operator's and the chart cannot read its mode, so it asks. One worker keeps
    working with any claim, and a release with no share is untouched.
    """

    def render(*extra: str) -> subprocess.CompletedProcess[str]:
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
                "--set",
                "temporal.namespace=chemclaw",
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    shared = ("--set", "documentShare.enabled=true")
    assert render().returncode == 0, "a release with no share must render at two workers"
    for refused in ("", "ReadWriteOnce", "ReadWriteOncePod"):
        result = render(*shared, "--set", f"documentShare.accessMode={refused}")
        assert result.returncode != 0 and "documentShare.accessMode" in result.stderr, (
            f"two workers rendered with a {refused or 'blank'} access mode: {result.stderr[-300:]}"
        )
    for accepted in ("ReadWriteMany", "ReadOnlyMany"):
        assert render(*shared, "--set", f"documentShare.accessMode={accepted}").returncode == 0
    single = render(*shared, "--set", "workers.background.replicas=1")
    assert single.returncode == 0, "one worker may mount any claim"


def test_the_connection_ceiling_has_a_runtime_check_config_validation_cannot_do() -> None:
    """The connection ceiling needs a runtime check, as the turn ceiling does.

    Scaling or an overlapping rollout pushes the live sum past the server's ceiling while every pod
    stays valid. The saturation alert on `requests_waiting` is the other half: the reading that
    separates an undersized pool from an unreachable database.
    """
    rules = (CHART / "templates" / "prometheusrule.yaml").read_text()
    assert "ChemclawFleetAboveItsConnectionCeiling" in rules
    # Sliced to the `expr:` block, because the alert's `description` quotes these fragments.
    expr = _alert_expression(rules, "ChemclawFleetAboveItsConnectionCeiling")
    # Each server against its own ceiling, not a sum against a sum, which can miss an over-ceiling
    # server and page a healthy split. The primary's side is the total minus the split store's; with
    # no split the subtrahend is 0.
    assert "sum(chemclaw_pg_pool_max_size)" in expr
    assert "- sum(chemclaw_pg_session_pool_max_size" in expr
    assert "> max(chemclaw_pg_fleet_max_connections)" in expr
    assert "> max(chemclaw_pg_session_fleet_max_connections)" in expr
    # `A - B` is a vector join, so a pod publishing neither gauge emptied the primary comparison
    # and silenced the alert while the fleet was over. Both `sum()`s take the absent case.
    assert expr.count("or vector(0)") == 2, (
        "a bare sum() of the session gauge is a vector join that goes empty when a pod has opened "
        "no pool, which silences the comparison it is part of"
    )
    # Each branch self-disables on *its own* ceiling, so `postgres.maxConnections: 0` ("no ceiling")
    # does not silence a declared `sessionStoreMaxConnections`.
    assert "max(chemclaw_pg_session_fleet_max_connections) > 0" in expr
    assert "max(chemclaw_pg_fleet_max_connections) > 0" in expr
    # The branches must be joined by `or`: with `and` the alert fires only when both servers are
    # over, and every fragment check above would still pass.
    joined = " ".join(expr.split())
    assert ") or ( max(chemclaw_pg_session_fleet_max_connections)" in joined, (
        "the two per-server comparisons are joined by something other than `or`; either one being "
        "over its own ceiling has to fire this, and `and` makes it unfireable"
    )
    assert (
        "max(chemclaw_pg_fleet_max_connections) + max(chemclaw_pg_session_fleet_max_connections)"
        not in expr
    ), "the summed comparison is back; it can only miss (see this test's docstring)"
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


def _evaluate_alerts(
    names: tuple[str, ...], cases: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    """Render the chart and run `promtool test rules` over the named alerts.

    Each case is a promtool `tests` entry (`input_series`, `alert_rule_test`). Rule bodies are
    rebuilt with only `alert`, `expr` and `for`, since a unit test matches annotations exactly.
    """
    render = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "retention.unboundedGrowthAccepted=true",
            "--set",
            "temporal.namespace=chemclaw",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rules = [
        {key: rule[key] for key in ("alert", "expr", "for")}
        for document in yaml.safe_load_all(render)
        if document and document.get("kind") == "PrometheusRule"
        for group in document["spec"]["groups"]
        for rule in group["rules"]
        if rule.get("alert") in names
    ]
    assert {rule["alert"] for rule in rules} == set(names), (
        f"the render no longer carries {names}: {[r['alert'] for r in rules]}"
    )
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        (work / "rules.yaml").write_text(
            yaml.safe_dump({"groups": [{"name": "unit", "rules": rules}]})
        )
        (work / "test.yaml").write_text(
            yaml.safe_dump(
                {"rule_files": ["rules.yaml"], "evaluation_interval": "1m", "tests": cases}
            )
        )
        return subprocess.run(
            ["promtool", "test", "rules", "test.yaml"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )


def _pool_fleet(
    pods: int, *, ceiling: int, width: int = 16, session: int = 0, session_ceiling: int = 0
) -> list[dict[str, str]]:
    """Per-pod gauges for `pods` identical pods, as the app exposes them."""
    series: list[dict[str, str]] = []
    for pod in range(pods):
        for metric, value in (
            ("chemclaw_pg_pool_max_size", width + session),
            ("chemclaw_pg_session_pool_max_size", session),
            ("chemclaw_pg_fleet_max_connections", ceiling),
            ("chemclaw_pg_session_fleet_max_connections", session_ceiling),
        ):
            series.append({"series": f'{metric}{{pod="p{pod}"}}', "values": f"{value}x25"})
    return series


@pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("promtool") is None,
    reason="helm is not installed (or promtool is): both render and evaluate the rule",
)
def test_the_connection_warning_fires_at_the_configured_share_of_each_ceiling() -> None:
    """The warning fires above 80% of a declared ceiling, before the 100% alert, per server.

    Evaluated, not read: 5 pods of 16 are exactly 80 of 100 (quiet), 6 are 96 (the warning only),
    7 are 112 (both). A split session store is judged against its own ceiling, so 90 session
    connections of a declared 100 warn while the primary sits far below its own.
    """
    near, above = "ChemclawFleetNearItsConnectionCeiling", "ChemclawFleetAboveItsConnectionCeiling"

    def expect(alerts: dict[str, bool]) -> list[dict[str, Any]]:
        return [
            {
                "eval_time": "20m",
                "alertname": name,
                "exp_alerts": [{"exp_labels": {}}] if fires else [],
            }
            for name, fires in alerts.items()
        ]

    cases = [
        {
            "interval": "1m",
            "input_series": _pool_fleet(5, ceiling=100),
            "alert_rule_test": expect({near: False, above: False}),
        },
        {
            "interval": "1m",
            "input_series": _pool_fleet(6, ceiling=100),
            "alert_rule_test": expect({near: True, above: False}),
        },
        {
            "interval": "1m",
            "input_series": _pool_fleet(7, ceiling=100),
            "alert_rule_test": expect({near: True, above: True}),
        },
        {
            # One pod holding 16 primary and 90 session connections; the primary ceiling is wide.
            "interval": "1m",
            "input_series": _pool_fleet(1, ceiling=200, session=90, session_ceiling=100),
            "alert_rule_test": expect({near: True, above: False}),
        },
        {
            # No ceiling declared (0) keeps the alert self-disabled however many pools there are.
            "interval": "1m",
            "input_series": _pool_fleet(20, ceiling=0),
            "alert_rule_test": expect({near: False, above: False}),
        },
    ]
    evaluated = _evaluate_alerts((near, above), cases)
    assert evaluated.returncode == 0, f"{evaluated.stdout}{evaluated.stderr}"


def test_the_connection_warning_is_configured_by_a_fraction_below_one() -> None:
    """The share is a chart value below 1; at or above 1 it would be the other alert."""
    fraction = _values()["monitoring"]["alerts"]["connectionsWarningFraction"]
    assert 0 < fraction < 1
    rules = (CHART / "templates" / "prometheusrule.yaml").read_text()
    expr = _alert_expression(rules, "ChemclawFleetNearItsConnectionCeiling")
    assert expr.count(".Values.monitoring.alerts.connectionsWarningFraction") == 2, (
        "each server's comparison must apply the configured share to its own ceiling"
    )
    joined = " ".join(expr.split())
    assert "or vector(0)" in joined and joined.count(") or (") == 1


@pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("promtool") is None,
    reason="helm is not installed (or promtool is): both render and evaluate the rule",
)
def test_an_idle_background_worker_does_not_page_a_current_ingest_source() -> None:
    """With two workers, the one that did not run the last sync reports an ever-growing lag.

    The gauge is the last cursor a *pod* loaded or stored, aged at scrape time, so the idle replica
    reads days behind for a source that is current. The alert takes the freshest reading (`min`);
    it still fires when every replica is behind, which is the stall it exists for.
    """
    budget = _values()["monitoring"]["alerts"]["ingestLagSeconds"]
    name = "ChemclawIngestCursorStalled"

    def pods(active: int, idle: int) -> list[dict[str, str]]:
        return [
            {
                "series": 'chemclaw_ingest_cursor_lag_seconds{pod="active",source="eln"}',
                "values": f"{active}x70",
            },
            {
                "series": 'chemclaw_ingest_cursor_lag_seconds{pod="idle",source="eln"}',
                "values": f"{idle}x70",
            },
        ]

    cases = [
        {
            "interval": "1m",
            "input_series": pods(active=60, idle=budget * 3),
            "alert_rule_test": [{"eval_time": "60m", "alertname": name, "exp_alerts": []}],
        },
        {
            "interval": "1m",
            "input_series": pods(active=budget * 2, idle=budget * 3),
            "alert_rule_test": [
                {
                    "eval_time": "60m",
                    "alertname": name,
                    "exp_alerts": [{"exp_labels": {"source": "eln"}}],
                }
            ],
        },
    ]
    evaluated = _evaluate_alerts((name,), cases)
    assert evaluated.returncode == 0, f"{evaluated.stdout}{evaluated.stderr}"


def test_the_background_worker_is_not_rolled_while_two_versions_could_replay_one_history() -> None:
    """Two replicas make a drain survivable; a rollout must still not overlap two code versions.

    The default `RollingUpdate` starts the new pod before stopping the old one, so two generations
    poll `background-jobs` together. `Recreate` means every unfinished run is resumed by exactly one
    code version, which is what replay requires and is independent of the replica count.
    """
    text = (CHART / "templates" / "deployment-workers.yaml").read_text()
    assert _values()["workers"]["background"]["replicas"] == 2, (
        "the background worker is a single point of failure again; every job it runs is safe under "
        "any replica count (docs/guides/runbook.md, `Background worker replicas`)"
    )
    strategy = re.search(r"^  strategy:\n\s+type: (\w+)", text, flags=re.MULTILINE)
    assert strategy and strategy.group(1) == "Recreate", (
        "the background worker takes the default RollingUpdate, which starts the second "
        "generation before stopping the first — two code versions on `background-jobs`"
    )


def test_the_migration_hook_cannot_hold_a_release_open_forever() -> None:
    """Helm waits for a `pre-upgrade` hook, so a Job with no deadline is an unbounded wait.

    A migration failing on a lock would leave the release in `pending-upgrade`, blocking every later
    upgrade; with a deadline Helm reports it and `docs/guides/runbook.md` documents the way out. Not
    derived, unlike the grace periods: the chart cannot know how long this deployment's slowest
    `CREATE INDEX` takes, so it is a stated default an operator raises.
    """
    job = (CHART / "templates" / "migrate-job.yaml").read_text()
    assert re.search(r"^\s*activeDeadlineSeconds:", job, flags=re.MULTILINE), (
        "the migration hook has no deadline, so a failing migration wedges the release"
    )
    settings_ = _values()["migrateJob"]
    assert settings_["activeDeadlineSeconds"] > settings_["backoffLimit"] * 60, (
        "the deadline leaves no room for the retries the same Job is configured to make"
    )


# What an interpolated `--set` value stands in as, once substituted rather than dropped. A string,
# because the one such flag is a Temporal namespace; a future interpolated flag needing another
# type must be given a representative value here, and fails the render below until it is.
_JENKINS_INTERPOLATED = "jenkins-interpolated"


def _jenkins_render_flags() -> list[str]:
    """Every `--set` the release pipeline's render stage can emit, with all its postures stated.

    Read out of the `Jenkinsfile` rather than copied, so the test proves the pipeline's own flags
    render the chart. `image.digest`/`image.repository` are dropped (no published digest, not a
    posture); an *interpolated* value is substituted with `_JENKINS_INTERPOLATED`, so the render
    exercises a posture the pipeline can state but only resolves inside Jenkins.
    """
    # Split on the stage declarations at their own indentation, not on the bare string: a stage
    # name quoted inside a comment in the body would otherwise truncate the block being read.
    blocks = re.split(r"\n    stage\('", (DEPLOY.parent / "Jenkinsfile").read_text())
    stage = next(block for block in blocks if block.startswith("Render the chart')"))
    flags: list[str] = []
    # The value runs to the next whitespace or Groovy string terminator, so an interpolated
    # `${...}` — and `${a}/${b}` — is *seen* rather than skipped past. That is what makes the two
    # decisions below decisions rather than an accident of the character class.
    for match in re.finditer(r"--set ([A-Za-z0-9_.]+)=([^\s'\"]+)", stage):
        key, value = match.group(1), match.group(2)
        if key.startswith("image."):
            continue
        flags += ["--set", f"{key}={_JENKINS_INTERPOLATED if '$' in value else value}"]
    return flags


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_release_pipeline_can_state_every_posture_the_chart_demands() -> None:
    """The chart refuses to render an unstated posture; the pipeline must be able to state each.

    Egress, retention and `temporal.namespace` each block the render when unstated, and
    `deploy/jenkins/environments/` ships empty, so the pipeline's own flags must be enough. Rendered
    with those flags rather than asserted as strings, because only helm can answer whether they
    suffice; a posture guard added later fails here with the message the operator would get.
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


#: The two "a process is running unguarded" alerts. Both read a per-pod gauge, so both have the
#: same aggregation question and both got the same answer wrong.
_DISARMED_ALERTS = ("ChemclawEgressGuardDisarmed", "ChemclawEgressPreloadDisarmed")


@pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("promtool") is None,
    # "helm is not installed" verbatim, because that literal is what `tests/conftest.py`'s
    # epilogue counts; worded freshly, this skip was invisible to the count.
    reason="helm is not installed (or promtool is): both render and evaluate the rule",
)
def test_a_single_disarmed_pod_is_what_these_alerts_are_for() -> None:
    """`max(...) < 1` over a per-pod gauge cannot fire while any one pod is armed; use `min`.

    Evaluated with `promtool test rules` rather than read, because `promtool check rules` only
    parses. Rule bodies are rebuilt with only `alert` and `expr`, since a unit test matches
    annotations exactly and copying them would duplicate the alert's prose.
    """
    render = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "retention.unboundedGrowthAccepted=true",
            "--set",
            "temporal.namespace=chemclaw",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rules = [
        rule
        for document in yaml.safe_load_all(render)
        if document and document.get("kind") == "PrometheusRule"
        for group in document["spec"]["groups"]
        for rule in group["rules"]
        if rule.get("alert") in _DISARMED_ALERTS
    ]
    assert {rule["alert"] for rule in rules} == set(_DISARMED_ALERTS), (
        f"the render no longer carries both disarmed alerts: {[r.get('alert') for r in rules]}"
    )
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        (work / "rules.yaml").write_text(
            yaml.safe_dump(
                {
                    "groups": [
                        {
                            "name": "disarmed",
                            "rules": [
                                {"alert": rule["alert"], "expr": rule["expr"]} for rule in rules
                            ],
                        }
                    ]
                }
            )
        )
        series = [
            {
                "series": f'{metric}{{pod="armed"}}',
                "values": "1x10",
            }
            for metric in ("chemclaw_egress_guard_armed", "chemclaw_egress_preload_armed")
        ] + [
            {
                "series": f'{metric}{{pod="disarmed"}}',
                "values": "0x10",
            }
            for metric in ("chemclaw_egress_guard_armed", "chemclaw_egress_preload_armed")
        ]
        (work / "test.yaml").write_text(
            yaml.safe_dump(
                {
                    "rule_files": ["rules.yaml"],
                    "evaluation_interval": "1m",
                    "tests": [
                        {
                            "interval": "1m",
                            "input_series": series,
                            "alert_rule_test": [
                                {
                                    "eval_time": "9m",
                                    "alertname": name,
                                    "exp_alerts": [{"exp_labels": {}}],
                                }
                                for name in _DISARMED_ALERTS
                            ],
                        }
                    ],
                }
            )
        )
        evaluated = subprocess.run(
            ["promtool", "test", "rules", "test.yaml"],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )
    assert evaluated.returncode == 0, (
        "one of the disarmed alerts does not fire for a fleet with a single disarmed pod — which "
        f"is the only shape it exists for:\n{evaluated.stdout}{evaluated.stderr}"
    )


def test_no_delivery_script_deploys_this_chart_atomically() -> None:
    """`--atomic` turns the `post-upgrade` convert Job back into a release gate.

    `chemclaw-convert` rewrites `session_messages` into a shape the previous release cannot read, so
    it runs after the upgrade and a rollback after it has run is wrong; `--atomic` rolls back on any
    failed hook, taking a healthy release with it. Scans every file that runs `helm` against the
    chart (delivery scripts, the `Jenkinsfile`, the `Makefile`).
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

# Each case is (values-file body, states the egress posture, states the retention posture). A
# *mentioned* key is not a stated posture: `unboundedGrowthAccepted: false` must not suppress the
# `--set` and leave the deploy to fail inside the chart.
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
        "retention:\n  windows:\n    CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS: 30\n",
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

    Sourced rather than executed (the file's `main` guard allows it); `set +e` afterwards because
    sourcing a `set -euo pipefail` script arms the calling shell, and a helper refusing is one of
    the two answers.
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
    """A posture declined (`false`) must not read as a posture stated.

    With neither opt-in variable set, a helper returns 0 only when the values file states the
    posture. Driven against the real functions, since "matches a key" versus "reads a value" is only
    visible in execution. The chart's own `values.yaml` states neither, which is why every caller
    passes both flags.
    """
    body, states_egress, states_retention = _POSTURE_CASES[case]
    values_file = tmp_path / "values.yaml"
    values_file.write_text(body)
    assert _posture_verdict("egress_flags", values_file) is states_egress
    assert _posture_verdict("retention_flags", values_file) is states_retention


def test_the_release_path_adopts_the_two_objects_the_previous_chart_left_unowned() -> None:
    """The release path must adopt the two objects the previous chart created as unowned hooks.

    `chemclaw-config` and the runtime ServiceAccount were persistent hooks carrying no
    `meta.helm.sh/release-*` annotations, so `helm upgrade` refuses to import them as tracked
    resources. The adoption is `oc annotate` against a live namespace, which no offline test runs;
    this pins that the release path carries the step and the hand-run documents carry the keys.
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

    Connection floods, idle keep-alives and dribbled headers exhaust the process before any request
    reaches the app (`_BodySizeLimit` covers only the body). Each value comes from a setting, and
    the entrypoint's `:-N` fallbacks must equal the `Settings` defaults so neither side drifts.
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

    A tag is mutable, so `helm rollback` would fetch whatever it means now and the revision stamped
    on audit records would stop identifying the bytes. Asserted as "no template builds its own image
    reference", since the failure is a new pod spec interpolating the tag directly.
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
    """Every pod spec must accept `imagePullSecrets`.

    Otherwise a private registry fails to pull, reading as a broken image rather than a missing
    credential; every pod spec, because a half-covered fleet comes up partly.
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
    """The supply-chain controls exist and are *blocking*.

    A non-blocking scanner is one nobody reads. `make deps-audit` is the command CI runs, so a red
    build reproduces locally.
    """
    image_workflow = (DEPLOY.parent / ".github" / "workflows" / "image.yml").read_text()
    assert "make deps-audit" in image_workflow, "no dependency scan in workflow"
    assert "syft" in image_workflow and "upload-artifact" in image_workflow, "no retained SBOM"
    assert "pip-audit" in (DEPLOY.parent / "Makefile").read_text(), (
        "deps-audit target must invoke pip-audit"
    )
    # The *image* scan is deliberately not asserted here: this target audits the locked closure.


def test_the_dependency_audit_gates_every_branch_push_and_the_local_gate() -> None:
    """The dependency audit must gate every branch push and `make ci`, not only `image.yml`.

    `image.yml` runs on `main` and pull requests only, so branch pushes and the local gate would go
    green against a vulnerable lockfile. Each wiring is one word in a list, easily dropped.
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


#: Binaries a `shutil.which(...)` skip guard may rely on without a CI install step, because the
#: runner image guarantees them (`flock` is util-linux). Each entry is a claim about the image.
_RUNNER_IMAGE_BINARIES = frozenset({"bash", "flock", "git", "make"})


def test_every_binary_the_suite_skips_on_is_installed_where_the_suite_runs() -> None:
    """A `skipif(shutil.which(...))` is a promise that CI has the binary.

    A missing binary is a skip, which reports green, so a skip guard is worth exactly what the CI
    job running the suite installs. Every binary a guard names must be installed by that job or be
    on the runner-image allowlist, so the next binary-gated test fails here instead of skipping
    quietly. Both directions, because an allowlist nobody prunes is the same defect.
    """
    sources = "\n".join(
        path.read_text() for path in sorted((DEPLOY.parent / "tests").rglob("*.py"))
    )
    gated = set(re.findall(r'shutil\.which\(\s*"([a-z0-9_-]+)"', sources))
    assert "helm" in gated, f"the skip-guard scan did not parse: {sorted(gated)}"

    jobs: dict[str, Any] = yaml.safe_load(
        (DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text()
    )["jobs"]
    suite_jobs = [
        name
        for name, job in jobs.items()
        if any(
            target in {"cov", "test"}
            for step in job.get("steps", [])
            for command in re.findall(r"^make (.+)", str(step.get("run", "")), re.MULTILINE)
            for target in command.split()
        )
    ]
    assert len(suite_jobs) == 1, (
        f"{suite_jobs} run the suite; this test assumes one job does, and two would mean a binary "
        "installed in one of them still leaves the other's run skipping"
    )
    installed = "\n".join(
        str(step)
        for step in jobs[suite_jobs[0]]["steps"]
        if str(step.get("name", "")).startswith("Install")
    )

    unprovided = sorted(
        binary for binary in gated - _RUNNER_IMAGE_BINARIES if binary not in installed
    )
    assert not unprovided, (
        f"tests skip on {unprovided} and the `{suite_jobs[0]}` job installs none of them, so those "
        "tests report green in CI without ever running. Add an install step, or add the binary to "
        "_RUNNER_IMAGE_BINARIES with the reason the runner image guarantees it"
    )
    stale = sorted(_RUNNER_IMAGE_BINARIES - gated)
    assert not stale, (
        f"_RUNNER_IMAGE_BINARIES exempts {stale}, which no test in this suite gates on any more"
    )

    # The exemption has to be earned, or it is the hole. The runner image's guarantees are
    # unverifiable here, but a binary this repository installs somewhere or tells a human to install
    # is known not to be guaranteed, so the two lists must be disjoint.
    install_steps = "\n".join(
        str(step)
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("name", "")).startswith("Install")
    )
    # The binary each step *installs*, not every word it mentions: matched on the `install -m` that
    # puts it on `PATH` and on the `setup-<tool>` action. A substring scan over the step text read
    # `git` out of the `github.com` in a download URL — a guard that fires on its own plumbing.
    installed_anywhere = set(re.findall(r"install -m \d+ \S*?/([a-z0-9_-]+)\b", install_steps))
    installed_anywhere |= set(re.findall(r"uses: \S+/setup-([a-z0-9-]+)@", install_steps))
    runbook = (DEPLOY.parent / "docs" / "guides" / "runbook.md").read_text()
    runbook_block = runbook.split('says a binary is "not installed', 1)[1].split("```")[1]
    told_to_install = {line.split()[0] for line in runbook_block.splitlines() if line.strip()}
    assert {"helm", "kubeconform", "promtool"} <= installed_anywhere | told_to_install, (
        "neither the workflow's install steps nor the runbook's install block parsed; this check "
        f"saw {sorted(installed_anywhere)} and {sorted(told_to_install)}"
    )
    contradicted = sorted(_RUNNER_IMAGE_BINARIES & (installed_anywhere | told_to_install))
    assert not contradicted, (
        f"_RUNNER_IMAGE_BINARIES claims the runner image guarantees {contradicted}, and this "
        "repository installs them or tells a human to — so it does not believe its own exemption. "
        "Install the binary in the suite's job instead of exempting it"
    )


def test_every_gate_make_ci_runs_is_a_step_ci_yml_runs() -> None:
    """Every gate `make ci` runs is a step `ci.yml` runs, and vice versa.

    "A green `make` locally means a green CI" rests on two hand-maintained lists agreeing; this
    closes the class rather than an instance. `helm-validate` deliberately runs in its own `chart`
    job, and that split is asserted, since a gate moving between jobs changes what blocks a merge.
    The jobs are parsed rather than sliced as text, so adding a job cannot make the check vacuous.
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
    # `db-migrate` is the one workflow target that is not a gate: it builds the database the
    # Postgres-backed tests run against. Named rather than pattern-matched, so a second non-gate
    # step has to be argued for here.
    setup = {"db-migrate"}
    extra = [target for target in everywhere if target not in set(gates) | setup]
    assert not extra, (
        f"ci.yml runs {extra} and `make ci` does not, so CI gates on something the documented "
        "pre-push gate never checks — the same drift running the other way"
    )


def test_the_default_branch_is_never_cancelled_mid_gate() -> None:
    """A cancelled run on `main` is not a superseded answer, it is a missing one.

    Cancelling the older run is right on a topic branch; on the default branch the runs are about
    different commits and nothing re-runs the cancelled one, so a commit on `main` could have no
    completed gate. Asserted as the expression, so it cannot be reverted to a bare `true`.
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
    """Every action is pinned to a commit, not a mutable tag.

    A retagged or compromised action runs with the workflow token in the job that builds the shipped
    image. The readable version stays as a trailing `# vX.Y.Z` comment, the form Dependabot
    rewrites (`.github/dependabot.yml` has a `github-actions` entry).
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
    """The two properties of `mutants.yml` whose loss is silent.

    The schedule is what makes the mutation control run at all. The Postgres service matters because
    Postgres-backed tests skip without it, and every mutant in the modules they cover would then be
    scored SURVIVED for a reason unrelated to the mutation.
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
    """Every downloaded binary is checksummed before it runs.

    A release asset is mutable and these execute on the runner (`kubeconform`, the `syft`
    installer, which piped into `sh` would also run a truncated download). The check is that each
    `curl` is accompanied by a `sha256sum -c`, not that a digest is current.
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

    Stubbed because a found vulnerability and an unreachable advisory database are distinguished by
    `pip-audit`'s *output*, and its cache makes a real offline run nondeterministic. `stale_log`
    makes `tee` fail while stale text sits at the old log path, which nothing under test may
    consult.
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

    `pip-audit` exits 1 both on a finding and on an unreachable database, so the output is
    classified rather than the status.
    """
    result = _run_deps_audit(tmp_path, _UNREACHABLE, 1, ci=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED" in result.stdout, result.stdout
    assert "NOT audited" in result.stdout, "an unaudited lockfile must say so, loudly"


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_an_unreachable_advisory_database_fails_in_ci(tmp_path: Path) -> None:
    """The other half, and the half that makes the tolerance safe.

    In CI the network is a given, so "unreachable" must fail; tolerating it would be a supply-chain
    hole that reads green forever.
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
    """The audit classifies what the command said, not what a file holds.

    If the classification read a log file written by `tee`, a `tee` that could not write would leave
    stale bytes to be classified, e.g. a vulnerable lockfile reported as an outage. The behavioural
    half hands the run every such ingredient; the structural half requires the classification's
    input to be the captured output, so this cannot pass vacuously.
    """
    result = _run_deps_audit(tmp_path, _FOUND, 1, ci=None, stale_log=_UNREACHABLE)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "SKIPPED" not in result.stdout, result.stdout
    assert "Found 2 known vulnerabilities" in result.stdout, (
        "the operator never even saw the finding"
    )
    after = (DEPLOY.parent / "Makefile").read_text().split("\ndeps-audit:")[1]
    recipe = re.split(r"\n[a-z][a-z0-9-]*:", after, maxsplit=1)[0]
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
    """No calculation binary ships in this image.

    `xtb` and `crest` are invoked by `Chemclaw3-mcp`, not here, so shipping them would add size to
    every pod and take a redistribution (LGPL/GPL) decision for programs this product does not run.
    Asserted as an absence, so re-adding one has to argue for itself.
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

    A source scan for host literals catches nothing at runtime. Destinations are deployment-specific
    and cannot be defaulted, so the chart makes the choice declarable and visible.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    assert ".Values.networkPolicy.egressDestinations" in policy
    assert _values()["networkPolicy"]["egressDestinations"] == []


def test_the_destination_list_says_which_layer_it_is_the_only_one_of() -> None:
    """An operator sizing this list has to know which shapes it is the whole control for.

    The in-process guard patches `socket.socket`, so it bounds no gRPC or Temporal traffic, and a
    loopback sidecar shares the pod's network namespace, so no entry can see mesh or egress-gateway
    traffic. The `values.yaml` comment must say so. The phrases checked are the claims, so a
    rewording that keeps the meaning stays green.
    """
    prose = (CHART / "values.yaml").read_text()
    _, _, after = prose.partition("egressPorts:")
    block, _, _ = after.partition("egressDestinations:")
    for claim in (
        "netguard_preload",
        "statically linked",
        "shares the pod's network namespace",
        "refuse_proxied_egress",
    ):
        assert claim in block, (
            f"the egressDestinations comment block does not say {claim!r} — a deployer cannot size "
            "this list without knowing which layer it is the only one of"
        )


def _makefile_renders() -> list[list[str]]:
    """Every `helm template` of this chart in the Makefile, as its whole (continued) command.

    Returned as lines per render, since a backslash-continued block is what carries a flag, so
    callers assert on *each* render rather than a count. Only renders of the shipped defaults: a
    render passing `-f` states that file's postures, which `tests/test_kind_deploy.py` checks.
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
        if not any(" -f " in part for part in block):
            renders.append(block)
    return renders


def test_an_unstated_egress_posture_refuses_to_render() -> None:
    """The chart must not render an egress posture nobody chose.

    An empty destination list is still `to: []`, every destination, behind an object that reads as
    restricted. The render fails unless exactly one of a destination list or
    `allowAnyDestination: true` is stated. Asserted on template text (no `helm` here), with the
    condition kept readable as one line. Every render site pays one `--set` for the default.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    guard = (
        "eq (empty .Values.networkPolicy.egressDestinations)"
        " (empty .Values.networkPolicy.allowAnyDestination)"
    )
    assert guard in policy, "the egress posture can be left unstated"
    assert "{{- fail " in policy, "the guard warns rather than refusing"
    # A quoted boolean is truthy to Go templates and non-empty to `empty`, so `--set-string
    # allowAnyDestination=false` would render allow-any while reading as off; the type guard refuses
    # a string so the emptiness logic only sees a real bool.
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
    """The sibling of the egress guard: an unstated retention posture refuses to render.

    Every retention window defaults to `0` (disabled) by deliberate policy, so a release that never
    states one would grow every durable table forever. The render fails unless exactly one of
    `retention.windows` or `retention.unboundedGrowthAccepted: true` is stated.
    """
    config = (CHART / "templates" / "config.yaml").read_text()
    guard = "eq (empty .Values.retention.windows) (empty .Values.retention.unboundedGrowthAccepted)"
    assert guard in config, "the retention posture can be left unstated"
    assert "{{- fail " in config, "the guard warns rather than refusing"
    # The type guard the egress twin has carried since it shipped and this half did not. `empty` is
    # what misreads a quoted boolean, so the check has to sit *before* the emptiness test, not
    # beside it — see `test_a_quoted_retention_escape_hatch_refuses_to_render` for the measurement.
    assert 'kindIs "string" .Values.retention.unboundedGrowthAccepted' in config, (
        "a quoted unboundedGrowthAccepted (--set-string) satisfies this gate while reading as off"
    )
    assert config.index('kindIs "string" .Values.retention.unboundedGrowthAccepted') < config.index(
        guard
    ), "the type guard runs after the emptiness test, which is the test that misreads the string"
    assert _values()["retention"]["windows"] == {}, (
        "the shipped default states a retention policy the release never wrote down"
    )
    assert _values()["retention"]["unboundedGrowthAccepted"] is False, (
        "the shipped default grants a permission the release never wrote down"
    )
    # Every render must state a retention posture. A disjunction rather than the escape hatch by
    # name, because the guard is an exclusive-or: the `helm-validate` render that parses
    # `ChemclawRetentionNotSweeping` states `retention.windows` instead.
    stated = ("--set retention.unboundedGrowthAccepted=true", "--set retention.windows")
    unflagged = [
        block[0].strip()
        for block in _makefile_renders()
        if not any(flag in line for line in block for flag in stated)
    ]
    assert not unflagged, (
        f"a render states no retention posture, so the chart refuses it: {unflagged}"
    )


def test_dns_egress_survives_narrowing_the_destinations() -> None:
    """DNS is its own rule, so scoping the destinations cannot take name resolution with it.

    Otherwise narrowing `egressDestinations` would stop DNS and present as every dependency being
    unreachable at once.
    """
    policy = (CHART / "templates" / "networkpolicy.yaml").read_text()
    egress = policy.split("policyTypes:")[1].split("---")[0]
    dns_rule, scoped_rule = egress.split("- to:")[1], egress.split("- to:")[2]
    assert "port: 53" in dns_rule and "egressDestinations" not in dns_rule
    assert "egressDestinations" in scoped_rule and "port: 53" not in scoped_rule


def _alert_expressions() -> str:
    """Every rule's PromQL, and nothing else.

    Annotations name metrics in prose, so reading the file as text would count a mentioned metric as
    alerted. `expr` ends at `for`, `labels` or `annotations`: `for` is optional, so ending only at
    `for` would run into the next rule.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    return " ".join(
        re.findall(r"expr:\s*(?:>-\s*)?((?:.|\n)*?)\n\s*(?:for|labels|annotations):", rule)
    )


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


# Counters ending `_failures_total` or `_dropped_total` that deliberately have no alert, with the
# reason. Kept small so the rule below is not satisfied by adding a name. Both are caused by the
# caller with a non-zero steady-state rate (expired tokens, malformed bodies); they are dashboard
# series, and alerting on them would train people to ignore the channel.
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
    """Every counter whose name says it counts a silent failure has an alert or a stated exemption.

    The *registry* is the list, so a counter added tomorrow is covered. `_failures_total` and
    `_dropped_total` are this codebase's suffixes for "something was swallowed", which makes the
    selection mechanical. The test below covers the other direction (a rename leaving its alert
    behind).
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

    `metrics_bridge.degraded` already increments `chemclaw_degraded_total{subsystem=...}`, so a site
    with its own counter puts one event on two series. Derived from the source, like
    `tests/test_degraded.py`.
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
    """One event must not raise two alerts.

    A `degraded()` site that also increments its own alerted counter fires both the umbrella and the
    specific alert, of which only the specific one names the cause. The umbrella's selector excludes
    such subsystems, and this derives that set from the call sites so it cannot become a
    hand-maintained list.
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
    """Severity is monotonic in how final the loss is.

    A publish failure leaves the row `pending` ("still trying"); a dead-lettered result or a
    projection failure will never be retried, so they must not be quieter. Pinned as an ordering
    between named alerts, because the claim is the ordering.
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

    `clamp_min(denominator, …)` is a division-by-zero guard, not a floor, so every ratio must first
    require its denominator to clear an absolute rate. Ratios are detected rather than listed: any
    expression dividing one `rate()`/`increase()` by another, with or without a `sum by (…)`
    grouping, so a new ratio is covered when it is added.
    """
    rules = re.split(r"\n\s*- alert: ", (CHART / "templates" / "prometheusrule.yaml").read_text())
    ratios = []
    for block in rules[1:]:
        name = block.splitlines()[0].strip()
        # Terminated the same way `_alert_expressions` is, and for the same reason: `for:` is only
        # one of the three keys that can follow `expr:`, and a rule without one would otherwise
        # swallow its own labels and annotations into the expression.
        expr = " ".join(
            re.split(r"\n\s*(?:for|labels|annotations):", block.split("expr:")[1])[0].split()
        )
        if re.search(r"(?:rate|increase)\([^)]*\)\)?\s*/\s*", expr):
            ratios.append((name, expr))
    assert ratios, "no ratio alerts found — the extraction is broken, not the rules"
    for name, expr in ratios:
        assert "clamp_min" not in expr, (
            f"{name} still guards its denominator with clamp_min, which converts an idle window "
            "into a large finite ratio instead of no sample"
        )
        assert re.search(r"\band\s+sum(?:\s+by\s*\([^)]*\))?\s*\((?:rate|increase)\(", expr), (
            f"{name} divides two range vectors with no absolute floor on the denominator, so one "
            "event in an idle window is a 100% failure rate"
        )


def test_every_declared_metric_has_a_consumer() -> None:
    """A metric with no panel and no rule is a number nobody has ever seen.

    Asserted against the registry, so the obligation lands on whoever declares the metric, the only
    moment anyone can say what question it answers.
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

    Every identifier is checked, not only `chemclaw_` ones: Temporal SDK series are emitted only
    under `monitoring.temporalSdkMetrics`, so an unconditional panel over them never draws. A
    declared but unbound gauge is caught separately by
    `tests/test_service.py::test_the_per_connector_health_gauge_actually_renders_a_series`. `up` and
    `absent()` are Prometheus's own and stay allowed.
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
    """The metrics designed to alert actually alert.

    Pinned by metric name, so renaming a metric without moving its alert fails. A dashboard panel
    satisfies `test_every_declared_metric_has_a_consumer`, so these need their own check: each is
    the only signal for its failure (a connector answering 500 on `/mcp` with a green `/healthz`,
    tool calls failing, the spend cap biting). Read off the rules' PromQL, not the file, so a
    comment mentioning a series does not count; runbook entries are guarded by
    `test_every_alert_carries_a_runbook_url_that_resolves`.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    assert "kind: PrometheusRule" in rule
    alerted = _series_referenced(_alert_expressions())
    for metric in [
        "chemclaw_audit_sink_failures_total",
        "chemclaw_notes_publish_failures_total",
        "chemclaw_turn_claim_refresh_failures_total",
        "chemclaw_turns_failed_total",
        "chemclaw_turns_shed_total",
        "chemclaw_connectors_unhealthy",
        "chemclaw_db_unavailable_total",
        "chemclaw_tokens_total",
        "chemclaw_connectors_unreachable_total",
        "chemclaw_tool_calls_total",
        "chemclaw_turns_finished_total",
    ]:
        assert metric in alerted, (
            f"{metric} is in no alert *expression* — a mention in a comment or in an "
            "annotation is not an alert"
        )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_no_alert_pages_for_a_pod_that_is_merely_still_starting() -> None:
    """No `critical` alert pages for a pod that is merely still starting.

    `up` is 0 from the moment a pod is a target, through its whole cold start, so
    `ChemclawTargetDown` must outlast the *largest* startup budget the chart grants. Asserted as
    that relation, so raising a budget cannot leave the alert behind.
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
    """Only the fleet group alerts on a series this system does not emit.

    Every other rule reads a series this registry declares; a rule against a series nothing emits
    would be green forever, reading like the condition never occurring. The allowed set is
    enumerated exactly rather than counted.
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
    # `ChemclawWorkerNotPolling` is rendered only under `monitoring.temporalSdkMetrics.enabled`, the
    # flag that renders the exporter's port, so it is absent rather than green forever.
    # `ChemclawNoBackgroundWorkerIsScraped` reads `up` because the shared-endpoint `absent()` cannot
    # see a missing background worker while other pods keep the `metrics` port satisfied.
    assert on_up == {
        "ChemclawTargetDown",
        "ChemclawNoWorkerIsScraped",
        "ChemclawNoBackgroundWorkerIsScraped",
        "ChemclawWorkerNotPolling",
    }, (
        f"the rules that read something other than a first-party series are {sorted(on_up)}; the "
        "file's header says the fleet group plus the flag-gated SDK rule is the whole of that set"
    )


def test_no_alert_asks_for_to_suppress_what_only_a_threshold_can() -> None:
    """`for:` cannot mean "sustained" over an `increase(...) > 0`.

    `increase(c[w]) > 0` stays true for the whole window after one increment, so a shorter `for:`
    only delays the page. The judgement belongs in the count. `for: 0m` on `> 0` is correct for
    alerts that mean the first increment.
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
    """The corpus alert must fire for the empty-tree sentinel, which a `>` comparison never reaches.

    `chemclaw_knowledge_sync_age_seconds` reports `kg/graph.py::NO_NOTES` (-1) for a tree with no
    note at all, the sharper failure. Both arms stay behind the one threshold, because a deployment
    not using the knowledge graph has an empty tree by design; opting out is still one number.
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
    # Histograms are queried through `_bucket`/`_sum`/`_count`, Prometheus's suffixes and not
    # declared names, so they are stripped; gauge families are queried by their bare name.
    referenced = {
        re.sub(r"_(bucket|sum|count)$", "", name)
        for name in re.findall(r"\b(chemclaw_[a-z_]+)\b", expressions)
    }
    assert referenced, "no PromQL expressions were parsed — the extraction is broken, not the rules"
    unknown = referenced - declared
    assert not unknown, f"alerts reference metrics the app never emits: {sorted(unknown)}"


# Supply-chain tooling the runbook's gate section may name — the only vocabulary this check knows.
# A named watch-list rather than every backticked token, since the surrounding prose legitimately
# backticks other things; a tool entering that section belongs here in the same commit.
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


def _invoked_commands(workflow: str) -> set[str]:
    """Every program `image.yml` actually *invokes*, plus each `make` target and each `uses`.

    Parsed as YAML, with shell comments stripped, because a comment is not a control. Reduced to
    command words rather than substrings: downloading a scanner is not running it, and its URL
    contains its name.
    """
    document: Any = yaml.safe_load(workflow)
    commands: set[str] = set()
    for job in (document.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if isinstance(uses, str):
                commands.add(uses)
            run = step.get("run")
            if not isinstance(run, str):
                continue
            script = re.sub(r"(?m)(^|\s)#.*$", r"\1", run).replace("\\\n", " ")
            for fragment in re.split(r"[\n|;&]+|\$\(", script):
                words = fragment.split()
                if not words:
                    continue
                program = words[0].rsplit("/", 1)[-1]
                commands.add(program)
                if program == "make":
                    commands.update(words[1:])
    return commands


def test_every_supply_chain_gate_the_runbook_names_actually_runs() -> None:
    """A documented control that does not run is worse than a missing one.

    Every supply-chain tool the runbook's gate section names — in any table cell or in a sentence —
    must be invoked by `image.yml` (parsed `uses`/`run`, comments stripped), unless the sentence
    carries one of `_ABSENCE_MARKERS`. Keyed on names, not a count: adding a real scan passes by
    making the claim true, re-adding a phantom one fails.
    """
    runbook = (DEPLOY.parent / "docs" / "guides" / "runbook.md").read_text()
    workflow = (DEPLOY.parent / ".github" / "workflows" / "image.yml").read_text()

    section = runbook.split("### When a supply-chain gate goes red", 1)[1].split("\n## ", 1)[0]
    table = [line for line in section.splitlines() if line.startswith("|")]
    assert any(line.startswith("| `") for line in table), (
        "the gate table was not found — this check is reading the wrong section"
    )

    # Every backticked tool in a table row, not just the first cell: a claim in any cell is a claim.
    named = {
        token
        for line in table
        for token in re.findall(r"`([^`]+)`", line)
        if token in _SUPPLY_CHAIN_TOOLS
    }
    named |= {
        match.group(1) for line in table if (match := re.match(r"\|\s*`([^`]+)`", line)) is not None
    }

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

    invoked = _invoked_commands(workflow)
    # `make deps-audit` is how the workflow spells pip-audit; the Makefile target is the real name.
    runs = {
        gate
        for gate in named
        if gate in invoked or gate.replace("pip-audit", "deps-audit") in invoked
    }
    assert named == runs, (
        f"the runbook names supply-chain gate(s) that nothing runs: {sorted(named - runs)}. "
        "Either merge the gate or stop documenting it as one — a comment in the workflow is not "
        "a gate, and neither is a sentence next to the table."
    )

    # A gate that runs is not yet a gate that blocks: a step with `continue-on-error: true` executes
    # and lets the merge through.
    document: Any = yaml.safe_load(workflow)
    non_blocking: list[str] = []
    for job in (document.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            body = f"{step.get('uses') or ''}\n{step.get('run') or ''}"
            if not any(
                gate in body or gate.replace("pip-audit", "deps-audit") in body for gate in named
            ):
                continue
            if step.get("continue-on-error"):
                non_blocking.append(str(step.get("name") or body.strip()[:60]))
    assert not non_blocking, (
        f"the runbook presents these as gates and their steps cannot fail the build: "
        f"{non_blocking}. A non-blocking scanner is a report nobody opens."
    )


# --- Rendered-chart assertions (need the `helm` binary) ----------------------------------------
#
# The source checks above cannot see what a value's *absence* renders to (`int nil` is `0`, a
# missing key is ""), which is where derivations fail silently. Skipped without `helm`;
# `make helm-validate` is the CI half.


@cache
def _render(*overrides: str) -> subprocess.CompletedProcess[str]:
    """`helm template` on the chart, with the egress, retention and namespace postures stated.

    Cached on the overrides because the chart does not change during a session; the returned
    `CompletedProcess` is shared and treated as immutable. `allowAnyDestination=true` and
    `unboundedGrowthAccepted=true` are the flags every caller of the chart passes, and
    `temporal.namespace=chemclaw` names a namespace, which the chart refuses to default because it
    is what separates releases on a shared broker.
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
            "--set",
            "temporal.namespace=chemclaw",
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
    """A derived value must refuse to render when its source `config` key is removed.

    Without `required`, each degrades to a plausible wrong value: a front-door grace period shorter
    than a turn, a worker grace period shorter than its drain, a knowledge path readers never look
    at, or a readiness timeout a budget short. An operator reaches this by moving a key into an
    ExternalSecret or `--set config.<KEY>=null`.
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
            # Derived from the values file, so "all disabled" stays all of them when a bundle is
            # added.
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
    """Both spellings of "no connectors" must refuse.

    An absent connectors block renders `CHEMCLAW_CONNECTORS_ENABLED: ""`, which means *every*
    bundle, and with an empty URL map each falls back to its loopback dev address: tools advertised,
    pods gone. It also renders a `values: null` selector the API rejects but `kubeconform` passes.
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


def _retention_env_names() -> set[str]:
    """Every `CHEMCLAW_RETENTION_*` env name that names a real field, from `Settings` itself.

    Derived rather than listed, because a list here would be a second copy of the chart's list and
    the two only have to *agree* — which is the whole defect this pair of tests exists to catch.
    """
    from chemclaw.core.config import Settings

    return {
        f"CHEMCLAW_{name.upper()}"
        for name in Settings.model_fields
        if name.startswith("retention_")
    }


def _render_windows(*keys: str) -> subprocess.CompletedProcess[str]:
    """`helm template` with `retention.windows` stated, instead of unbounded growth accepted.

    The escape hatch is overridden back to `false` (`--set` is last-wins), leaving exactly the
    posture under test. Stating windows makes the artifact-store and exhibits postures mandatory
    too, so those are accepted here unless the exhibits window is among `keys`.
    """
    exhibits = "CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS"
    accepted = () if exhibits in keys else ("--set", "retention.exhibitsGrowthAccepted=true")
    return _render(
        "--set",
        "retention.unboundedGrowthAccepted=false",
        "--set",
        "retention.artifactGrowthAccepted=true",
        *accepted,
        *(arg for key in keys for arg in ("--set", f"retention.windows.{key}=30")),
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_retention_window_naming_no_setting_refuses_to_render() -> None:
    """A retention window naming no setting refuses to render.

    pydantic-settings ignores an unknown prefixed variable, so a misspelled key would satisfy the
    posture gate, report retention enabled, and leave every window disabled.
    `CHEMCLAW_RETENTION_AUDIT_DAYS` is a realistic misspelling (there is no such field). Driven
    through a real render, because the shipped `values.yaml` leaves `windows` empty and the keys
    arrive at install time.
    """
    typo = _render_windows("CHEMCLAW_RETENTION_AUDIT_DAYS")
    assert typo.returncode != 0, (
        "a window key naming no setting still renders; retention reports on and prunes nothing:\n"
        f"{typo.stdout[:2000]}"
    )
    assert "CHEMCLAW_RETENTION_AUDIT_DAYS" in typo.stderr, typo.stderr

    # Derived, so the chart's list cannot fall behind `Settings`: a retention field added next year
    # is refused by the chart the day it exists, and this arm is what says so.
    settable = sorted(_retention_env_names() - {"CHEMCLAW_RETENTION_ENABLED"})
    assert len(settable) == 10, f"the retention field set moved: {settable}"
    stated = _render_windows(*settable)
    assert stated.returncode == 0, stated.stderr
    for key in settable:
        assert f'{key}: "30"' in stated.stdout, f"the chart drops a real retention setting: {key}"
    assert 'CHEMCLAW_RETENTION_ENABLED: "true"' in stated.stdout, (
        "stating a window no longer derives the enable switch"
    )

    # The switch is derived from the block, so writing it *into* the block renders the key twice
    # and every parser silently keeps the last — a duplicate this chart already refuses elsewhere
    # (`test_no_values_key_is_declared_twice`), reached here through a values file instead.
    by_hand = _render_windows("CHEMCLAW_RETENTION_ENABLED")
    assert by_hand.returncode != 0, by_hand.stdout[:2000]
    assert "CHEMCLAW_RETENTION_ENABLED" in by_hand.stderr, by_hand.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_release_that_does_not_name_its_temporal_namespace_refuses_to_render() -> None:
    """A release that does not name its Temporal namespace refuses to render.

    The broker is cluster-shared and the namespace is the only boundary in it: the task queue and
    the owned Schedule ids are constants, and `_prune` deletes any owned Schedule id this release
    did not plan, so two releases on one namespace overwrite and delete each other's Schedules.
    Driven to the refusal, to a render, and to the duplicate case (the key also written in `config`
    would render twice and the last wins).
    """
    # Built by hand rather than through `_render`, which states all three: `--set` is last-wins and
    # there is no "unset", so `temporal.namespace=` would state the empty string — which is what
    # the gate refuses — and this would then pass for the wrong reason.
    unstated = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "retention.unboundedGrowthAccepted=true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unstated.returncode != 0, (
        "the chart still renders a Temporal namespace nobody chose, so two releases share one "
        f"schedule-id space:\n{unstated.stdout[:2000]}"
    )
    assert "temporal.namespace" in unstated.stderr, unstated.stderr

    stated = _render("--set", "temporal.namespace=chemclaw-staging")
    assert stated.returncode == 0, stated.stderr
    assert 'CHEMCLAW_TEMPORAL_NAMESPACE: "chemclaw-staging"' in stated.stdout, (
        "the stated namespace does not reach the ConfigMap the pods read"
    )

    values = _values()
    assert values["temporal"]["namespace"] == "", (
        "the chart ships a default Temporal namespace again — a default is exactly the thing two "
        "releases would share, which is why this gate has no escape hatch"
    )
    assert "CHEMCLAW_TEMPORAL_NAMESPACE" not in values["config"], (
        "the namespace is back in `config`, where it is a constant rather than a release's choice"
    )

    both = _render("--set", "config.CHEMCLAW_TEMPORAL_NAMESPACE=written-by-hand")
    assert both.returncode != 0, both.stdout[:2000]
    assert "CHEMCLAW_TEMPORAL_NAMESPACE" in both.stderr, both.stderr

    # Every shipped-defaults render in the `Makefile` must carry the flag too, since these defaults
    # cannot render without it.
    renders = _makefile_renders()
    assert renders, "no `helm template` found in the Makefile — the extraction is broken"
    unflagged = [
        block[0].strip()
        for block in renders
        if not any("--set temporal.namespace=" in line for line in block)
    ]
    assert not unflagged, (
        f"a shipped-defaults render is missing the flag it cannot render without: {unflagged}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_quoted_retention_escape_hatch_refuses_to_render() -> None:
    """A quoted retention escape hatch refuses to render.

    A non-empty string is truthy to Go templates and to `empty`, so
    `--set-string retention.unboundedGrowthAccepted=false` would satisfy the gate while disabling
    retention. Driven rather than read, because the text test can see the `kindIs "string"` guard's
    presence but not what `empty` does with a string.
    """
    quoted = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(CHART),
            "--set",
            "networkPolicy.allowAnyDestination=true",
            "--set",
            "temporal.namespace=chemclaw",
            "--set-string",
            "retention.unboundedGrowthAccepted=false",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert quoted.returncode != 0, (
        "a quoted retention escape hatch still renders; the release states a posture it does not "
        f"have and nothing is ever pruned:\n{quoted.stdout[:2000]}"
    )
    assert "must be a boolean" in quoted.stderr, quoted.stderr

    # And the real boolean is untouched: a guard that refused both would be a broken gate, not a
    # strict one, and the shipped defaults plus this flag are what every render site passes.
    stated = _render()
    assert stated.returncode == 0, stated.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_stating_retention_windows_also_requires_an_artifact_store_posture() -> None:
    """Stating retention windows also requires an artifact-store posture.

    `artifact_blobs` is outside the retention windows (its bounds have their own settings and
    sweep), so a release that states windows still needs to state this. Asked only on the `windows`
    arm: `unboundedGrowthAccepted: true` already covers it, and the first arm pins that so the
    shipped defaults stay at two `--set` flags.
    """
    accepted = _render()  # the `unboundedGrowthAccepted` arm, unchanged
    assert accepted.returncode == 0, (
        "accepting unbounded growth now also demands an artifact posture, which makes a third "
        f"`--set` mandatory at every render site for nothing:\n{accepted.stderr}"
    )

    silent = _render(
        "--set",
        "retention.unboundedGrowthAccepted=false",
        "--set",
        "retention.windows.CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS=30",
    )
    assert silent.returncode != 0, (
        "a release states a retention posture and the artifact store still grows unbounded with "
        f"nothing having asked:\n{silent.stdout[:2000]}"
    )
    assert "artifact_blobs" in silent.stderr, silent.stderr

    bounded = _render_windows("CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS")
    assert bounded.returncode == 0, bounded.stderr  # `_render_windows` accepts the growth

    evicting = _render(
        "--set",
        "retention.unboundedGrowthAccepted=false",
        "--set",
        "retention.windows.CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS=30",
        "--set",
        "retention.artifactStore.CHEMCLAW_ARTIFACT_STORE_MAX_BYTES=53687091200",
        "--set",
        "retention.exhibitsGrowthAccepted=true",
    )
    assert evicting.returncode == 0, evicting.stderr
    assert 'CHEMCLAW_ARTIFACT_STORE_MAX_BYTES: "53687091200"' in evicting.stdout, (
        "the stated bound does not reach the ConfigMap, so the sweep is still off"
    )

    # And a key that names no bound refuses, for the reason `retention.windows` checks its own: an
    # unknown prefixed variable is ignored by pydantic-settings, so it would state a posture and
    # evict nothing. The cadence is the sharp case — it is a real setting, and it bounds nothing.
    cadence_only = _render(
        "--set",
        "retention.unboundedGrowthAccepted=false",
        "--set",
        "retention.windows.CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS=30",
        "--set",
        "retention.artifactStore.CHEMCLAW_ARTIFACT_EVICTION_SCHEDULE_MINUTES=60",
    )
    assert cadence_only.returncode != 0, cadence_only.stdout[:2000]
    assert "CHEMCLAW_ARTIFACT_EVICTION_SCHEDULE_MINUTES" in cadence_only.stderr, cadence_only.stderr

    # And the escape hatch must be a real boolean: a quoted one is truthy to Helm and to `empty`.
    quoted = _render(
        "--set",
        "retention.unboundedGrowthAccepted=false",
        "--set",
        "retention.windows.CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS=30",
        "--set-string",
        "retention.artifactGrowthAccepted=false",
    )
    assert quoted.returncode != 0, quoted.stdout[:2000]
    assert "must be a boolean" in quoted.stderr, quoted.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_stating_retention_windows_also_requires_an_artefact_posture() -> None:
    """Windows without the artefacts' window, or an accepted growth, refuse; either one renders.

    `windows` is free-form, so a release could omit `CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS` and
    keep every artefact (and its session) forever. Exactly one, and a real boolean.
    """
    base = (
        "--set",
        "retention.unboundedGrowthAccepted=false",
        "--set",
        "retention.artifactGrowthAccepted=true",
        "--set",
        "retention.windows.CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS=30",
    )
    silent = _render(*base)
    assert silent.returncode != 0, silent.stdout[:2000]
    assert "CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS" in silent.stderr, silent.stderr

    window = ("--set", "retention.windows.CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS=90")
    kept = _render(*base, *window)
    assert kept.returncode == 0, kept.stderr
    assert 'CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS: "90"' in kept.stdout

    accepted = _render(*base, "--set", "retention.exhibitsGrowthAccepted=true")
    assert accepted.returncode == 0, accepted.stderr

    both = _render(*base, *window, "--set", "retention.exhibitsGrowthAccepted=true")
    assert both.returncode != 0 and "Neither is set, or both are" in both.stderr, both.stderr

    quoted = _render(*base, "--set-string", "retention.exhibitsGrowthAccepted=false")
    assert quoted.returncode != 0 and "must be a boolean" in quoted.stderr, quoted.stderr

    # The shipped posture is unchanged: unbounded growth accepted asks nothing further.
    assert _render().returncode == 0
    # And an upgrade that hits the refusal finds it named in the runbook's upgrade steps, quoted as
    # the render prints it — a new refusal nobody is told about reads as a broken chart.
    runbook = (Path(__file__).resolve().parents[1] / "docs/guides/runbook.md").read_text("utf-8")
    refusal = "retention: this release states retention windows and must say how long"
    assert refusal in silent.stderr
    assert "now refuses to render" in runbook and refusal in " ".join(runbook.split())


# What a switch needs *besides itself* to render the branch it gates: `monitoring.alertmanager`
# refuses with no receivers, and `mcpFace.route` renders nothing without its Deployment. A switch
# absent from this map needs nothing.
_SWITCH_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "monitoring.alertmanager.enabled": (
        "--set-json",
        'monitoring.alertmanager.receivers=[{"name":"chemclaw-oncall"}]',
        "--set",
        "monitoring.alertmanager.defaultReceiver=chemclaw-oncall",
    ),
    # The second is a posture: the chart refuses to publish the face until a deployment names who
    # may reach it; stated here as the router's selector, as a real publishing release would.
    # A posture: every background worker mounts the share, so with two the chart asks for the
    # claim's access mode.
    "documentShare.enabled": ("--set", "documentShare.accessMode=ReadWriteMany"),
    "mcpFace.route.enabled": (
        "--set",
        "mcpFace.enabled=true",
        "--set-json",
        'mcpFace.ingressNamespaces=[{"network.openshift.io/policy-group":"ingress"}]',
    ),
}


def _off_by_default_switches() -> list[str]:
    """Every `enabled`/`create` boolean `values.yaml` ships **false** that a template reads.

    Derived, because templates behind these flags are rendered by no default gate until an operator
    turns one on. A switch is a key named `enabled` or `create` (the chart's convention), which
    keeps the two posture flags out; `_render` states those on every call.
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

    That arm is the only place an off-by-default template meets `kubeconform`. Its flag list is read
    out of the `Makefile`, so a copy here cannot stay green while the render narrows.
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
    """Every hook Job Helm waits on carries `activeDeadlineSeconds`.

    Without one a hang holds the release pending indefinitely; `backoffLimit` bounds failures, not
    hangs (temporalio's default RPC timeout is `None`, so a stalled frontend hangs the Schedules
    Job). Checked over rendered manifests, including off-by-default variants, since hook Jobs are a
    property of the render, not of filenames.
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
    """Every HTTP-served container has a startup probe, stated probe bounds and a grace period.

    Otherwise Kubernetes defaults (1 s timeout, three failures, no startup probe, 30 s grace) kill a
    slow cold start and cut drains short. Rendered, so probes from helpers count and new components
    are covered. `exec`-only containers (the knowledge-sync sidecar) serve nothing and are out of
    scope.
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
    """Every pod a Service routes to sleeps in `preStop` before it stops accepting.

    Kubernetes removes the Endpoint and sends SIGTERM concurrently, so without the sleep a router
    keeps choosing a pod that has stopped; a grace period alone does nothing for that. Scoped to
    pods a Service selects, derived from the render: workers' probe ports have no Service.
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

    Hook resources stay out of the release manifest, so this is exactly what `helm rollback`
    restores and `helm uninstall` removes.
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
            "--set",
            "temporal.namespace=chemclaw",
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
    """The configuration the pods read is part of the release, not a hook.

    `helm rollback` does not restore hook resources, so a hooked ConfigMap would keep the new
    release's values behind rolled-back pods. The hook copies the pre-install Job needs take new
    names; the names pods reference are tracked resources. The cost of that move is covered by
    `test_the_pair_the_previous_chart_hooked_is_not_deleted_by_a_rollback_across_the_boundary`.
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
    """A rollback across the hook-to-manifest move must not delete the ConfigMap and ServiceAccount.

    `helm rollback` deletes what the target revision's manifest lacks, and a revision from the
    previous chart lists neither, so the pods it restores would lose their config and identity. The
    `helm.sh/resource-policy: keep` annotation prevents it; a render cannot run a rollback, so this
    pins the one input Helm reads at deletion time. The cost: `helm uninstall` leaves both behind.
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
    """The pre-install hook reads configuration that exists when it runs.

    `pre-install` hooks run before ordinary resources exist, so the migrate Job reads hook-scoped
    copies rendered from the same values; at `pre-upgrade` and `pre-rollback` those copies are also
    the target revision's own values. The post-install/upgrade Jobs read the tracked objects, which
    by then are live.
    """
    hooks = {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(_render().stdout)
        if doc and (doc["metadata"].get("annotations") or {}).get("helm.sh/hook")
    }
    for name, expected in (
        ("chemclaw-migrate", "pre-install,pre-upgrade,pre-rollback"),
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
    migrate_annotations = hooks["chemclaw-migrate"]["metadata"]["annotations"]
    migrate_events = set(migrate_annotations["helm.sh/hook"].split(","))
    for name in (pre_sa, pre_config):
        assert name in hooks, f"the migrate Job reads {name!r}, which is neither a hook nor tracked"
        # Every event the Job runs on, not a subset: the copies are `hook-succeeded`, so an event
        # that runs the Job without them hands it objects that do not exist.
        events = set(hooks[name]["metadata"]["annotations"]["helm.sh/hook"].split(","))
        assert events == migrate_events, (
            f"{name} is a hook on {sorted(events)} but the migrate Job that reads it runs on "
            f"{sorted(migrate_events)}: on {sorted(migrate_events - events)} the Job's pod cannot "
            "start"
        )
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
def test_the_connection_ceiling_covers_the_rollout_peak_and_not_only_the_steady_state() -> None:
    """The declared ceiling must contain what an *upgrade* opens, not what the fleet settles at.

    A rolling update runs both generations, so surging Deployments hold their pools twice. Driven
    through the real `Settings` against a real render, so this asserts the arithmetic the pods run;
    `fleet_connections_per_server` charges each narrow `/readyz` pool one connection.
    """
    values = _values()
    rendered = _render()
    assert rendered.returncode == 0, rendered.stderr
    config = _rendered_config(rendered.stdout)

    from chemclaw.core.config import Settings

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        pg_fleet_pools=int(config["CHEMCLAW_PG_FLEET_POOLS"]),
        pg_fleet_pools_at_rollout_peak=int(config["CHEMCLAW_PG_FLEET_POOLS_AT_ROLLOUT_PEAK"]),
        service_fleet_replicas=int(config["CHEMCLAW_SERVICE_FLEET_REPLICAS"]),
        service_fleet_replicas_at_rollout_peak=int(
            config["CHEMCLAW_SERVICE_FLEET_REPLICAS_AT_ROLLOUT_PEAK"]
        ),
        pg_pool_max_size=int(config["CHEMCLAW_PG_POOL_MAX_SIZE"]),
    )
    steady = settings.fleet_connections_per_server()[0]
    peak = settings.fleet_connections_per_server(at_rollout_peak=True)[0]
    declared = int(values["postgres"]["maxConnections"])

    assert peak > steady, (
        f"the rollout peak ({peak}) is not above the steady state ({steady}); either every "
        "Deployment stopped surging or the peak keys are not reaching the pods, and this test "
        "would then pass for a fleet that never had the exposure it exists to bound"
    )
    assert peak <= declared, (
        f"a rolling update opens {peak} connections against a declared ceiling of {declared}. "
        "Raise postgres.maxConnections to cover the peak, lower CHEMCLAW_PG_POOL_MAX_SIZE, or "
        "lower rollout.maxSurgePods — the startup check refuses this in every pod, so it is a "
        "CrashLoop on first deploy rather than a surprise during the next upgrade"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_every_pool_holding_deployment_surges_by_the_number_the_budget_counts() -> None:
    """The peak arithmetic multiplies by a surge; this is what makes that surge real.

    Every rolling pool-holder renders exactly `rollout.maxSurgePods` (the Kubernetes default of 25%
    rounded up can exceed it); the background worker alone uses `Recreate` and is the one unsurged
    term; and every Deployment that reads this release's config (and so opens a pool) has a term in
    the arithmetic.
    """
    rendered = _render("--set", "mcpFace.enabled=true")
    assert rendered.returncode == 0, rendered.stderr
    surge = int(_values()["rollout"]["maxSurgePods"])

    rolling: dict[str, Any] = {}
    recreate: set[str] = set()
    pooled: set[str] = set()
    for doc in yaml.safe_load_all(rendered.stdout):
        if not doc or doc.get("kind") != "Deployment":
            continue
        strategy = doc["spec"].get("strategy") or {}
        name = doc["metadata"]["name"]
        if strategy.get("type") == "Recreate":
            recreate.add(name)
        else:
            rolling[name] = strategy
        containers = doc["spec"]["template"]["spec"].get("containers") or []
        if any(
            source.get("configMapRef", {}).get("name", "").startswith("chemclaw")
            for container in containers
            for source in container.get("envFrom") or []
        ):
            pooled.add(name)

    assert recreate == {"chemclaw-background-worker"}, (
        f"the roles that never overlap generations are {sorted(recreate)}; the peak arithmetic "
        "leaves exactly the background worker's term unsurged, so any other Recreate makes it "
        "over-count and a background worker that starts rolling makes it under-count"
    )
    assert rolling, "no rolling Deployment rendered — the extraction is broken"
    for name, strategy in sorted(rolling.items()):
        assert strategy.get("rollingUpdate", {}).get("maxSurge") == surge, (
            f"{name} renders {strategy!r}; the peak is counted against "
            f"rollout.maxSurgePods={surge}, and a Deployment that surges by anything else "
            "(Kubernetes defaults to 25% rounded up) peaks above the number the budget checked"
        )

    counted = {
        "chemclaw-service",
        "chemclaw-background-worker",
        "chemclaw-mcp-face",
        *(
            f"chemclaw-connector-{half}{name}"
            for name in _values()["connectors"]
            for half in ("", "worker-")
        ),
        # Counted at zero rather than omitted: it reads the config and opens no pool, which the
        # helper argues and `test_queued_tools.py::test_a_queued_call_touches_no_database` holds.
        *(f"chemclaw-interactive-worker-{name}" for name in _values()["connectors"]),
    }
    assert pooled <= counted, (
        f"{sorted(pooled - counted)} read this release's config — so each pod opens a Postgres "
        "pool — and neither chemclaw.fleetPools nor its peak counts a term for it"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_turning_on_a_pooled_component_moves_the_declared_connection_budget() -> None:
    """Turning on `mcp-face` moves the declared connection budget.

    It opens a Postgres pool per replica, and an undercount cannot trip the startup guard, which
    checks the declared number. Asserted as the difference between two renders, isolating this term
    without copying the helper's arithmetic: one pool per face replica, since it takes no turn.
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
    """One more front-door replica is three more pools.

    A front-door process holds the stores' pool, the `/readyz` probe's and the checkpointer's
    (`tests/test_fleet_pools.py`), and it is the front door that scales. A difference between two
    renders, for the same reason as the face test.
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
def test_a_connector_server_that_declares_its_own_sizing_gets_it_and_no_other_does() -> None:
    """`connectors.<name>.serverResources` reaches that bundle's server pod and only that one.

    Some servers outgrow the shared `resources.connector` budget (`bo` is OOM-killed on start under
    it), and a knob that rendered nothing would leave the crash loop while the values read as fixed.
    """
    result = _render()
    assert result.returncode == 0, result.stderr
    values = _values()
    sized = {
        f"chemclaw-connector-{name}": entry["serverResources"]
        for name, entry in values["connectors"].items()
        if entry.get("enabled") and entry.get("server") and entry.get("serverResources")
    }
    assert "chemclaw-connector-bo" in sized, "bo no longer declares its own server sizing"
    servers = [
        (name, spec)
        for name, spec in _pod_specs(result.stdout)
        if name.startswith("chemclaw-connector-") and "worker" not in name
    ]
    assert servers, "the render has no connector server pods"
    for name, spec in servers:
        expected = sized.get(name, values["resources"]["connector"])
        assert spec["containers"][0]["resources"] == expected, name


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_no_pod_is_handed_its_own_front_door_s_address_as_a_setting(
    overrides: tuple[str, ...],
) -> None:
    """Service links are off on every pod, because this chart's Service name is a setting's prefix.

    Kubernetes injects `<SERVICE>_PORT=tcp://<ip>:<port>`, and `chemclaw-service` yields
    `CHEMCLAW_SERVICE_PORT`, which `Settings.service_port` fails to parse after the first restart.
    Asserted over every pod spec of every variant, since a new template inherits the defect by
    omission.
    """
    result = _render(*overrides)
    assert result.returncode == 0, result.stderr
    specs = _pod_specs(result.stdout)
    assert specs, "the render has no pod specs — this test would assert nothing"
    linked = sorted(name for name, spec in specs if spec.get("enableServiceLinks") is not False)
    assert not linked, f"pods still handed CHEMCLAW_SERVICE_* by service links: {linked}"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_every_mounted_volume_is_a_volume_the_pod_declares(overrides: tuple[str, ...]) -> None:
    """A `volumeMounts` entry naming no volume is rejected at apply, and by nothing before it.

    `kubeconform` validates objects against schemas, and mount-to-volume is a cross-field invariant
    no schema expresses; under `--atomic` it rolls the release back. The defect is a pairing between
    two helpers (`chemclaw.knowledgeMounts` needs `chemclaw.noteRepoVolume`), so every pod spec is
    checked.
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

    `kubeconform` accepts any length, and a shared helper can make every worker invalid at apply at
    once. The character class is checked too, since the same validator enforces both.
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
    """A connector server is not SIGKILLed before it finishes starting.

    With no probe thresholds, Kubernetes defaults kill the container about thirty seconds after
    start, while RDKit imports and a Postgres pool open during lifespan; a cold start on a throttled
    node would crash-loop forever. Pinned as "a startup probe exists and buys more than a minute",
    since the exact budget is a deployment's to tune.
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


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_a_connector_app_is_killed_only_by_a_route_that_consults_nothing(
    overrides: tuple[str, ...],
) -> None:
    """A `connector_app` pod's liveness probe reads `/livez`, which consults nothing.

    On a shared route, the first dependency anyone teaches `/healthz` to check would become a reason
    to kill a pod a restart cannot repair (`/livez` is driven by
    `tests/test_connector_identity.py::test_liveness_is_its_own_route_and_consults_nothing`).
    Containers are found by component in the render, and both paths are checked against the routes
    the app serves.
    """
    from mcp.server.fastmcp import FastMCP

    from chemclaw.connectors.server import connector_app

    app = connector_app(FastMCP("p"), name="p")
    served = {getattr(route, "path", None) for route in app.routes}
    rendered = _render(*overrides)
    assert rendered.returncode == 0, rendered.stderr
    checked: list[str] = []
    for name, spec in _pod_specs(rendered.stdout):
        for container in spec.get("containers") or []:
            env = {e["name"]: e.get("value") for e in container.get("env") or []}
            component = env.get("CHEMCLAW_COMPONENT") or ""
            if not (
                component == "mcp-face"
                or (component.startswith("connector-") and "worker" not in component)
            ):
                continue
            liveness = container["livenessProbe"]["httpGet"]["path"]
            readiness = container["readinessProbe"]["httpGet"]["path"]
            assert liveness == "/livez", f"{name}: liveness probes {liveness}, not /livez"
            assert liveness != readiness, f"{name}: liveness and readiness share {liveness}"
            assert {liveness, readiness} <= served, (
                f"{name} probes {sorted({liveness, readiness} - served)}, which connector_app "
                "does not serve"
            )
            checked.append(name)
    assert checked, "no connector_app container was rendered; the selection is broken"


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

    `/readyz` does budgeted work (a connector sweep, then Postgres), so a 1 s default timeout would
    drain a serving front door during somebody else's outage. Whether the number is large enough is
    checked on rendered values below.
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

    That is the artefact an override (`--set`, an overlay) reaches; the file on disk is not.
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

    `/readyz` may spend the connector health timeout plus the readiness DB timeout; if the probe
    gives up first the kubelet drains a serving front door. Both sides come from one `helm
    template`, so a raised app budget is compared with the probe it actually renders, not with code
    defaults.
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

    9 s exceeds the old literal `timeoutSeconds: 5` on its own, so a template that kept the literal
    fails here.
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
    """The chart's declared readiness budgets equal the `Settings` defaults.

    The two tests above would stay green with both numbers wrong in the same direction; this holds
    the code default and `values.yaml` to the same system.
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
    """A connector pod drains before it dies.

    It holds in-flight MCP calls behind an Endpoint the front door still routes to, and Kubernetes
    removes the Endpoint and sends SIGTERM concurrently, so it needs the `preStop` sleep and a
    derived grace period like the front door.
    """
    text = (CHART / "templates" / "deployment-connectors.yaml").read_text()
    assert "preStop:" in text, "a connector pod stops accepting while the front door still dials it"
    assert 'include "chemclaw.connectorGracePeriod"' in text, (
        "a connector pod keeps the 30 s default, which SIGKILLs through its own drain"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_connector_pod_outlives_the_call_it_may_be_holding() -> None:
    """A connector pod outlives the calc call it may be holding.

    The synchronous HTTP call to `servers/calc` is the in-process wait, so a shorter grace period
    kills a call this repository will wait for, and `cached_compute` stores only on return, so the
    retry recomputes. Asserted against `CalculatorSettings`' defaults so raising a bound in code
    fails here. `calc_sampling_timeout_seconds` is excluded, checked: CREST searches run only on
    Temporal worker pods.
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
    """Every alert carries a `runbook_url` that resolves, and every runbook heading is reached.

    Both directions, because either alone rots: a link to a renamed heading, or a heading whose
    alert lost its annotation.
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

    A gone process emits no counter, so `up` and `absent()` are the only shapes that detect it;
    `kube_pod_status_ready` lives in the platform Prometheus, which user-workload rules cannot read.
    The label is pinned against the monitor rather than memory of the operator's behaviour.
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
    """The chart tells an operator to turn user-workload monitoring on.

    On stock OpenShift it is off, which makes every ServiceMonitor, PodMonitor and PrometheusRule
    here inert with no error anywhere. Pinned on the exact ConfigMap and key
    (`openshift-monitoring/cluster-monitoring-config`), which is an instruction rather than advice.
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
    """The AlertmanagerConfig refuses to route to nothing.

    Values-gated because a receiver is a deployment fact, and refusing when enabled without one,
    like the egress and retention postures: a route to nowhere reads as coverage.
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
    """Enabling the route without the rules refuses rather than rendering nothing.

    Otherwise the template's own `{{ if }}` skips every `fail`, so the switch that decides whether
    an alert reaches a person silently does nothing. Both directions: the shipped defaults still
    render, and real receivers still get their route.
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
    """The dashboards carry the label each reader selects on.

    The OpenShift console selects `console.openshift.io/dashboard: "true"`, a Grafana sidecar
    `grafana_dashboard: "1"`; with neither, nothing opens them.
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
    """Every process role names itself in its traces (`OTEL_SERVICE_NAME` per Deployment).

    The ordering matters: Kubernetes expands `$(VAR)` only against variables declared earlier in the
    same container, so an attribute string before `POD_NAME` exports literals silently.
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

    A syntax error would pass the render gate and the API server, then Prometheus would drop the
    whole rule group at load. The gate parses rule and dashboard queries, and both the `Makefile`
    and CI are asserted, because a target CI does not run is not a gate.
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

    Without the release selector labels, an ingress policy would also apply to a second release's
    pods in the same namespace and cut them off from their own front door.
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
    """A wedged knowledge sync can be seen from outside the pod.

    `loop` swallows refresh failures so a dead remote cannot kill the pod, which makes a stuck loop
    invisible. A heartbeat file on each successful refresh and a liveness probe on its age turn a
    stopped loop into a restarting container. Liveness, not readiness: a sidecar's readiness is the
    pod's, and a stale corpus beats a connection error.
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

    Nothing under `src/` calls the Kubernetes API, and the cluster default mounts the token, so the
    guard must be an explicit `false` in values and on the rendered ServiceAccount. Entra workload
    identity uses a federated token, so this is orthogonal to identity.
    """
    assert _values()["serviceAccount"]["automountServiceAccountToken"] is False
    config = (CHART / "templates" / "config.yaml").read_text()
    assert "kind: ServiceAccount" in config
    assert (
        "automountServiceAccountToken: {{ .Values.serviceAccount.automountServiceAccountToken }}"
        in config
    )


# The `egressPorts` entries that are not a sibling MCP server: this release's own infrastructure,
# whose ports the runbook's connector section has no reason to enumerate either way.
_INFRASTRUCTURE_EGRESS = frozenset({"postgres", "temporal", "https", "llm", "otel"})


def test_the_runbook_connector_section_defers_the_sibling_ports_to_the_chart() -> None:
    """The runbook's connector section defers the sibling ports to the chart.

    A port list in prose drifts from `networkPolicy.egressPorts` and an operator following it leaves
    servers dropped. So the section carries no sibling port literal and points at that one roster.
    """
    ports = _values()["networkPolicy"]["egressPorts"]
    siblings = {name: port for name, port in ports.items() if name not in _INFRASTRUCTURE_EGRESS}
    assert len(siblings) >= 2, f"egressPorts no longer names sibling servers: {ports}"

    runbook = (DEPLOY.parent / "docs" / "guides" / "runbook.md").read_text()
    heading = "## (iv) Add a capability"
    assert heading in runbook, "the connector section has been renamed"
    section = runbook.split(heading, 1)[1].split("\n## ", 1)[0]

    literals = sorted(
        f"{name} ({port})" for name, port in siblings.items() if re.search(rf"\b{port}\b", section)
    )
    assert not literals, (
        f"the connector section states sibling ports {literals}; a port list here goes stale "
        "against `networkPolicy.egressPorts`, and a NetworkPolicy drop is silent — point at "
        "`deploy/helm/chemclaw/values.yaml` rather than repeating its numbers"
    )
    assert "networkPolicy.egressPorts" in section, (
        "the connector section no longer names `networkPolicy.egressPorts`, which is the only "
        "maintained list of which port each sibling server is on"
    )


def test_no_shipped_document_states_a_coverage_floor_other_than_fail_under() -> None:
    """No shipped document states a coverage floor; `pyproject.toml`'s `fail_under` is the one.

    A shipped `.md` or workflow may name the floor but not state a percentage. `docs/archive/`,
    `docs/decisions/` and `tasks/` are excluded, being accurate about the commit they describe.
    """
    import tomllib

    root = DEPLOY.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    fail_under = pyproject["tool"]["coverage"]["report"]["fail_under"]

    tracked = subprocess.run(
        ["git", "ls-files", "*.md", ".github/workflows/*.yml"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    ).stdout.split()
    # A percentage and the word "floor" in one sentence, on a line that is talking about coverage.
    stated = re.compile(
        r"(\d+(?:\.\d+)?)\s*%[^.\n]{0,60}?\bfloor\b|\bfloor\b[^.\n]{0,60}?(\d+(?:\.\d+)?)\s*%"
    )
    about_coverage = re.compile(r"\bcov\b|coverage|fail_under", re.IGNORECASE)

    offenders: list[str] = []
    for relative in tracked:
        if relative.startswith(("docs/archive/", "docs/decisions/", "tasks/")):
            continue
        for number, line in enumerate(
            (root / relative).read_text(encoding="utf-8").splitlines(), 1
        ):
            if not about_coverage.search(line):
                continue
            match = stated.search(line)
            if match and (match.group(1) or match.group(2)) != str(fail_under):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, (
        f"these state a coverage floor that is not `fail_under` ({fail_under}): {offenders}. "
        "Name `fail_under` in `pyproject.toml` rather than repeating a percentage."
    )


def test_no_shipped_document_states_how_many_alerts_the_rule_file_holds() -> None:
    """No shipped document states how many alerts the rule file holds.

    The count grows with every alert and goes stale in prose, understating the alerting surface. The
    group and dashboard counts stay, checked by their own tests.
    """
    rule = (CHART / "templates" / "prometheusrule.yaml").read_text()
    alerts = len(re.findall(r"^\s*- alert:", rule, re.MULTILINE))
    assert alerts > 10, f"prometheusrule.yaml no longer looks like a rule file ({alerts} alerts)"

    numbers = (
        r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
        r"twenty-\w+|thirty-\w+|forty-\w+|fifty-\w+|\d{2,}"
    )
    stated = re.compile(rf"\b({numbers})\s+alerts?\b", re.IGNORECASE)
    offenders: list[str] = []
    for document in (DEPLOY / "README.md", DEPLOY.parent / "docs" / "guides" / "runbook.md"):
        for number, line in enumerate(document.read_text().splitlines(), 1):
            match = stated.search(line)
            if match:
                offenders.append(f"{document.name}:{number}: {match.group(0)!r}")
    assert not offenders, (
        f"these state an alert count against {alerts} in the rendered rule file: {offenders}. "
        "Name `templates/prometheusrule.yaml` instead — it is the roster, and it grows."
    )


# --- The connector seam on the target stack ---------------------------------------------------
#
# A new bundle is one directory plus its name, with zero core edits; these two tests hold that for
# the chart, against a **rendered** manifest, since both failure shapes are "the value is accepted
# and nothing comes out" (`D-2026-09-07-a-seam-that-stops-at-the-chart-is-not-a-seam`).


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_an_egress_port_an_operator_adds_actually_reaches_the_policy() -> None:
    """An egress port an operator adds to `networkPolicy.egressPorts` reaches the rendered policy.

    The rule ranges the map rather than naming keys, so a third-party bundle's own port is emitted;
    a NetworkPolicy drop is silent and reads as an unreachable connector.
    """
    rendered = _render("--set", "networkPolicy.egressPorts.props=8850").stdout
    policies = [
        document
        for document in yaml.safe_load_all(rendered)
        if document and document.get("kind") == "NetworkPolicy" and "egress" in document["spec"]
    ]
    assert policies, "the render produced no egress NetworkPolicy"
    permitted = {
        port["port"]
        for policy in policies
        for rule in policy["spec"]["egress"]
        for port in rule["ports"]
    }
    assert 8850 in permitted, (
        "networkPolicy.egressPorts.props=8850 was accepted and never emitted, so every packet to "
        "that bundle is dropped whatever egressDestinations says. The rule permits "
        f"{sorted(permitted)}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_every_declared_egress_port_reaches_the_rendered_policy() -> None:
    """The other direction of the test above, over the roster this chart ships.

    A port entry no rule emits permits nothing and reads in review as a control; checked on the
    render, not on template text.
    """
    rendered = _render().stdout
    permitted = {
        port["port"]
        for document in yaml.safe_load_all(rendered)
        if document and document.get("kind") == "NetworkPolicy" and "egress" in document["spec"]
        for rule in document["spec"]["egress"]
        for port in rule["ports"]
    }
    ports = _values()["networkPolicy"]["egressPorts"]
    assert ports, "networkPolicy.egressPorts is empty; this test would assert nothing"
    unemitted = sorted(key for key, port in ports.items() if port not in permitted)
    assert not unemitted, (
        f"networkPolicy.egressPorts declares {unemitted}, which the rendered policy does not "
        f"permit. It permits {sorted(permitted)}."
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_bundle_this_image_does_not_ship_can_be_mounted_and_discovered() -> None:
    """A third-party bundle can be mounted onto `CHEMCLAW_CONNECTORS_DIR` and discovered.

    Otherwise naming it in the connectors block puts it in `CHEMCLAW_CONNECTORS_ENABLED` with no
    manifest behind it, and `registry.enabled()` raises in every pod. Asserted over every pod spec,
    since the variable is set once in the shared ConfigMap and any pod lacking the mount
    crash-loops. Exactly two exemptions: the knowledge-sync containers (a shell script, no
    `Settings`) and the `migrate`/`convert` hook Jobs (never import the registry, and a pre-install
    hook cannot mount an operator ConfigMap that does not exist yet).
    """
    rendered = _render(
        "--set",
        "extraConnectors.bundles[0].name=our-eln",
        "--set",
        "extraConnectors.bundles[0].configMap=chemclaw-connector-our-eln",
    ).stdout
    documents = [document for document in yaml.safe_load_all(rendered) if document]

    values = _values()["extraConnectors"]
    expected = f"{values['mountPath']}:{values['contractsPath']}:{values['shippedPath']}"
    configmaps = [
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and "CHEMCLAW_CONNECTORS_DIR" in (document.get("data") or {})
    ]
    assert configmaps, (
        "no rendered ConfigMap sets CHEMCLAW_CONNECTORS_DIR, so the mounted bundle is invisible to "
        "discovery while `CHEMCLAW_CONNECTORS_ENABLED` already names it"
    )
    for configmap in configmaps:
        assert configmap["data"]["CHEMCLAW_CONNECTORS_DIR"] == expected, (
            f"{configmap['metadata']['name']} sets CHEMCLAW_CONNECTORS_DIR to "
            f"{configmap['data']['CHEMCLAW_CONNECTORS_DIR']!r}, not {expected!r}"
        )

    mount = f"{values['mountPath']}/our-eln"
    exempt = {"chemclaw-migrate", "chemclaw-convert"}
    workloads = {
        f"{document['kind']}/{document['metadata']['name']}": spec
        for document in documents
        if (spec := _pod_spec(document)) is not None
    }
    assert workloads, "the render produced no workload to check"

    carried = {
        name
        for name, spec in workloads.items()
        if any(
            volume.get("configMap", {}).get("name") == "chemclaw-connector-our-eln"
            for volume in spec.get("volumes") or []
        )
    }
    skipped = {name for name in workloads if name.split("/", 1)[1] in exempt}
    assert {name.split("/", 1)[1] for name in workloads} & exempt == exempt, (
        f"the exemption names {sorted(exempt)}, and the render has no such workload"
    )
    assert carried == set(workloads) - skipped, (
        "the mounted bundle reaches a set of pods that is not 'everything but the two migration "
        f"hooks': carried by {sorted(carried)}, exempt {sorted(skipped)}, all "
        f"{sorted(workloads)}"
    )

    missing: list[str] = []
    for name in sorted(carried):
        spec = workloads[name]
        for container in spec["containers"] + (spec.get("initContainers") or []):
            command = container.get("command") or []
            if command and command[0].endswith("chemclaw-knowledge-sync"):
                continue
            paths = {m["mountPath"] for m in container.get("volumeMounts") or []}
            if mount not in paths:
                missing.append(f"{name}/{container['name']}: does not mount {mount}")
    assert not missing, (
        "every pod reads CHEMCLAW_CONNECTORS_DIR from the shared ConfigMap, so one that declares "
        "the volume and does not mount it crash-loops on `connectors_enabled names unknown "
        "connector(s)` all the same:\n" + "\n".join(missing)
    )


def _pod_spec(document: dict[str, Any]) -> dict[str, Any] | None:
    """The `PodSpec` of any workload document, or `None` for objects that have none."""
    if document.get("kind") in ("Deployment", "StatefulSet", "DaemonSet"):
        return dict(document["spec"]["template"]["spec"])
    if document.get("kind") == "Job":
        return dict(document["spec"]["template"]["spec"])
    return None


def test_the_shipped_connector_path_is_the_path_the_image_has() -> None:
    """`extraConnectors.shippedPath` restates the image's layout, so it is derived and compared.

    Setting `CHEMCLAW_CONNECTORS_DIR` replaces the default outright, so the chart must name the
    directory the image ships; `_bundle_dirs` skips a missing directory silently, losing every
    shipped bundle. Derived from the Containerfile's `WORKDIR` and `COPY src ./src` (editable
    install) and this package's location in the checkout.
    """
    import chemclaw.connectors

    containerfile = (DEPLOY / "Containerfile").read_text(encoding="utf-8")
    workdir = re.search(r"^WORKDIR (\S+)", containerfile, flags=re.MULTILINE)
    assert workdir, "deploy/Containerfile declares no WORKDIR"
    assert re.search(r"^COPY src \./src$", containerfile, flags=re.MULTILINE), (
        "deploy/Containerfile no longer copies `src` to the workdir, so the path "
        "`extraConnectors.shippedPath` is derived from is no longer how the image is built"
    )
    package = Path(chemclaw.connectors.__file__).resolve().parent
    relative = package.relative_to(DEPLOY.parent)
    expected = f"{workdir.group(1).rstrip('/')}/{relative}"
    assert _values()["extraConnectors"]["shippedPath"] == expected, (
        f"extraConnectors.shippedPath is {_values()['extraConnectors']['shippedPath']!r}; the "
        f"image puts the shipped bundles at {expected!r}"
    )


def test_the_fleet_manifest_path_is_the_path_the_image_has() -> None:
    """`extraConnectors.contractsPath` restates where the image installs `chemclaw-contracts`.

    Setting `CHEMCLAW_CONNECTORS_DIR` replaces the default outright, so a release that mounts a
    bundle must name the fleet's manifests again or lose every fleet connector. The image runs
    `uv sync` in its `WORKDIR`, which builds `.venv` there for the base image's Python; the
    package's own layout under a virtualenv supplies the rest.
    """
    import sys

    import chemclaw_contracts

    containerfile = (DEPLOY / "Containerfile").read_text(encoding="utf-8")
    workdir = re.search(r"^WORKDIR (\S+)", containerfile, flags=re.MULTILINE)
    assert workdir, "deploy/Containerfile declares no WORKDIR"
    base = re.search(r"^ARG BASE_IMAGE=\S*python-(\d)(\d+)\b", containerfile, flags=re.MULTILINE)
    assert base, "deploy/Containerfile's BASE_IMAGE no longer names a python-3NN image"
    assert "UV_PROJECT_ENVIRONMENT" not in containerfile, (
        "the image no longer builds its virtualenv at `.venv` in the workdir, which is where "
        "`extraConnectors.contractsPath` is derived from"
    )
    assert re.search(r"^RUN uv sync --frozen\b", containerfile, flags=re.MULTILINE), (
        "the image no longer installs with `uv sync --frozen`"
    )
    installed = chemclaw_contracts.manifests_dir().relative_to(Path(sys.prefix))
    assert installed.parts[0] == "lib" and installed.parts[2:] == (
        "site-packages",
        "chemclaw_contracts",
        "manifests",
    ), f"the package no longer installs under <venv>/lib/python3.N/site-packages: {installed}"
    expected = (
        f"{workdir.group(1).rstrip('/')}/.venv/lib/python{base.group(1)}.{base.group(2)}"
        "/site-packages/chemclaw_contracts/manifests"
    )
    assert _values()["extraConnectors"]["contractsPath"] == expected, (
        f"extraConnectors.contractsPath is {_values()['extraConnectors']['contractsPath']!r}; the "
        f"image installs the fleet's manifests at {expected!r}"
    )


def test_the_image_bundle_names_are_the_names_the_image_declares() -> None:
    """`extraConnectors.imageBundles` is every connector name the image already declares.

    The render refuses a mounted bundle with one of these names (a collision is a startup error in
    every pod). Derived from the installed package and this tree's own bundles, so a connector
    either side adds is refused the day it ships.
    """
    import chemclaw_contracts

    import chemclaw.connectors

    root = Path(chemclaw.connectors.__file__).resolve().parent
    declared = set(chemclaw_contracts.manifest_names()) | {
        path.parent.name for path in root.glob("*/connector.yaml")
    }
    assert set(_values()["extraConnectors"]["imageBundles"]) == declared


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_mounting_a_bundle_the_image_already_ships_is_refused_at_render() -> None:
    """The upgrade trap: a release that mounted `pyexec` (it was not shipped then) fails at render.

    Without the guard every pod crash-loops at boot on the collision. The message names the way
    out. A bundle with a name the image does not declare still renders.
    """
    refused = _render(
        "--set",
        "extraConnectors.bundles[0].name=pyexec",
        "--set",
        "extraConnectors.bundles[0].configMap=chemclaw-connector-pyexec",
    )
    assert refused.returncode != 0
    assert "pyexec" in refused.stderr
    assert "connectors.pyexec.enabled" in refused.stderr
    accepted = _render(
        "--set",
        "extraConnectors.bundles[0].name=our-eln",
        "--set",
        "extraConnectors.bundles[0].configMap=chemclaw-connector-our-eln",
    )
    assert accepted.returncode == 0, accepted.stderr[-500:]


def test_the_image_workflow_derives_component_modules_that_actually_import() -> None:
    """`image.yml` derives the smoke list by grepping `entrypoint.sh`, and a grep reads prose.

    The derivation must read the script as the shell does, so whole-line comments are stripped
    first. Asserted as *importability* of every derived name rather than the absence of one broken
    shape, because that is the property the workflow needs.
    """
    import importlib.util
    import re

    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8")
    assert "sed -E 's/^[[:space:]]*#.*$//' deploy/entrypoint.sh" in workflow, (
        "image.yml no longer strips comments before deriving the component list, so a sentence in "
        "entrypoint.sh can be smoke-tested as a module again"
    )

    # Reproduce the workflow's own derivation rather than restating its answer.
    script = (DEPLOY / "entrypoint.sh").read_text(encoding="utf-8")
    commands = re.sub(r"(?m)^[ \t]*#.*$", "", script)
    modules = re.findall(r"python -m ([a-z_][a-z0-9_.]*)", commands)
    targets = re.findall(r"uvicorn ([a-z_][a-z0-9_.]*:[a-zA-Z_]+)", commands)
    modules += [target.split(":")[0] for target in targets]
    assert len(modules) >= 2, "the derivation found nothing; it has drifted from the script"

    for module in modules:
        assert importlib.util.find_spec(module) is not None, (
            f"image.yml would smoke-test {module!r}, which is not an importable module — the "
            "derivation has picked up prose rather than a command"
        )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_release_on_the_memory_session_store_refuses_to_render() -> None:
    """A release on the memory session store refuses to render.

    `GitNoteWriter._cluster_lock` takes its Postgres advisory lock only when
    `session_store == "postgres"`; a chart release runs separate pods with separate clones, so
    without it two pods writing one note id are last-writer-wins. Both arms: the shipped `postgres`
    must still render, which is what makes refusing safe.
    """
    refused = _render("--set", "config.CHEMCLAW_SESSION_STORE=memory")
    assert refused.returncode != 0, (
        "a release still renders on the memory session store, so every note write in it is "
        f"unguarded across pods:\n{refused.stdout[:2000]}"
    )
    assert "CHEMCLAW_SESSION_STORE" in refused.stderr, refused.stderr

    shipped = _render()
    assert shipped.returncode == 0, shipped.stderr
    assert 'CHEMCLAW_SESSION_STORE: "postgres"' in shipped.stdout, (
        "the shipped defaults no longer state the session store the guard above requires"
    )


# The ecosystems `.github/dependabot.yml` declares an updater for, mapped to the `make ci` target
# that audits one for known vulnerabilities. An ecosystem absent from here is one nothing gates,
# and the file has to say so in the accepted-risk form below.
_AUDITED_ECOSYSTEMS = {"uv": "deps-audit"}
_ACCEPTED_RISK = "ACCEPTED RISK"


def test_every_declared_ecosystem_is_audited_or_accepted() -> None:
    """Every ecosystem `.github/dependabot.yml` declares is audited or accepted as a risk.

    Asserted as a choice rather than coverage: widening the audit to `github-actions` closes nothing
    today, so each declared ecosystem must be audited by a target `make ci` runs or named as an
    accepted risk. A new ecosystem, or `deps-audit` dropped from `make ci`, fails here.
    """
    dependabot = DEPLOY.parent / ".github" / "dependabot.yml"
    document: Any = yaml.safe_load(dependabot.read_text(encoding="utf-8"))
    declared = {update["package-ecosystem"] for update in document["updates"]}
    assert declared, "no updaters are declared; this check is reading the wrong file"

    ci_target = next(
        line
        for line in (DEPLOY.parent / "Makefile").read_text().splitlines()
        if line.startswith("ci:")
    )
    gates = set(ci_target.split(":", 1)[1].split("##")[0].split())
    assert len(gates) > 10, f"the `make ci` prerequisite list did not parse: {sorted(gates)}"
    prose = dependabot.read_text(encoding="utf-8")

    unheld: list[str] = []
    for ecosystem in sorted(declared):
        target = _AUDITED_ECOSYSTEMS.get(ecosystem)
        if target is not None and target in gates:
            continue
        accepted = any(_ACCEPTED_RISK in line and ecosystem in line for line in prose.splitlines())
        if not accepted:
            unheld.append(ecosystem)

    assert not unheld, (
        f"`.github/dependabot.yml` declares updater(s) for {unheld} that no `make ci` target "
        "audits and that the file does not record as an accepted risk. Either audit the ecosystem "
        f"(and name its target in _AUDITED_ECOSYSTEMS) or write the `{_ACCEPTED_RISK}` line "
        "naming it — an updater without either is a control a reader will assume exists."
    )


def test_the_background_worker_has_an_alert_the_shared_endpoint_cannot_give_it() -> None:
    """The background worker has an alert the shared-endpoint alert cannot give it.

    Its `Recreate` strategy (one worker per corpus clone) means a worker that cannot start leaves
    none. `ChemclawNoWorkerIsScraped`'s `absent()` is satisfied by any pod serving the shared
    `metrics` port, so the distinguishing label has to be in the expression.
    """
    rules = (CHART / "templates" / "prometheusrule.yaml").read_text()
    assert "ChemclawNoBackgroundWorkerIsScraped" in rules

    expr = _alert_expression(rules, "ChemclawNoBackgroundWorkerIsScraped")
    assert "absent(" in expr, "only absent() alerts on no series existing at all"
    assert 'app_kubernetes_io_component="background-worker"' in expr, (
        "without the component label this is the shared-endpoint alert again, which any connector "
        "or front-door pod keeps satisfied while no background worker exists"
    )

    # The other direction, which is what makes the first assertion mean something: the alert this
    # one supplements must still NOT carry that label, or the two are the same rule twice.
    shared = _alert_expression(rules, "ChemclawNoWorkerIsScraped")
    assert "app_kubernetes_io_component" not in shared, (
        "ChemclawNoWorkerIsScraped now scopes to a component, so the new alert is redundant and "
        "one of the two should go"
    )


def test_the_component_label_the_worker_alert_reads_is_one_the_podmonitor_stamps() -> None:
    """An alert label the scrape config does not produce is a rule that is wrong.

    `podTargetLabels` is what copies `app_kubernetes_io_component` onto samples, so the alert and
    the PodMonitor are held together in both directions. For an `absent()` a missing label fails
    loudly: the vector matches nothing, so a `critical` alert pages permanently.
    """
    monitor = (CHART / "templates" / "podmonitor.yaml").read_text()
    assert "app.kubernetes.io/component" in monitor.split("podTargetLabels:")[1][:200], (
        "the PodMonitor stopped copying the component label, so the background-worker alert's "
        "`absent()` now matches no series and is 1 on every evaluation — a critical page that "
        "never clears, rather than a rule that never fires"
    )
    workers = (CHART / "templates" / "deployment-workers.yaml").read_text()
    assert "app.kubernetes.io/component: background-worker" in workers, (
        "the worker Deployment's component label changed; the alert's selector no longer matches it"
    )


def test_no_alert_reads_a_series_this_prometheus_cannot_see() -> None:
    """kube-state-metrics is platform monitoring; these rules are evaluated by user-workload.

    So a `kube_*` series in an alert is permanently empty, green forever. Asserted here so the next
    person cannot miss it; if a deployment federates those series, this is the one place to change.
    Every alert's expression is checked, cut at the first of `for:`, `labels:` or `annotations:`,
    because annotation prose may legitimately name those series.
    """
    rules = (CHART / "templates" / "prometheusrule.yaml").read_text()
    alerts = rules.split("- alert: ")[1:]
    expressions = "\n".join(
        min(
            (block.split(marker)[0] for marker in ("for:", "labels:", "annotations:")),
            key=len,
        )
        for block in alerts
    )
    assert len(alerts) == rules.count("- alert: "), "the split lost an alert"
    for series in ("kube_deployment_", "kube_pod_", "kube_statefulset_", "kube_daemonset_"):
        assert series not in expressions, (
            f"an alert expression reads `{series}*`, which kube-state-metrics publishes to the "
            "*platform* Prometheus in `openshift-monitoring`. A user-workload PrometheusRule "
            "cannot see those series, so this rule is green forever."
        )


#: What the front door holds before it parses anything, in MiB.
#:
#: Measured on the real serving object (uvicorn factory, lifespan run, `/healthz` served) as its
#: `Pss`. A floor: no agent graph compiled, no connector session, no turn. `Pss` is a system-wide
#: share and moves with what else maps the same pages, which is acceptable only because every use
#: here is a floor; a new derivation should use the cgroup's own peak charge.
FRONT_DOOR_RESIDENT_MIB = 432

#: The same for the background worker (`python -m chemclaw.durable.background_worker`), which starts
#: a forkserver too — `ingest/documents/sync.py` parses every crawled document in one. Measured at
#: 284,880 kB of `Pss` with every activity module imported, which is 278.2 MiB.
WORKER_RESIDENT_MIB = 279

#: What warming the parse forkserver costs the pod, in MiB.
#:
#: The forkserver's `Pss`, which sits above every pod-level delta measured for it — the property the
#: budget relies on. `forkserver` starts its server by fork *and exec*, so it is a full second copy
#: of the parser libraries, nothing copy-on-write. The live guard,
#: `test_a_warm_parse_forkserver_still_costs_what_this_budget_was_derived_against`, ratchets `VmRSS`
#: instead (see there).
FORKSERVER_POD_COST_MIB = 91

#: What a warm forkserver's `VmRSS` may be, in MiB — the live guard on the constant above.
#:
#: `VmRSS` rather than `Pss` because it belongs to the process alone and is stable under load and
#: peers, while `Pss` swings with whatever else maps the same libraries. It moves with the preload
#: closure, which is what it ratchets (adding `jinja2` passes; `pandas`, `chemclaw.core.chem` or the
#: agent graph red). The measurement runs in a child with `_PTH_INJECTORS` dropped, because
#: `forkserver` inherits the environment and a coverage `.pth` would load `coverage` into it.
#:
#: The base reading differs by a few MiB between host environments for the same closure (anonymous
#: pages, not file pages), so the ceiling leaves room for that plus a patch release. The assertion
#: prints the `RssAnon`/`RssFile` decomposition so a closure that grew can be told from another
#: environment.
FORKSERVER_RSS_CEILING_MIB = 120

#: What one parse in flight costs the pod, per MiB of the budget the *parse* declares.
#:
#: A parse is not a function of a document's expanded size (string width and DOM overhead vary it
#: several-fold), so the coefficient is taken against what the parse may allocate:
#: `document_parse_memory_bytes`, which `ingest/documents/isolate.py` sets as `RLIMIT_DATA` on the
#: child before it reads a byte. Measured over a real memory cgroup across formats, width classes
#: and budgets, the pod's peak per parse stayed under this factor. Above 1.0 because the parent
#: unpickles a second copy of the text; below 2.0 because the child's intermediates are inside its
#: limit.
#:
#: The budget bounds allocation *beyond* the document handed in, so the coefficient also depends on
#: the largest document a binding allows: `PARSE_COEFFICIENT_BASIS_BYTES` is that basis, and the
#: test below holds `binding.max_file_bytes` to it.
PARSE_MIB_PER_PARSE_BUDGET_MIB = 1.4

#: What the front door keeps after its first turns, in MiB, over `FRONT_DOOR_RESIDENT_MIB`: the
#: compiled graph's modules, lazy imports and once-filled caches. Measured as the cgroup's anonymous
#: charge on the real front door against the mock LLM; flat after warm-up. Rounded up.
TURN_WARM_MIB = 70

#: What each admitted turn permit adds to the front door, in MiB, on a short thread. Retained as
#: allocator high-water and bounded by the admission cap, which is why it is multiplied by that cap
#: below. Rounded up.
TURN_MIB_PER_PERMIT = 6

#: What a turn costs the front door per byte of the thread it continues, in bytes of pod per byte
#: of the stored `messages` blob (`agent/checkpointer.stored_thread_bytes`).
#:
#: Every turn loads its whole thread (compaction trims what is sent, not what is held), so this is
#: a per-permit term bounded by `session_max_thread_bytes`. Width-dependent like the parse
#: coefficient, so measured at the widest code point; the largest ratio at or under the ceiling,
#: rounded up.
POD_BYTES_PER_THREAD_BYTE = 18


def test_the_parse_coefficient_still_describes_the_largest_document_a_binding_may_declare() -> None:
    """The parse coefficient's basis covers the largest document a binding may declare.

    The coefficient multiplies a budget that bounds allocation *beyond* the document, so the pod's
    charge depends on `binding.max_file_bytes` as well. Asserted as agreement between that field's
    bound and `PARSE_COEFFICIENT_BASIS_BYTES`: raising the cap is legitimate and costs a
    re-measurement, and this makes that cost visible.
    """
    from chemclaw.ingest.documents.binding import (
        PARSE_COEFFICIENT_BASIS_BYTES,
        DocumentShareBinding,
    )

    field = DocumentShareBinding.model_fields["max_file_bytes"]
    ceiling = next(
        (getattr(item, "le", None) for item in field.metadata if getattr(item, "le", None)),
        None,
    )
    assert ceiling == PARSE_COEFFICIENT_BASIS_BYTES, (
        f"`max_file_bytes` is bounded at {ceiling} and the parse coefficient was measured against "
        f"{PARSE_COEFFICIENT_BASIS_BYTES}; a binding may hand the pod a document larger than "
        "anything `PARSE_MIB_PER_PARSE_BUDGET_MIB` has ever seen, and no inequality here moves"
    )
    assert field.default <= PARSE_COEFFICIENT_BASIS_BYTES, (
        "the shipped default is already above the basis the coefficient was measured at"
    )


def _declared_mib(resources: dict[str, Any], kind: str) -> int:
    """The `requests`/`limits` memory a `resources` block declares, in MiB."""
    declared = str(resources[kind]["memory"])
    units = {"Mi": 1, "Gi": 1024}
    suffix = declared[-2:]
    assert suffix in units, f"unhandled memory unit in {declared!r}"
    return int(declared[:-2]) * units[suffix]


#: The env name both halves of the parse budget are spelled with.
_PARSE_BUDGET_KEY = "CHEMCLAW_DOCUMENT_PARSE_MEMORY_BYTES"


def _parse_budget_mib(override: Any) -> float:
    """What one parse may allocate on a pod, in MiB, resolved the way the kubelet resolves it.

    Per component, because the front door and the background worker have different limits and slot
    counts. Three sources in container order: an explicit `env` entry on the Deployment, then the
    shared ConfigMap reached through `envFrom`, then the code default — so a fleet-wide raise moves
    the inequality too.
    """
    from chemclaw.core.config import settings

    if override in (None, ""):
        return float(settings.document_parse_memory_bytes) / 1024**2
    return float(override) / 1024**2


def _parse_peak_mib(concurrent: int, budget_mib: float) -> float:
    """What `concurrent` parses at `budget_mib` each peak at, in MiB."""
    return concurrent * PARSE_MIB_PER_PARSE_BUDGET_MIB * budget_mib


def _front_door_turn_mib(config: dict[str, Any]) -> tuple[float, float]:
    """What the front door's turns hold, in MiB: warm at the admission cap, and at its peak.

    Warm (warm-up plus every permit's high-water) belongs under the *request*; peak adds each permit
    loading a thread at `session_max_thread_bytes` and belongs under the *limit*. Inputs resolve as
    a container sees them: the release's `config` first, then the code default.
    """
    from chemclaw.core.config import settings

    def resolved(key: str, default: int) -> int:
        """The release's value where it states one — a YAML `0` included — else the code's."""
        stated = config.get(key)
        return default if stated in (None, "") else int(stated)

    permits = resolved(
        "CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS", settings.service_max_concurrent_turns
    )
    thread_bytes = resolved("CHEMCLAW_SESSION_MAX_THREAD_BYTES", settings.session_max_thread_bytes)
    assert thread_bytes, (
        "session_max_thread_bytes is 0, so a turn may load a thread of any size and the front "
        "door's peak has no bound this file can state"
    )
    warm = TURN_WARM_MIB + permits * TURN_MIB_PER_PERMIT
    return warm, warm + permits * POD_BYTES_PER_THREAD_BYTE * thread_bytes / 1024**2


def test_a_pod_that_starts_a_parse_forkserver_fits_the_memory_it_declares() -> None:
    """Both components that parse documents are sized against the second process they start.

    An inequality over measured constants and the settings that bound the work: the resident floor
    plus the warm forkserver must fit the *request*, and that plus concurrent parses at
    `document_parse_memory_bytes` times the coefficient (plus turns, on the front door) must fit the
    *limit*. The multiplied quantity is the ceiling the kernel enforces on the allocating process,
    so raising the budget or the parse/activity concurrency, or lowering a declaration, fails here
    instead of in an OOMKill.
    """
    from chemclaw.core.config import settings

    values = _values()
    resources = values["resources"]
    front_door = FRONT_DOOR_RESIDENT_MIB + FORKSERVER_POD_COST_MIB
    worker = WORKER_RESIDENT_MIB + FORKSERVER_POD_COST_MIB
    # The worker declares its own parse allowance (the fleet-wide one is derived from the front
    # door), read from the values file so the inequality uses the number the Deployment renders.
    worker_override = values["workers"]["background"].get("documentParseMemoryBytes")
    # The fleet-wide entry every component reads through `envFrom`; absent today, but reading it
    # means a release-wide raise moves the inequality.
    fleet_wide = (values.get("config") or {}).get(_PARSE_BUDGET_KEY)

    # The turns are the third term and the one that scales with load; the worker takes no front-door
    # turns.
    turns_warm, turns_peak = _front_door_turn_mib(values.get("config") or {})

    for label, key, idle, concurrent, budget_mib, turns_idle, turns in (
        (
            "front door",
            "service",
            front_door + turns_warm,
            settings.attachment_max_concurrent_parses,
            _parse_budget_mib(fleet_wide),
            turns_warm,
            turns_peak,
        ),
        # The worker's count is its activity cap, not 1: the sequential document sync makes 1 true
        # today only by an argument a second schedule or a manual run breaks, and the pod fits the
        # cap outright.
        (
            "background worker",
            "worker",
            worker,
            settings.worker_max_concurrent_activities,
            _parse_budget_mib(worker_override if worker_override is not None else fleet_wide),
            0.0,
            0.0,
        ),
    ):
        request = _declared_mib(resources[key], "requests")
        limit = _declared_mib(resources[key], "limits")
        assert idle <= request, (
            f"the {label} holds {idle:.0f} MiB with its parse forkserver warm and nothing in "
            f"flight ({turns_idle:.0f} of it kept from turns already served), "
            f"against a memory request of {request} MiB. A pod over its request while idle is "
            "scheduled onto a node that does not have the memory it uses, and is the first thing "
            "evicted when that node comes under pressure"
        )
        needed = idle - turns_idle + turns + _parse_peak_mib(concurrent, budget_mib)
        assert needed <= limit, (
            f"{concurrent} concurrent parse(s) at the {budget_mib:.0f} MiB allocation ceiling this "
            f"component declares need {needed:.0f} MiB in the {label} — the resident set, the "
            f"warm forkserver and {turns:.0f} MiB of turns at the admission cap included — against "
            f"the {limit} MiB its container declares. That is an OOMKill of the whole pod, not a "
            "refused upload"
        )


#: The program the measurement runs, in a child of this process rather than in it.
#:
#: It imports the shipped module, so this ratchets the real `_PRELOAD`. A child that ends without a
#: forkserver prints nothing, which the caller reports.
_FORKSERVER_RSS_PROGRAM = """
from multiprocessing import forkserver

from chemclaw.ingest.documents.isolate import parse_document_isolated

parse_document_isolated("budget.csv", b"id,yield\\nR-1,88\\n", None, 60.0)
# Read through `getattr` because the pid is not on typeshed's `ForkServer`: upstream keeps no
# public handle on the process it starts, and the alternative -- matching a `/proc` child by its
# command line -- would be a second private shape with more code around it.
pid = getattr(forkserver._forkserver, "_forkserver_pid", None)
if pid is not None:
    with open("/proc/%d/status" % pid, encoding="utf-8") as status:
        for line in status:
            # `RssAnon`/`RssFile` are the decomposition, printed before the total because the
            # caller reads the *last* field as the reading. They are what tells a closure that grew
            # from an environment whose allocator holds more anonymous pages for the same objects;
            # see `FORKSERVER_RSS_CEILING_MIB` for the run where that distinction was the answer.
            if line.startswith(("RssAnon:", "RssFile:")):
                print(line.split()[0], line.split()[1])
    with open("/proc/%d/status" % pid, encoding="utf-8") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                print(line.split()[1])
                break
"""

#: Environment variables that load a module into *every* interpreter this virtualenv starts via a
#: `.pth`, and so into the process being measured rather than the closure. Dropped for the
#: measurement.
_PTH_INJECTORS = ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG")


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs a Linux /proc")
def test_a_warm_parse_forkserver_still_costs_what_this_budget_was_derived_against() -> None:
    """The constant the chart rests on is re-measured here, against the shipped preload list.

    `FORKSERVER_POD_COST_MIB` is a property of this tree: whatever `isolate._PRELOAD` (or an import
    in `ingest/documents/parse.py`) drags in. Measured as `VmRSS`, which belongs to the process
    alone, rather than `Pss`, whose share depends on other processes mapping the same pages.
    Measured in a child with `_PTH_INJECTORS` dropped, because the forkserver inherits the
    environment and a coverage `.pth` would otherwise make how the gate was invoked red this
    assertion.
    """
    environment = {k: v for k, v in os.environ.items() if k not in _PTH_INJECTORS}
    child = subprocess.run(
        [sys.executable, "-c", _FORKSERVER_RSS_PROGRAM],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=180,
    )
    reading = child.stdout.split()
    # An empty answer is a parse that ran without a forkserver, or an upstream rename of the handle
    # the child reads — a named failure rather than a silent skip, which is what this used to be.
    assert child.returncode == 0 and reading and reading[-1].isdigit(), (
        "the measurement child reported no forkserver `VmRSS`: a parse ran without a forkserver, "
        "or upstream renamed the private handle it reads, and this budget then describes another "
        f"shape. stdout={child.stdout!r} stderr={child.stderr[-2000:]!r}"
    )
    measured = int(reading[-1]) / 1024
    split = " ".join(reading[:-1]) or "no decomposition reported"
    assert measured <= FORKSERVER_RSS_CEILING_MIB, (
        f"a warm parse forkserver is resident at {measured:.1f} MiB where the budget above was "
        f"derived against a closure measured at {FORKSERVER_RSS_CEILING_MIB}. Whatever grew "
        "`isolate._PRELOAD`'s closure has moved what every front door and every background worker "
        "costs its node, and `FORKSERVER_POD_COST_MIB` — with `resources.service` and "
        f"`resources.worker` under it — needs re-deriving before it ships. Decomposition: {split} "
        "(kB). **Before attributing this to `_PRELOAD`, run the same measurement against a "
        "revision whose closure is known**, which is the one thing that separates a closure that "
        "grew from a machine that holds more anonymous pages for the same objects: `git archive "
        "<rev> src | tar -x -C /tmp/base && PYTHONPATH=/tmp/base/src python -c "
        "'import chemclaw.ingest.documents.parse'` and read `/proc/self/status`. A reading that is "
        "high at the older revision too is the environment, and this ceiling is what moves"
    )


def test_the_chart_caps_turns_per_actor_strictly_below_the_process_cap() -> None:
    """A fairness cap at or above the pod's own cap enforces nothing while reading as protection.

    The code default is 0 (off), because the live storm sweeps the admission cap from one
    credential, so the production posture lives in the chart and is checked here: strictly below the
    process cap.
    """
    config = _values()["config"]
    per_actor = int(config["CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS_PER_ACTOR"])
    per_process = int(config["CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS"])

    assert per_actor > 0, "the chart carries the production posture; 0 is the code default"
    assert per_actor < per_process, (
        f"a per-actor cap of {per_actor} against {per_process} permits refuses nothing; one "
        f"principal can still hold every permit on the replica"
    )


#: The router's own namespace selector, as `networkPolicy.ingressNamespaces` already ships it for
#: the chat front door. Written once here because the two tests below need the same value on
#: opposite sides of one assertion — one renders with it, the other without.
_ROUTER_PEER = 'mcpFace.ingressNamespaces=[{"network.openshift.io/policy-group":"ingress"}]'


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_publishing_the_face_without_a_router_peer_refuses_to_render() -> None:
    """Publishing the face without a router peer refuses to render.

    `mcp-face-ingress` admits only same-namespace pods plus `mcpFace.ingressNamespaces` (default
    empty), so a `Route` with no router peer publishes an address the policy drops. Three
    directions through a real render, because a `fail` is easy to write too wide:

    1. route on, list empty: refused, naming the key to set;
    2. route on, list named: renders the `Route` and the policy with the peer;
    3. face on, route off: renders with an empty peer list, a stated in-cluster posture.

    Not defaulted from `networkPolicy.ingressNamespaces`: that list grants access to a surface
    behind Entra, this one to a surface guarded by one bearer token.
    """
    unstated = _render("--set", "mcpFace.enabled=true", "--set", "mcpFace.route.enabled=true")
    assert unstated.returncode != 0, (
        "the chart published a Route whose traffic its own `mcp-face-ingress` policy drops:\n"
        f"{unstated.stdout[:2000]}"
    )
    assert "mcpFace.ingressNamespaces" in unstated.stderr, (
        f"the refusal does not name the key that fixes it: {unstated.stderr}"
    )

    stated = _render(
        "--set",
        "mcpFace.enabled=true",
        "--set",
        "mcpFace.route.enabled=true",
        "--set-json",
        _ROUTER_PEER,
    )
    assert stated.returncode == 0, stated.stderr
    published = [
        document
        for document in yaml.safe_load_all(stated.stdout)
        if document and document.get("metadata", {}).get("name", "").endswith("-mcp-face")
    ]
    assert {document["kind"] for document in published} >= {"Route", "Service"}, (
        f"naming the peer did not publish the face: {[d['kind'] for d in published]}"
    )
    policy = next(
        document
        for document in yaml.safe_load_all(stated.stdout)
        if document and document.get("metadata", {}).get("name", "").endswith("-mcp-face-ingress")
    )
    peers = policy["spec"]["ingress"][0]["from"]
    assert any("namespaceSelector" in peer for peer in peers), (
        "the policy still admits only this namespace's pods, so the Route the chart just agreed to "
        f"publish is still dropped: {peers}"
    )

    # The narrow direction: an unpublished face with no peers is a posture, not an omission.
    internal = _render("--set", "mcpFace.enabled=true")
    assert internal.returncode == 0, internal.stderr
    names = {
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in yaml.safe_load_all(internal.stdout)
        if document
    }
    assert ("NetworkPolicy", "chemclaw-mcp-face-ingress") in names, sorted(names)
    # By parsed name, not by a substring of the whole render: the *chat* front door renders its own
    # `Route` in the same output, so a text search finds one and says nothing about the face.
    assert ("Route", "chemclaw-mcp-face") not in names, (
        f"an unpublished face rendered a Route anyway: {sorted(names)}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize("overrides", _OFF_BY_DEFAULT_RENDERS.values(), ids=_OFF_BY_DEFAULT_RENDERS)
def test_no_rendered_setting_reaches_a_pod_in_scientific_notation(
    overrides: tuple[str, ...],
) -> None:
    """Helm renders a large or fractional values entry as a float, and `Settings` cannot read one.

    A values-file integer like `335544320` piped through `| quote` reaches the pod as
    `"3.3554432e+08"`, a pydantic `int_parsing` error and a crash loop, invisible to every check
    that reads `values.yaml` with `yaml.safe_load`. So this reads container `env`, `envFrom` sources
    and the `data` of every rendered ConfigMap and Secret (a float there crash-loops the whole
    release), over `_OFF_BY_DEFAULT_RENDERS` too. Scoped to `CHEMCLAW_*`, the names `Settings`
    parses.
    """
    rendered = _render(*overrides)
    assert rendered.returncode == 0, rendered.stderr
    offenders: list[str] = []

    def _suspect(where: str, name: str, value: object) -> None:
        text = str(value)
        if name.startswith("CHEMCLAW_") and ("e+" in text or "E+" in text):
            offenders.append(f"{where} {name}={text}")

    for doc in yaml.safe_load_all(rendered.stdout):
        if not doc:
            continue
        kind, name = doc.get("kind"), doc["metadata"]["name"]
        if kind in {"ConfigMap", "Secret"}:
            # Where a float does the most damage: one `envFrom: configMapRef` per pod, so the
            # whole release crash-loops rather than one Deployment.
            for key, value in (doc.get("data") or {}).items():
                _suspect(f"{kind} {name}", key, value)
            continue
        for owner, spec in _pod_specs(yaml.safe_dump(doc)):
            for container in [*spec.get("containers", []), *spec.get("initContainers", [])]:
                for entry in container.get("env") or []:
                    _suspect(f"{owner}/{container['name']}", entry["name"], entry.get("value", ""))

    assert not offenders, (
        "these rendered settings reach a pod in scientific notation, which pydantic refuses with "
        f"`int_parsing`, so the container crash-loops on start: {offenders}. Render it with "
        "`int64` rather than `| quote`, which prints a Helm float the way Go does"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        pytest.param(
            ("--set", "service.autoscaling.maxReplicas=0"), "maxReplicas", id="hpa-ceiling"
        ),
        pytest.param(
            (
                "--set",
                "service.autoscaling.enabled=false",
                "--set",
                "service.replicas=0",
            ),
            "service.replicas",
            id="fixed-count",
        ),
    ],
)
def test_a_release_with_no_front_door_refuses_to_render(
    overrides: tuple[str, ...], named: str
) -> None:
    """A zero front door renders a release in which every pod refuses to start.

    `service_fleet_replicas` is `gt=0` and rendered into the ConfigMap every pod reads, and the
    entrypoint constructs `Settings` before dispatching, so every container and hook Job would
    crash-loop and `helm upgrade` would never converge. Neither `helm template` nor `kubeconform`
    can see it, so a render-time `fail` naming the key is the guard. Both reachable arms are
    parametrised (`service.replicas=0` alone changes nothing while the HPA is on).
    """
    refused = _render(*overrides)

    assert refused.returncode != 0, (
        "the chart still renders a release whose every pod refuses to start on "
        f"{overrides}:\n{refused.stdout[:2000]}"
    )
    assert named in refused.stderr, refused.stderr
    assert "CHEMCLAW_SERVICE_FLEET_REPLICAS" in refused.stderr, (
        "the refusal must name the setting that actually refuses the value, since that is what an "
        f"operator has to look up: {refused.stderr}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_front_door_count_the_chart_refuses_is_the_one_settings_refuses() -> None:
    """The chart's bound and `Settings`' bound are one decision, asserted against each other.

    A drifted chart guard would either render into a crash loop or refuse a value the code accepts.
    Read off `model_fields`, so moving the field moves this.
    """
    from chemclaw.core.config import Settings

    constraints = Settings.model_fields["service_fleet_replicas"].metadata
    floors = [getattr(item, "gt", None) for item in constraints]
    assert 0 in floors, (
        "service_fleet_replicas no longer carries `gt=0`, so the chart guard refusing zero is now "
        f"stricter than the code it protects: {constraints}"
    )

    # And the value one above the floor still renders, so the guard is a floor rather than a ban.
    assert _render("--set", "service.autoscaling.maxReplicas=1").returncode == 0


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_fixed_replica_count_renders_nowhere_while_the_hpa_is_on() -> None:
    """`service.replicas` is dead config on the shipped defaults, and that is now written down.

    With the HPA on, `chemclaw.frontDoorProcesses` reads `maxReplicas` and the Deployment omits
    `replicas`, so a `--set service.replicas` does nothing. Pinned so the `values.yaml` comment
    saying so is corrected if that ever changes.
    """
    baseline = _render()
    overridden = _render("--set", "service.replicas=1")

    assert baseline.returncode == 0 and overridden.returncode == 0
    assert baseline.stdout == overridden.stdout, (
        "service.replicas now changes the shipped render, so the values.yaml comment saying it is "
        "read only when the HPA is off is stale"
    )


def _containers(rendered: str) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Every container and init container in a render: `(owner, pod spec, container)`."""
    return [
        (name, spec, container)
        for name, spec in _pod_specs(rendered)
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or [])
    ]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_push_token_is_required_only_where_something_pushes() -> None:
    """The push token is required only where something pushes.

    With `knowledge.sync.repoUrl` empty nothing clones a writer checkout or pushes, so the key is
    `optional: true`; with a remote configured it is required, since every note push would fail
    without it. Mounted in both cases.
    """
    name = _values()["secrets"]["keys"]["knowledgeRepoToken"]

    def _refs(rendered: str) -> list[dict[str, Any]]:
        return [
            env["valueFrom"]["secretKeyRef"]
            for _, _, container in _containers(rendered)
            for env in container.get("env") or []
            if env["name"] == name
        ]

    without_remote = _render()
    assert without_remote.returncode == 0, without_remote.stderr
    refs = _refs(without_remote.stdout)
    assert refs, f"{name} is no longer mounted at all on a release with no remote"
    assert all(ref.get("optional") is True for ref in refs), (
        f"{name} is required on a release with no knowledge remote, where nothing pushes"
    )

    with_remote = _render("--set", "knowledge.sync.repoUrl=https://git.example.org/notes.git")
    assert with_remote.returncode == 0, with_remote.stderr
    refs = _refs(with_remote.stdout)
    assert refs and not any(ref.get("optional") for ref in refs), (
        f"{name} is optional on a release that pushes notes, so an absent token fails every note "
        "at push instead of failing the pod at creation"
    )
    # The other required keys are untouched by the rule.
    others = set(_values()["secrets"]["keys"].values()) - {name}
    for _, _, container in _containers(without_remote.stdout):
        for env in container.get("env") or []:
            if env["name"] in others:
                assert not env["valueFrom"]["secretKeyRef"].get("optional"), env


def _service_ingress_peers(rendered: str) -> list[dict[str, Any]]:
    """The `from:` peers of the front door's ingress policy."""
    for document in yaml.safe_load_all(rendered):
        if document and document["metadata"]["name"] == "chemclaw-service-ingress":
            peers: list[dict[str, Any]] = document["spec"]["ingress"][0]["from"]
            return peers
    raise AssertionError("no chemclaw-service-ingress NetworkPolicy was rendered")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_ui_beside_the_release_may_reach_the_front_door() -> None:
    """The `Chemclaw3_ui` BFF beside the release may reach the front door.

    `networkPolicy.uiPodSelector` admits the UI's pods by label from this namespace only (a bare
    `podSelector`); `null` removes it. The labels are checked against the UI's own Deployment when
    that checkout is present, since a stale selector admits nothing silently.
    """
    selector = _values()["networkPolicy"]["uiPodSelector"]
    assert selector, "the shipped chart admits no UI pod"
    rendered = _render()
    assert rendered.returncode == 0, rendered.stderr
    ui_peers = [
        peer
        for peer in _service_ingress_peers(rendered.stdout)
        if peer.get("podSelector", {}).get("matchLabels") == selector
    ]
    assert ui_peers == [{"podSelector": {"matchLabels": selector}}], (
        "the UI peer is missing, or carries a namespaceSelector that would widen it beyond this "
        f"namespace: {ui_peers}"
    )
    removed = _render("--set", "networkPolicy.uiPodSelector=null")
    assert removed.returncode == 0, removed.stderr
    assert not [
        peer
        for peer in _service_ingress_peers(removed.stdout)
        if peer.get("podSelector", {}).get("matchLabels") == selector
    ], "`networkPolicy.uiPodSelector=null` did not remove the UI peer"

    from tests.siblings import SIBLING_SKIP, sibling_root

    checkout, reason = sibling_root("CHEMCLAW_UI_REPO", "Chemclaw3_ui")
    if checkout is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}; the selector was NOT checked against the UI's pods")
    deployment = yaml.safe_load(
        (checkout / "deploy" / "openshift" / "deployment.yaml").read_text(encoding="utf-8")
    )
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels.items() >= selector.items(), (
        f"networkPolicy.uiPodSelector {selector} does not match the UI's pod labels {labels}"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_private_ca_reaches_every_container_that_reads_the_settings_pointing_at_it() -> None:
    """`trustedCA` mounts one PEM bundle into every container and points opted-in settings at it.

    The CA settings and a DSN's `sslrootcert=` are file paths, so every container that reads them
    (the migrate and convert hook Jobs included) needs the mount or cannot reach its database. Off,
    nothing renders; misconfigured, the render refuses.
    """
    path = "/etc/chemclaw/ca/ca.crt"
    rendered = _render(
        "--set",
        "trustedCA.configMap=site-ca",
        "--set",
        "trustedCA.llm=true",
        "--set",
        "trustedCA.entra=true",
        "--set",
        "trustedCA.git=true",
    )
    assert rendered.returncode == 0, rendered.stderr
    containers = _containers(rendered.stdout)
    assert {name for name, _, _ in containers} >= {"chemclaw-migrate", "chemclaw-convert"}
    missing: list[str] = []
    for name, spec, container in containers:
        volumes = {volume["name"]: volume for volume in spec.get("volumes") or []}
        mounts = {mount["name"]: mount for mount in container.get("volumeMounts") or []}
        env = {e["name"]: e.get("value") for e in container.get("env") or []}
        if (
            "trusted-ca" not in mounts
            or volumes.get("trusted-ca", {}).get("configMap", {}).get("name") != "site-ca"
        ):
            missing.append(f"{name}/{container['name']}: no trusted-ca mount")
        elif not mounts["trusted-ca"].get("readOnly"):
            missing.append(f"{name}/{container['name']}: trusted-ca is writable")
        for setting in ("CHEMCLAW_LLM_TLS_CA_BUNDLE", "CHEMCLAW_ENTRA_CA_BUNDLE", "GIT_SSL_CAINFO"):
            if env.get(setting) != path:
                missing.append(f"{name}/{container['name']}: {setting}={env.get(setting)!r}")
    assert not missing, "\n".join(missing)

    off = _render()
    assert off.returncode == 0, off.stderr
    assert "trusted-ca" not in off.stdout and "CHEMCLAW_ENTRA_CA_BUNDLE" not in off.stdout

    # Mounted without opting a setting in: the file is there and no setting is redirected, which
    # is the Postgres-only case (`sslrootcert=` in the DSN names it).
    mount_only = _render("--set", "trustedCA.secret=site-ca")
    assert mount_only.returncode == 0, mount_only.stderr
    assert "CHEMCLAW_LLM_TLS_CA_BUNDLE" not in mount_only.stdout
    assert "secretName: site-ca" in mount_only.stdout

    for refused in (
        ("--set", "trustedCA.llm=true"),
        ("--set", "trustedCA.git=true"),
        ("--set", "trustedCA.configMap=a", "--set", "trustedCA.secret=b"),
    ):
        result = _render(*refused)
        assert result.returncode != 0 and "trustedCA" in result.stderr, refused


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_a_result_sink_this_image_does_not_ship_can_be_mounted_and_discovered() -> None:
    """`extraSinks` puts a `sink.yaml` folder on `CHEMCLAW_RESULT_SINKS_DIR` in every pod.

    Mirrors `extraConnectors` and is asserted the same way: the variable once, in the shared
    ConfigMap, and the mount on every container that reads it, the two migration hooks excepted.
    """
    rendered = _render(
        "--set",
        "extraSinks.sinks[0].name=postgres",
        "--set",
        "extraSinks.sinks[0].configMap=chemclaw-sink-postgres",
    )
    assert rendered.returncode == 0, rendered.stderr
    values = _values()["extraSinks"]
    config = _rendered_config(rendered.stdout)
    assert config["CHEMCLAW_RESULT_SINKS_DIR"] == f"{values['mountPath']}:{values['shippedPath']}"
    assert "CHEMCLAW_RESULT_SINKS_DIR" not in _rendered_config(_render().stdout), (
        "a release that mounts no sink must not restate the image's sink directory"
    )
    mount = f"{values['mountPath']}/postgres"
    exempt = {"chemclaw-migrate", "chemclaw-convert"}
    missing = [
        f"{name}/{container['name']}"
        for name, _, container in _containers(rendered.stdout)
        if name not in exempt
        and not (container.get("command") or [""])[0].endswith("chemclaw-knowledge-sync")
        and mount not in {m["mountPath"] for m in container.get("volumeMounts") or []}
    ]
    assert not missing, f"containers that read CHEMCLAW_RESULT_SINKS_DIR without {mount}: {missing}"


def test_the_shipped_sink_path_is_the_path_the_image_has() -> None:
    """`extraSinks.shippedPath` restates the image's layout, derived like the connector one."""
    import chemclaw.publish.sinks

    containerfile = (DEPLOY / "Containerfile").read_text(encoding="utf-8")
    workdir = re.search(r"^WORKDIR (\S+)", containerfile, flags=re.MULTILINE)
    assert workdir, "deploy/Containerfile declares no WORKDIR"
    package = Path(chemclaw.publish.sinks.__file__).resolve().parent
    expected = f"{workdir.group(1).rstrip('/')}/{package.relative_to(DEPLOY.parent)}"
    assert _values()["extraSinks"]["shippedPath"] == expected
