"""The delivery pipelines describe this repository; these are the halves a file can check.

A Jenkinsfile cannot be run here — there is no controller, no registry and no cluster — so the
temptation is to check nothing and call the pipeline "prepared". That is exactly the shape this
repository keeps finding and removing: a control that exists, is described in the present tense,
and is never exercised (`mcp_servers/calc/` asserted deleted across four ADRs while still
dispatchable; `audit_events.agent` empty on every row ever written).

What *is* checkable offline is every claim the pipelines make about **this tree**:

- a `make` target they invoke exists (the pipeline's own `make ci` was the drift that D-117 found
  in the GitHub workflows, in the other direction);
- a script they call exists and parses;
- the deploy path passes a **digest** rather than a tag, which is the one property
  `deploy/helm/chemclaw/values.yaml` builds its release knob around;
- `DRY_RUN` defaults to true, because a delivery pipeline whose first run mutates a namespace is
  one nobody can safely try.

Deliberately not checked: whether any of it works against a cluster. Nothing here can know that, and
`deploy/jenkins/README.md` says so in the file rather than implying otherwise by testing around it.
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

    Three substitutions, and each is a real difference rather than a formality. `${...}` is
    interpolated by Jenkins before the shell sees anything. `\${...}` and `\$(...)` reach the shell
    verbatim — that escape is how a pipeline writes a *shell* variable inside an interpolated
    string, and getting it backwards is the most common way one of these files breaks. A `\\` at
    end of line reaches it as the single backslash that makes a line continuation.
    """
    resolved = re.sub(r"(?<!\\)\$\{[^}]*\}", "PLACEHOLDER", block)
    return resolved.replace("\\$", "$").replace("\\\\", "\\")


def _parses_as_shell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)


def test_every_shell_block_in_the_pipelines_parses() -> None:
    """The one thing that can be executed about a pipeline nobody here can run.

    A Jenkinsfile is checked by no compiler and no linter in this repository, and its shell bodies
    are strings — so an unbalanced quote, or a `||` left on its own line by a lost continuation, is
    invisible until a run, against a registry, on the way to a namespace. `bash -n` costs
    milliseconds and speaks about the text the shell is handed rather than the text in the file.
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
    """`image.digest` is the chart's release knob; a tag would reintroduce the hole it closed.

    `values.yaml` ignores `image.tag` entirely when a digest is set, because `helm rollback` to a
    release naming a re-pushed tag fetches bytes nobody reviewed, and every audit record stamps a
    build revision that stops being answerable at the same moment
    (D-2026-08-01-a-tag-is-a-pointer-not-a-build).
    """
    target = (_JENKINS_DIR / "targets" / "openshift.sh").read_text(encoding="utf-8")
    assert "image.digest" in target, "the helm path no longer sets image.digest"
    assert "--set image.tag" not in target, (
        "the helm path deploys a tag, which the chart's own release knob refuses"
    )
    assert "@${digest}" in target, "the Deployment path no longer pins the image by digest"


def test_a_release_states_its_egress_posture() -> None:
    """An unstated posture renders `to: []`, which a NetworkPolicy reads as every destination.

    The chart refuses to render without one
    (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`).
    The target must refuse too rather than quietly supplying the permissive answer to get past it.
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
    """Free-text parameters are interpolated into `sh`, so each must be allowlisted before use.

    A `string(...)` parameter can hold any text, and both pipelines interpolate several of them
    (`${params.IMAGE_REGISTRY}`, `${params.NAMESPACE}`, ...) into `sh` blocks and image refs, where
    a shell metacharacter would run on the agent (CWE-78). The choice/boolean parameters cannot —
    Jenkins fixes their values. So the invariant, for *each* pipeline: every free-text parameter is
    checked against a conservative allowlist in a `Validate parameters` stage, before any `sh` sees
    it. A new free-text parameter that skips the stage fails this test, not the next release.
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

    Read off the sibling's own manifests rather than restated here, for the reason the whole of
    this wave's fleet-seam work rests on: a name this repository writes down for an object another
    repository creates is a claim about that repository, and the only evidence about it is there.
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


#: A component descriptor's `deployment:` value, and the `container:` beside it.
#:
#: Both pipelines and the README write the pair within a few tokens of each other, in Groovy
#: (`deployment: "chemclaw-mcp-${name}", container: 'server'`) and in JSON (`"deployment": "…",
#: "container": "…"`). One pattern reads both because the question is the same in either syntax.
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
    """The release descriptor names objects in another repository, so measure them there.

    `Jenkinsfile.release` built every MCP component as `deployment: "chemclaw3-mcp-${name}",
    container: name` — and the fleet's Deployments are `chemclaw-mcp-<name>` with the container
    called `server` in all seven. So a release patching image digests targeted a Deployment that
    does not exist and, inside it, a container that does not exist either. The image reference on
    the next line was already spelled correctly, which is precisely what made the mismatch
    invisible to a reader: two adjacent lines, one right, one wrong, about the same server.

    This is the third place one repository wrote down a name the other owns — after the chart's
    five `chemclaw3-mcp-*` addresses and its own prose asserting the rule those five broke — which
    is why it is checked rather than corrected and left to drift again.

    **A skip is not a pass**: with no fleet checkout this asserts nothing and says which pairs it
    therefore did not check.
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
