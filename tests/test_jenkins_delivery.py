"""The delivery pipelines describe this repository; these are the halves a file can check.

A Jenkinsfile cannot run here, but every claim the pipelines make about this tree can be checked
offline:

- each `make` target they invoke exists;
- each script they call exists and parses;
- the deploy path passes a digest rather than a tag, the property `values.yaml` builds its release
  knob around;
- `DRY_RUN` defaults to true, so a first run mutates nothing.

Whether any of it works against a cluster is not checked, and `deploy/jenkins/README.md` says so.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.siblings import SIBLING_SKIP, sibling_root

_ROOT = Path(__file__).resolve().parents[1]
_JENKINS_DIR = _ROOT / "deploy" / "jenkins"
_PIPELINES = (_ROOT / "Jenkinsfile", _JENKINS_DIR / "Jenkinsfile.release")
_SHELL = sorted((_JENKINS_DIR / "lib").glob("*.sh")) + sorted(
    (_JENKINS_DIR / "targets").glob("*.sh")
)

_MAKE_CALL = re.compile(r"\bmake ([a-z][a-z-]*(?: [a-z][a-z-]*)*)")
_MAKE_TARGET = re.compile(r"^([a-zA-Z_-]+):", re.MULTILINE)


def _make_targets() -> set[str]:
    return set(_MAKE_TARGET.findall((_ROOT / "Makefile").read_text(encoding="utf-8")))


def test_every_make_target_the_pipelines_invoke_exists() -> None:
    """A renamed target must break the pipeline here, not at 2am in front of a namespace."""
    targets = _make_targets()
    assert targets, "no Makefile targets parsed — this test would assert nothing"

    invoked: set[str] = set()
    for pipeline in _PIPELINES:
        for call in _MAKE_CALL.findall(pipeline.read_text(encoding="utf-8")):
            invoked.update(call.split())

    assert invoked, "the pipelines invoke no make target — the parse has drifted"
    missing = sorted(invoked - targets)
    assert not missing, f"the Jenkins pipelines call make targets that do not exist: {missing}"


def test_every_script_the_pipelines_call_exists_and_is_executable() -> None:
    """The `sh` steps name paths; a moved file is a red build against a live cluster otherwise."""
    referenced: set[str] = set()
    for pipeline in _PIPELINES:
        text = pipeline.read_text(encoding="utf-8")
        referenced.update(re.findall(r"deploy/jenkins/[\w./-]+\.sh", text))

    assert referenced, "no deploy/jenkins scripts referenced — the parse has drifted"
    for path in sorted(referenced):
        script = _ROOT / path
        assert script.is_file(), f"{path} is called by a pipeline and does not exist"
        assert script.stat().st_mode & 0o111, f"{path} is called by a pipeline and is not +x"


def test_the_shell_halves_parse() -> None:
    """`bash -n` is the cheapest possible proof that an unrunnable file is at least well formed."""
    assert _SHELL, "no shell scripts found under deploy/jenkins — this test would assert nothing"
    for script in _SHELL:
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name} does not parse: {result.stderr.strip()}"


def _shell_as_the_shell_receives_it(block: str) -> str:
    r"""Resolve a Groovy GString to the text bash is actually handed.

    `${...}` is interpolated by Jenkins; `\${...}` and `\$(...)` reach the shell verbatim (how a
    pipeline writes a shell variable); a `\\` at end of line reaches it as a line continuation.
    """
    resolved = re.sub(r"(?<!\\)\$\{[^}]*\}", "PLACEHOLDER", block)
    return resolved.replace("\\$", "$").replace("\\\\", "\\")


def _parses_as_shell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)


def test_every_shell_block_in_the_pipelines_parses() -> None:
    """Every shell block in the pipelines parses.

    The shell bodies are strings no linter here checks, so `bash -n` over the text the shell
    receives catches an unbalanced quote or a lost continuation before a run.
    """
    checked = 0
    for pipeline in _PIPELINES:
        text = pipeline.read_text(encoding="utf-8")
        for block in re.findall(r'"""(.*?)"""', text, re.S):
            result = _parses_as_shell(_shell_as_the_shell_receives_it(block))
            assert result.returncode == 0, (
                f"a shell block in {pipeline.name} does not parse: {result.stderr.strip()}"
            )
            checked += 1
        for block in re.findall(r"sh '''(.*?)'''", text, re.S):
            result = _parses_as_shell(block)
            assert result.returncode == 0, (
                f"a shell block in {pipeline.name} does not parse: {result.stderr.strip()}"
            )
            checked += 1
    assert checked >= 5, f"only {checked} shell blocks found — the parse has drifted"


def test_the_cluster_target_deploys_bytes_rather_than_a_pointer() -> None:
    """The cluster target deploys a digest rather than a tag.

    `values.yaml` ignores `image.tag` when a digest is set, because a rollback to a re-pushed tag
    fetches unreviewed bytes and breaks the build revision audit records stamp.
    """
    target = (_JENKINS_DIR / "targets" / "openshift.sh").read_text(encoding="utf-8")
    assert "image.digest" in target, "the helm path no longer sets image.digest"
    assert "--set image.tag" not in target, (
        "the helm path deploys a tag, which the chart's own release knob refuses"
    )
    assert "@${digest}" in target, "the Deployment path no longer pins the image by digest"


def test_a_release_states_its_egress_posture() -> None:
    """A release states its egress posture.

    An unstated posture renders `to: []`, which a NetworkPolicy reads as every destination; the
    chart refuses to render, and the target must not supply the permissive answer to get past it.
    """
    target = (_JENKINS_DIR / "targets" / "openshift.sh").read_text(encoding="utf-8")
    assert "ALLOW_ANY_EGRESS_DESTINATION" in target, (
        "no way to state the permissive posture deliberately"
    )
    assert "allowAnyDestination=true" in target
    assert "ALLOW_ANY_EGRESS_DESTINATION:-false" in target, (
        "the permissive posture must be opt-in; defaulting it on is the failure the chart's "
        "refusal-to-render exists to prevent"
    )


def test_dry_run_is_the_default_everywhere() -> None:
    """First runs happen against real namespaces. The safe direction has to be the default."""
    for script in (_JENKINS_DIR / "targets").glob("*.sh"):
        assert "DRY_RUN:-true" in script.read_text(encoding="utf-8"), (
            f"{script.name} does not default DRY_RUN to true"
        )
    for pipeline in _PIPELINES:
        text = pipeline.read_text(encoding="utf-8")
        assert "booleanParam(name: 'DRY_RUN', defaultValue: true" in text, (
            f"{pipeline.name} does not default its DRY_RUN parameter to true"
        )


def test_the_release_job_refuses_a_tag_where_a_digest_belongs() -> None:
    """The one guard that cannot live in the shell: the parameters arrive from a human."""
    release = (_JENKINS_DIR / "Jenkinsfile.release").read_text(encoding="utf-8")
    assert "startsWith('sha256:')" in release, "the release job accepts a tag as a digest"


def test_every_free_text_release_parameter_is_allowlist_validated() -> None:
    """Every free-text release parameter is allowlist-validated before any `sh` sees it.

    `string(...)` parameters are interpolated into `sh` blocks, where a metacharacter would run on
    the agent (CWE-78); choice and boolean parameters are fixed by Jenkins. A new free-text
    parameter that skips the `Validate parameters` stage fails here.
    """
    for pipeline in _PIPELINES:
        text = pipeline.read_text(encoding="utf-8")
        assert "stage('Validate parameters')" in text, (
            f"{pipeline.name} lost its parameter-validation stage"
        )
        free_text = set(re.findall(r"string\(name: '([^']+)'", text))
        assert free_text, f"no free-text parameters parsed in {pipeline.name} — parse drifted"
        validated = set(re.findall(r"\[name: '([^']+)', value: params\.", text))
        missing = free_text - validated
        assert not missing, (
            f"{pipeline.name}: free-text parameters reach sh without validation: {missing}"
        )
        # Every validation is an anchored allowlist match, not a loose contains-check.
        assert "c.value ==~ c.pattern" in text, f"{pipeline.name}: validation is not a regex match"


def _fleet_workloads(checkout: Path) -> dict[str, set[str]]:
    """Every Deployment `Chemclaw3-mcp` creates, mapped to the container names inside it.

    Read off the sibling's own manifests, since a name for another repository's object is a claim
    only that repository can confirm.
    """
    import yaml

    workloads: dict[str, set[str]] = {}
    for manifest in sorted(checkout.glob("servers/*/deploy/deployment.yaml")):
        for document in yaml.safe_load_all(manifest.read_text(encoding="utf-8")):
            if not isinstance(document, dict) or document.get("kind") != "Deployment":
                continue
            spec = document["spec"]["template"]["spec"]
            workloads[str(document["metadata"]["name"])] = {
                str(container["name"]) for container in spec["containers"]
            }
    return workloads


#: A component descriptor's `deployment:` value and the `container:` beside it, in either the Groovy
#: or the JSON spelling the pipelines and README use.
_COMPONENT_PAIR = re.compile(
    r"""["']?deployment["']?\s*:\s*["']([^"']*mcp-[^"']*)["']\s*,\s*"""
    r"""["']?container["']?\s*:\s*["']?([A-Za-z0-9_${}-]+)["']?""",
)


def _declared_fleet_workloads() -> dict[str, tuple[str, str]]:
    """Every `(deployment, container)` pair this repository declares for a fleet server."""
    declared: dict[str, tuple[str, str]] = {}
    for path in (*_PIPELINES, _JENKINS_DIR / "README.md"):
        for deployment, container in _COMPONENT_PAIR.findall(path.read_text(encoding="utf-8")):
            declared[f"{path.relative_to(_ROOT)}: {deployment}"] = (deployment, container)
    return declared


def test_a_release_patches_a_fleet_workload_that_exists() -> None:
    """A release patches a fleet workload that exists.

    The fleet's Deployments are `chemclaw-mcp-<name>` with a container called `server`, and a
    release naming anything else patches nothing. Checked against the sibling checkout; with none,
    this asserts nothing and says which pairs it did not check.
    """
    declared = _declared_fleet_workloads()
    assert declared, (
        "no (deployment, container) pair found for a fleet server — either the release descriptor "
        "stopped naming them, or `_COMPONENT_PAIR` no longer matches how it does"
    )

    checkout, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if checkout is None:
        listed = ", ".join(f"{d}/{c}" for d, c in sorted(declared.values()))
        pytest.skip(
            f"{SIBLING_SKIP} {reason}; NOT checked against the fleet's own workloads: {listed}"
        )

    workloads = _fleet_workloads(checkout)
    assert workloads, f"{checkout} declares no servers/*/deploy/deployment.yaml to compare against"

    servers = sorted(name.removeprefix("chemclaw-mcp-") for name in workloads)
    wrong: list[str] = []
    for where, (deployment, container) in sorted(declared.items()):
        # A Groovy template is checked against every server it will be rendered for; a literal
        # names one. Substituting each in turn covers both without parsing Groovy.
        for server in servers if "${name}" in deployment else [""]:
            rendered = deployment.replace("${name}", server)
            in_container = container.replace("${name}", server)
            # `container: name` in Groovy is the loop variable, which holds the server name.
            resolved = server if in_container == "name" and server else in_container
            if rendered not in workloads:
                wrong.append(
                    f"{where} patches Deployment {rendered!r}, and {checkout} creates no such "
                    f"workload — it creates {sorted(workloads)}"
                )
            elif resolved not in workloads[rendered]:
                wrong.append(
                    f"{where} patches container {resolved!r} in {rendered!r}, and that Deployment "
                    f"runs {sorted(workloads[rendered])}"
                )
    assert not wrong, "\n".join(dict.fromkeys(wrong))


def test_a_chartless_component_says_what_a_release_could_not_do(tmp_path: Path) -> None:
    """A chartless component says what a release cannot do to it.

    `Chemclaw3_ui` and the `Chemclaw3-mcp` servers ship an image and a NetworkPolicy but no chart,
    so a release can only change their bytes, not create them; a `NotFound` must say so rather than
    read as a deleted Deployment. Driven with a fake `oc` on PATH that refuses as a real one does.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "oc").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "set" ]; then\n'
        "  echo 'Error from server (NotFound): deployments.apps \"x\" not found' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    (fake_bin / "oc").chmod(0o755)
    (fake_bin / "helm").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "helm").chmod(0o755)

    descriptor = tmp_path / "release.json"
    descriptor.write_text(
        '{"environment": "probe", "components": {"ui": {"kind": "deployment", '
        '"deployment": "chemclaw-ui", "image": "reg/ui", "digest": "sha256:abc"}}}'
    )

    target = _JENKINS_DIR / "targets" / "openshift.sh"
    result = subprocess.run(
        ["bash", str(target), str(descriptor)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "NAMESPACE": "probe-ns",
            "DRY_RUN": "false",
        },
    )

    assert result.returncode != 0, "a failed `oc set image` must fail the release"
    assert "ships no chart" in result.stderr, (
        "the release left the operator with only `oc`'s NotFound, which names the symptom and not "
        f"the shape of a chartless component:\n{result.stderr}"
    )
    assert "cannot create the Deployment" in result.stderr
