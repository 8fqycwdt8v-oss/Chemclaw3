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


def test_a_release_states_its_retention_posture_too() -> None:
    """The chart refuses to render on **two** postures; the delivery path pre-flighted one.

    `D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` put a `fail` in
    `templates/networkpolicy.yaml` *and* one in `templates/config.yaml`, and every other caller
    treats them as a pair — the Makefile's three renders, the runbook and `deploy/README.md` all
    pass both flags. This target grew `egress_flags` for the first and nothing at all for the
    second: `grep -rn retention deploy/jenkins/` returned nothing.

    The cost is not a silent one — helm prints its own `fail` — it is *where*: the egress twin is
    caught in a pre-flight that names both remedies, and the retention one surfaces from inside
    `helm upgrade`, after the image has been built and pushed, in a pipeline whose parameters
    offered no way to state the posture at all. That is the lesson-written-too-narrowly shape
    `D-2026-08-28-a-lane-primitive-must-verify-the-act-it-was-asked-for` describes, one repository
    layer out.

    Asserted with the same three claims as its egress sibling above, because the two guards are
    one decision and a check that covers half of it is how they came to be treated differently.
    """
    target = (_JENKINS_DIR / "targets" / "openshift.sh").read_text(encoding="utf-8")
    assert "ACCEPT_UNBOUNDED_GROWTH" in target, (
        "no way to state the unbounded-growth posture deliberately"
    )
    assert "retention.unboundedGrowthAccepted=true" in target
    assert "ACCEPT_UNBOUNDED_GROWTH:-false" in target, (
        "the unbounded posture must be opt-in; defaulting it on ships tables that grow for the "
        "life of the deployment under a comment naming retention as the bound"
    )
    for pipeline in _PIPELINES:
        assert "ACCEPT_UNBOUNDED_GROWTH" in pipeline.read_text(encoding="utf-8"), (
            f"{pipeline.name} cannot state a retention posture, so its chart render refuses for a "
            "release that has not written one into its values file"
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
