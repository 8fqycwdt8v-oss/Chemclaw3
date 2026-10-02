"""`deploy/kind/` — the local cluster lane — agrees with the chart, the fleet and itself.

The lane is a set of files that each name the others: `values-kind.yaml` names Service addresses
that `manifests/` and the sibling fleet create, `kind-config.yaml` maps host ports onto NodePorts
that `manifests/` declares, `up.sh` generates the Secret keys the rendered chart references, and
the release's egress policy has to admit every port the release dials. Each pair is a string in two
files, and each failure is silent until a cluster is up: a mistyped Service is a connector that
never answers, a missing Secret key is a pod stuck in `CreateContainerConfigError`, a port the
egress policy omits drops every packet. `make kind-validate` schema-checks the documents; this file
checks what no schema can — that they describe one system.

The rendered-chart tests need `helm` and skip without it (`tests/conftest.py` counts the skips); the
fleet comparisons need the `Chemclaw3-mcp` checkout and skip without it, naming what they did not
check.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from tests.siblings import SIBLING_SKIP, sibling_root

_ROOT = Path(__file__).resolve().parents[1]
_KIND = _ROOT / "deploy" / "kind"
_CHART = _ROOT / "deploy" / "helm" / "chemclaw"
_UP = (_KIND / "up.sh").read_text(encoding="utf-8")

# Kinds a vanilla API server does not serve. A kind overlay that renders one fails at `helm install`
# with "no matches for kind", after the pre-install hook has already migrated the database.
_CLUSTER_OPERATOR_KINDS = {
    "Route",
    "ServiceMonitor",
    "PodMonitor",
    "PrometheusRule",
    "AlertmanagerConfig",
    "ScaledObject",
}


def _documents(text: str) -> list[dict[str, Any]]:
    """Every non-empty YAML document in a multi-document stream."""
    return [doc for doc in yaml.safe_load_all(text) if doc]


def _manifests() -> list[dict[str, Any]]:
    """Every object `deploy/kind/manifests/` declares."""
    return [
        doc
        for path in sorted((_KIND / "manifests").glob("*.yaml"))
        for doc in _documents(path.read_text(encoding="utf-8"))
    ]


@cache
def _rendered() -> list[dict[str, Any]]:
    """The chart under the kind overlay, exactly as `up.sh` installs it (mock LLM, all images)."""
    result = subprocess.run(
        [
            "helm",
            "template",
            "chemclaw",
            str(_CHART),
            "--namespace",
            "chemclaw",
            "-f",
            str(_KIND / "values-kind.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"the chart refuses the kind overlay:\n{result.stderr}"
    return _documents(result.stdout)


def _config_map() -> dict[str, str]:
    """The release's ConfigMap data — what every pod reads as `CHEMCLAW_*`."""
    found = [
        doc
        for doc in _rendered()
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "chemclaw-config"
    ]
    assert len(found) == 1, "the render has no chemclaw-config ConfigMap"
    data: dict[str, str] = found[0]["data"]
    return data


def _dialled() -> dict[str, tuple[str, int]]:
    """Every address the release dials, setting or connector name onto (host, port)."""
    config = _config_map()
    urls: dict[str, str] = dict(json.loads(config["CHEMCLAW_CONNECTOR_URLS"]))
    for key in (
        "CHEMCLAW_LLM_BASE_URL",
        "CHEMCLAW_CALC_SERVER_URL",
        "CHEMCLAW_RXNLABEL_SERVER_URL",
    ):
        urls[key] = config[key]
    dialled: dict[str, tuple[str, int]] = {}
    for name, url in urls.items():
        parts = urlsplit(url)
        assert parts.hostname and parts.port, f"{name}={url} names no host and port"
        dialled[name] = (parts.hostname, parts.port)
    host, port = config["CHEMCLAW_TEMPORAL_ADDRESS"].rsplit(":", 1)
    dialled["CHEMCLAW_TEMPORAL_ADDRESS"] = (host, int(port))
    return dialled


def _service_ports(documents: list[dict[str, Any]]) -> dict[str, set[int]]:
    """Service name onto the ports it exposes."""
    return {
        doc["metadata"]["name"]: {int(port["port"]) for port in doc["spec"]["ports"]}
        for doc in documents
        if doc["kind"] == "Service"
    }


requires_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


@requires_helm
def test_the_overlay_renders_nothing_a_vanilla_cluster_cannot_serve() -> None:
    """No OpenShift Route and no prometheus-operator or KEDA object reaches a kind cluster."""
    kinds = {doc["kind"] for doc in _rendered()}
    leaked = sorted(kinds & _CLUSTER_OPERATOR_KINDS)
    assert not leaked, f"values-kind.yaml still renders {leaked}, which kind has no API for"
    assert {"Job", "Deployment", "NetworkPolicy"} <= kinds, f"implausible render: {sorted(kinds)}"


@requires_helm
def test_every_pod_runs_the_core_image_the_lane_loads() -> None:
    """`imagePullPolicy: Never` and the image `up.sh` loads, or a pod waits on a registry."""
    for doc in _rendered():
        if doc["kind"] not in {"Deployment", "Job"}:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec["containers"] + spec.get("initContainers", []):
            name = f"{doc['metadata']['name']}/{container['name']}"
            assert container["image"] == "chemclaw/core:kind", name
            assert container["imagePullPolicy"] == "Never", name


@requires_helm
def test_the_production_hook_jobs_still_run_in_their_production_order() -> None:
    """The overlay keeps migrate before the rollout and convert/schedules after it."""
    hooks = {
        doc["metadata"]["name"]: doc["metadata"]["annotations"]["helm.sh/hook"]
        for doc in _rendered()
        if doc["kind"] == "Job"
    }
    assert hooks == {
        "chemclaw-migrate": "pre-install,pre-upgrade,pre-rollback",
        "chemclaw-convert": "post-install,post-upgrade",
        "chemclaw-schedules": "post-install,post-upgrade",
    }


@requires_helm
def test_every_secret_key_the_release_requires_is_one_up_sh_generates() -> None:
    """A required `secretKeyRef` the script never writes is a pod stuck before it starts."""
    required: set[str] = set()
    for doc in _rendered():
        if doc["kind"] not in {"Deployment", "Job"}:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec["containers"] + spec.get("initContainers", []):
            for env in container.get("env", []):
                ref = env.get("valueFrom", {}).get("secretKeyRef")
                if ref and ref["name"] == "chemclaw-secrets" and not ref.get("optional"):
                    required.add(ref["key"])
    assert required, "the render references no chemclaw-secrets key — the scan did not parse"
    generated = set(re.findall(r"printf '(CHEMCLAW_[A-Z0-9_]+)=", _UP))
    missing = sorted(required - generated)
    assert not missing, (
        f"the chart requires {missing} from chemclaw-secrets; up.sh never writes them"
    )


@requires_helm
def test_the_release_may_dial_every_port_it_is_configured_to_dial() -> None:
    """The egress policy is enforced on kind (kindnet), so an unlisted port is a silent drop."""
    egress = [
        doc
        for doc in _rendered()
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "chemclaw-egress"
    ]
    assert len(egress) == 1, "the render has no chemclaw-egress policy"
    allowed = {
        int(port["port"]) for rule in egress[0]["spec"]["egress"] for port in rule.get("ports", [])
    }
    blocked = sorted(
        f"{name} → {host}:{port}"
        for name, (host, port) in _dialled().items()
        if port not in allowed and port != 5432
    )
    assert 5432 in allowed, "the release cannot reach Postgres"
    assert not blocked, f"configured but dropped by chemclaw-egress: {blocked}"


@requires_helm
def test_every_in_cluster_address_names_a_service_this_lane_creates() -> None:
    """Each dialled host is a Service from the chart, `manifests/`, or the fleet, on that port."""
    services = _service_ports(_rendered()) | _service_ports(_manifests())
    checkout, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    fleet_hosts = {host for host, _ in _dialled().values() if host.startswith("chemclaw-mcp-")}
    if checkout is not None:
        for path in sorted(checkout.glob("servers/*/deploy/service.yaml")):
            services |= _service_ports(_documents(path.read_text(encoding="utf-8")))
    wrong = [
        f"{name} → {host}:{port}"
        for name, (host, port) in sorted(_dialled().items())
        if (checkout is not None or host not in fleet_hosts)
        and port not in services.get(host, set())
    ]
    assert not wrong, f"addresses no Service in this lane answers: {wrong}"
    if checkout is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}; fleet addresses NOT checked: {sorted(fleet_hosts)}")


def test_the_host_ports_are_loopback_and_land_on_the_nodeports_manifests_declare() -> None:
    """`kind-config.yaml` and `manifests/` name the same three NodePorts, and only on 127.0.0.1."""
    config = yaml.safe_load((_KIND / "kind-config.yaml").read_text(encoding="utf-8"))
    mappings = config["nodes"][0]["extraPortMappings"]
    assert all(mapping["listenAddress"] == "127.0.0.1" for mapping in mappings), mappings
    mapped = {int(mapping["containerPort"]) for mapping in mappings}
    node_ports = {
        int(port["nodePort"])
        for doc in _manifests()
        if doc["kind"] == "Service" and doc["spec"].get("type") == "NodePort"
        for port in doc["spec"]["ports"]
    }
    assert mapped == node_ports, (
        f"kind maps {sorted(mapped)}; manifests expose {sorted(node_ports)}"
    )
    host_ports = {int(mapping["hostPort"]) for mapping in mappings}
    # The ports the compose lanes and the cc3-live container keep on the same workstation.
    assert not host_ports & {5432, 5173, 8000, 8091}, host_ports


def test_up_sh_deploys_every_server_the_fleet_ships_a_deployment_for() -> None:
    """`up.sh`'s FLEET list is the fleet's own `servers/*/deploy/deployment.yaml` set."""
    match = re.search(r"readonly -a FLEET=\(([^)]*)\)", _UP)
    assert match, "up.sh no longer declares FLEET=( … )"
    listed = set(match.group(1).split())
    checkout, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if checkout is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}; FLEET NOT checked: {sorted(listed)}")
    shipped = {path.parts[-3] for path in checkout.glob("servers/*/deploy/deployment.yaml")}
    assert listed == shipped, (
        f"up.sh deploys {sorted(listed - shipped)} the fleet does not ship and misses "
        f"{sorted(shipped - listed)}"
    )


def test_the_llm_key_never_becomes_an_argument_or_a_log_line() -> None:
    """The live-mode key travels sourced variable → pipe → Secret, and nowhere else.

    `ps` shows every argument of a running process, so a `--from-literal` would publish the key to
    every user on the machine for as long as `kubectl` runs; a `log` line would put it in a
    terminal's scrollback. Neither is a hypothetical shape for a script that builds a Secret.
    """
    assert "--from-literal=CHEMCLAW_LLM_API_KEY" not in _UP
    for line in _UP.splitlines():
        if re.match(r"\s*(log|warn|die|echo)\b", line):
            assert "LLM_KEY" not in line and "CHEMCLAW_LLM_API_KEY" not in line, line
    assert "--from-env-file=/dev/stdin" in _UP, "the Secret is no longer assembled on a pipe"
