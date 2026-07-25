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
    # Templated names (e.g. "mcp-{{ $name }}") are checked by their prefix instead.
    concrete = {name for name in declared if "{{" not in name}
    assert concrete <= cases, f"components with no entrypoint case: {sorted(concrete - cases)}"


def test_image_ships_every_directory_the_components_read() -> None:
    """Directories read at runtime (`skills/`, `scripts/`, `evals/`, `knowledge/`) must ship.

    Their absence is invisible offline: the agent simply advertises no skills, no Schedule is ever
    created, and the drift job has no case-set — each a silent capability loss, not a crash.
    """
    containerfile = (DEPLOY / "Containerfile").read_text()
    copied = set(re.findall(r"^COPY\s+(\S+)\s", containerfile, flags=re.MULTILINE))
    for required in ("skills", "scripts", "evals", "knowledge", "chemclaw", "agents", "service"):
        assert required in copied, f"Containerfile never COPYs {required}/"


def test_image_installs_git() -> None:
    """The PR-gate shells out to git; an image without it fails every knowledge write at push."""
    assert "dnf install -y git" in (DEPLOY / "Containerfile").read_text()


def test_mcp_standalone_pods_require_a_networked_transport() -> None:
    """Stdio MCP servers must not be deployed as standalone pods — they would crash-loop (DEP-3)."""
    values = _values()
    assert values["mcp"]["transport"] == "stdio"
    for name in ("molfp", "rxnfp"):
        assert values["mcp"][name]["enabled"] is False, f"{name} would run stdio with no stdin"
    guard = (CHART / "templates" / "deployment-mcp.yaml").read_text()
    assert 'eq $.Values.mcp.transport "http"' in guard


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
    # The hpc worker routes note writes to the background queue and must NOT get one.
    workers = (CHART / "templates" / "deployment-workers.yaml").read_text()
    hpc = workers.split("---")[0]
    assert 'include "chemclaw.noteRepoInit"' not in hpc


def test_schedules_are_applied_by_a_post_install_hook() -> None:
    """Without this Job no Temporal Schedule exists, so no periodic job ever fires (DEP-5)."""
    job = (CHART / "templates" / "schedules-job.yaml").read_text()
    assert '"helm.sh/hook": post-install,post-upgrade' in job
    assert '"python", "-m", "scripts.schedules"' in job


def test_push_credential_is_declared() -> None:
    """Every agent-authored note fails at push without a git credential in the chart (DEP-2)."""
    assert "knowledgeRepoToken" in _values()["secrets"]["keys"]


def test_mcp_deployments_become_meaningful_once_the_transport_is_networked() -> None:
    """DEP-3's guard is now satisfiable rather than a permanent off switch (gap TOOL-1).

    Before the streamable-HTTP client existed, `transport: http` was a flag with nothing behind it.
    The agent can now attach a server it does not spawn, which is what the MCP Deployments were
    written in anticipation of.
    """
    from chemclaw.config import HttpMcpServerSpec

    networked = HttpMcpServerSpec(name="mcp-molfp", url="http://chemclaw-mcp-molfp:8080/mcp")
    assert networked.transport == "http"
    assert networked.url
