"""`deploy/kind/`, the local cluster lane, agrees with the chart, the fleet and itself.

The files name each other: `values-kind.yaml` names Services `manifests/` and the fleet create,
`kind-config.yaml` maps host ports onto NodePorts, `up.sh` generates the Secret keys the chart
references, and the egress policy must admit every port dialled. Each mismatch is silent until a
cluster is up. `make kind-validate` schema-checks the documents; this checks they describe one
system. Rendered-chart tests need `helm` and fleet comparisons need the `Chemclaw3-mcp` checkout;
both skip without them, naming what they did not check.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from chemclaw.cli import kind_stale_images
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


# The two auth modes `up.sh` installs, as the values files it passes for each.
_MODES = {
    "devauth": ("values-kind.yaml",),
    "oidc-mock": ("values-kind.yaml", "values-kind-oidc-mock.yaml"),
}


@cache
def _rendered(mode: str = "devauth") -> list[dict[str, Any]]:
    """The chart under the kind overlay, exactly as `up.sh` installs it in `mode`."""
    files = [arg for name in _MODES[mode] for arg in ("-f", str(_KIND / name))]
    result = subprocess.run(
        ["helm", "template", "chemclaw", str(_CHART), "--namespace", "chemclaw", *files],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"the chart refuses the kind overlay ({mode}):\n{result.stderr}"
    return _documents(result.stdout)


def _config_map(mode: str = "devauth") -> dict[str, str]:
    """The release's ConfigMap data — what every pod reads as `CHEMCLAW_*`."""
    found = [
        doc
        for doc in _rendered(mode)
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "chemclaw-config"
    ]
    assert len(found) == 1, "the render has no chemclaw-config ConfigMap"
    data: dict[str, str] = found[0]["data"]
    return data


def _dialled(mode: str = "devauth") -> dict[str, tuple[str, int]]:
    """Every address the release dials, setting or connector name onto (host, port)."""
    config = _config_map(mode)
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
    if config.get("CHEMCLAW_ENTRA_JWKS_URL"):
        parts = urlsplit(config["CHEMCLAW_ENTRA_JWKS_URL"])
        assert parts.hostname and parts.port, "CHEMCLAW_ENTRA_JWKS_URL names no host and port"
        dialled["CHEMCLAW_ENTRA_JWKS_URL"] = (parts.hostname, parts.port)
    return dialled


def _service_ports(documents: list[dict[str, Any]]) -> dict[str, set[int]]:
    """Service name onto the ports it exposes."""
    return {
        doc["metadata"]["name"]: {int(port["port"]) for port in doc["spec"]["ports"]}
        for doc in documents
        if doc["kind"] == "Service"
    }


requires_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
each_mode = pytest.mark.parametrize("mode", sorted(_MODES))


@requires_helm
@each_mode
def test_the_overlay_renders_nothing_a_vanilla_cluster_cannot_serve(mode: str) -> None:
    """No OpenShift Route and no prometheus-operator or KEDA object reaches a kind cluster."""
    kinds = {doc["kind"] for doc in _rendered(mode)}
    leaked = sorted(kinds & _CLUSTER_OPERATOR_KINDS)
    assert not leaked, f"values-kind.yaml still renders {leaked}, which kind has no API for"
    assert {"Job", "Deployment", "NetworkPolicy"} <= kinds, f"implausible render: {sorted(kinds)}"


@requires_helm
@each_mode
def test_every_pod_runs_the_core_image_the_lane_loads(mode: str) -> None:
    """`imagePullPolicy: Never` and the image `up.sh` loads, or a pod waits on a registry."""
    for doc in _rendered(mode):
        if doc["kind"] not in {"Deployment", "Job"}:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec["containers"] + spec.get("initContainers", []):
            name = f"{doc['metadata']['name']}/{container['name']}"
            assert container["image"] == "chemclaw/core:kind", name
            assert container["imagePullPolicy"] == "Never", name


@requires_helm
@each_mode
def test_the_production_hook_jobs_still_run_in_their_production_order(mode: str) -> None:
    """The overlay keeps migrate before the rollout and convert/schedules after it."""
    hooks = {
        doc["metadata"]["name"]: doc["metadata"]["annotations"]["helm.sh/hook"]
        for doc in _rendered(mode)
        if doc["kind"] == "Job"
    }
    assert hooks == {
        "chemclaw-migrate": "pre-install,pre-upgrade,pre-rollback",
        "chemclaw-convert": "post-install,post-upgrade",
        "chemclaw-schedules": "post-install,post-upgrade",
    }


@requires_helm
@each_mode
def test_every_secret_key_the_release_requires_is_one_up_sh_generates(mode: str) -> None:
    """A required `secretKeyRef` the script never writes is a pod stuck before it starts."""
    required: set[str] = set()
    for doc in _rendered(mode):
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
    # And every Secret a pod mounts as a volume (oidc-mock's `chemclaw-temporal-tls`) is one up.sh
    # creates, or the pod waits in `ContainerCreating` on a mount that never resolves.
    mounted = {
        volume["secret"]["secretName"]
        for doc in _rendered(mode)
        if doc["kind"] in {"Deployment", "Job"}
        for volume in doc["spec"]["template"]["spec"].get("volumes") or []
        if "secret" in volume
    }
    created = set(re.findall(r"apply_secret ([a-z0-9-]+) ", _UP))
    assert mounted <= created, f"pods mount {sorted(mounted - created)}, which up.sh never creates"
    assert not missing, (
        f"the chart requires {missing} from chemclaw-secrets; up.sh never writes them"
    )


@requires_helm
@each_mode
def test_the_release_may_dial_every_port_it_is_configured_to_dial(mode: str) -> None:
    """The egress policy is enforced on kind (kindnet), so an unlisted port is a silent drop."""
    egress = [
        doc
        for doc in _rendered(mode)
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "chemclaw-egress"
    ]
    assert len(egress) == 1, "the render has no chemclaw-egress policy"
    allowed = {
        int(port["port"]) for rule in egress[0]["spec"]["egress"] for port in rule.get("ports", [])
    }
    blocked = sorted(
        f"{name} → {host}:{port}"
        for name, (host, port) in _dialled(mode).items()
        if port not in allowed and port != 5432
    )
    assert 5432 in allowed, "the release cannot reach Postgres"
    assert not blocked, f"configured but dropped by chemclaw-egress: {blocked}"


@requires_helm
@each_mode
def test_every_in_cluster_address_names_a_service_this_lane_creates(mode: str) -> None:
    """Each dialled host is a Service from the chart, `manifests/`, or the fleet, on that port."""
    services = _service_ports(_rendered(mode)) | _service_ports(_manifests())
    checkout, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    fleet_hosts = {host for host, _ in _dialled(mode).values() if host.startswith("chemclaw-mcp-")}
    if checkout is not None:
        for path in sorted(checkout.glob("servers/*/deploy/service.yaml")):
            services |= _service_ports(_documents(path.read_text(encoding="utf-8")))
    wrong = [
        f"{name} → {host}:{port}"
        for name, (host, port) in sorted(_dialled(mode).items())
        if (checkout is not None or host not in fleet_hosts)
        and port not in services.get(host, set())
    ]
    assert not wrong, f"addresses no Service in this lane answers: {wrong}"
    if checkout is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}; fleet addresses NOT checked: {sorted(fleet_hosts)}")


def test_the_host_ports_are_loopback_and_land_on_the_nodeports_manifests_declare() -> None:
    """`kind-config.yaml` and `manifests/` name the same NodePorts, and only on 127.0.0.1."""
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


def test_the_ui_sandbox_is_its_own_origin_and_the_origins_are_the_mapped_ports() -> None:
    """The html sandbox is a different hostname from the app, on its own listener's host port.

    The UI's BFF refuses a sandbox origin equal to the app's but cannot check that the origin it was
    given is what the browser uses, which is this lane's port mapping.
    """
    from urllib.parse import urlsplit

    docs = _manifests()
    [deployment] = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "ui"]
    [container] = deployment["spec"]["template"]["spec"]["containers"]
    env = {item["name"]: item.get("value") for item in container["env"]}
    sandbox, app = urlsplit(env["SANDBOX_ORIGIN"]), urlsplit(env["APP_ORIGIN"])
    assert sandbox.hostname != app.hostname, (sandbox, app)
    [service] = [d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "ui"]
    node_port = {int(p["targetPort"]): int(p["nodePort"]) for p in service["spec"]["ports"]}
    config = yaml.safe_load((_KIND / "kind-config.yaml").read_text(encoding="utf-8"))
    host_port = {
        int(m["containerPort"]): int(m["hostPort"]) for m in config["nodes"][0]["extraPortMappings"]
    }
    assert host_port[node_port[int(env["SANDBOX_PORT"])]] == sandbox.port
    assert host_port[node_port[int(env["PORT"])]] == app.port
    listening = {int(p["containerPort"]) for p in container["ports"]}
    assert {int(env["SANDBOX_PORT"]), int(env["PORT"])} <= listening


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
    """The LLM key travels sourced variable → pipe → Secret, never as an argument or a log line.

    `ps` shows process arguments, so `--from-literal` would expose the key on the machine.
    """
    assert "--from-literal=CHEMCLAW_LLM_API_KEY" not in _UP
    for line in _UP.splitlines():
        if re.match(r"\s*(log|warn|die|echo)\b", line):
            assert "LLM_KEY" not in line and "CHEMCLAW_LLM_API_KEY" not in line, line
    assert "--from-env-file=/dev/stdin" in _UP, "the Secret is no longer assembled on a pipe"


@requires_helm
def test_oidc_mock_is_the_shipped_identity_posture_against_the_mock_tenant() -> None:
    """oidc-mock turns sign-in on, and its prerequisites are what core demands.

    With `CHEMCLAW_ENTRA_REQUIRED=true` core refuses plaintext Temporal and a non-loopback Postgres
    DSN below `sslmode=require`, so the chart's Temporal mTLS mount and `up.sh`'s `sslmode=require`
    must both be present.
    """
    config = _config_map("oidc-mock")
    assert config["CHEMCLAW_ENTRA_REQUIRED"] == "true"
    assert config["CHEMCLAW_SERVICE_ALLOW_INSECURE"] == "false"
    assert config["CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED"] == "false"
    assert config["CHEMCLAW_ENTRA_PRIVILEGED_ROLES"], "every expensive job would be closed"
    issuer = urlsplit(config["CHEMCLAW_ENTRA_ISSUER"])
    assert issuer.scheme == "https", "MSAL.js accepts only an https authority"
    assert f"{issuer.scheme}://{issuer.netloc}/entra/mock-tenant" in _UP, (
        "up.sh hands the browser and the mock a different tenant URL than core checks `iss` against"
    )
    env = {
        env["name"]
        for doc in _rendered("oidc-mock")
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "chemclaw-service"
        for env in doc["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert {"CHEMCLAW_TEMPORAL_TLS_CERT", "CHEMCLAW_TEMPORAL_TLS_CA"} <= env
    assert "sslmode=require" in _UP
    # The UI is told the privileged role separately; a mismatch hides actions core would accept.
    assert f"REVIEWER_ROLES={config['CHEMCLAW_ENTRA_PRIVILEGED_ROLES']}" in _UP


def test_the_lane_scripts_are_executable_in_git() -> None:
    """The README runs `deploy/kind/up.sh up`, which a dropped executable bit refuses outright.

    The mode is what git records, not what this working tree happens to have, so it is read from the
    index: an editor that rewrites the file can clear the bit and the commit carries it silently.
    """
    if shutil.which("git") is None:
        pytest.skip("no git to read file modes with")
    listed = subprocess.run(
        ["git", "ls-files", "-s", "--", "deploy/kind", "src/chemclaw/cli"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        pytest.skip(f"not a readable git checkout: {listed.stderr.strip()}")
    modes = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in listed.stdout.splitlines()}
    for script in (
        "deploy/kind/up.sh",
        "deploy/kind/render-fleet.sh",
        "src/chemclaw/cli/kind_stale_images.py",
    ):
        assert modes.get(script) == "100755", f"{script} is not executable in git"


_OLD = "sha256:" + "a" * 64
_NEW = "sha256:" + "b" * 64
_NEW_ID = "sha256:" + "c" * 64


def _pod(deployment: str, containers: list[tuple[str, str]], statuses: list[dict[str, Any]]) -> Any:
    """A pod as `kubectl get pods -o json` lists it, owned by `<deployment>`'s ReplicaSet."""
    return {
        "metadata": {
            "name": f"{deployment}-5d9f7c-x1",
            "ownerReferences": [{"kind": "ReplicaSet", "name": f"{deployment}-5d9f7c"}],
        },
        "spec": {"containers": [{"name": n, "image": i} for n, i in containers]},
        "status": {"containerStatuses": statuses},
    }


def _current() -> dict[str, set[str]]:
    """The node after a core rebuild: the new repo digest and image id answer to the tag."""
    inspecti = {"status": {"repoDigests": [f"docker.io/chemclaw/core@{_NEW}"], "id": _NEW_ID}}
    return {
        "chemclaw/core:kind": kind_stale_images.node_digests(inspecti),
        "chemclaw/ui:kind": kind_stale_images.node_digests(None),  # the node could not be asked
    }


def test_a_pod_on_the_superseded_digest_is_stale_and_one_on_the_current_is_not() -> None:
    """The tag is the same either way; only the digest tells the two pods apart."""
    core = [("service", "chemclaw/core:kind")]
    pods = {
        "items": [
            _pod("old", core, [{"name": "service", "imageID": f"docker.io/chemclaw/core@{_OLD}"}]),
            _pod("new", core, [{"name": "service", "imageID": f"docker.io/chemclaw/core@{_NEW}"}]),
        ]
    }
    assert kind_stale_images.stale_deployments(pods, _current()) == ["old"]


@pytest.mark.parametrize(
    "image_id",
    [
        f"docker.io/chemclaw/core@{_NEW}",  # a repo digest
        _NEW_ID,  # the bare image id a `kind load`ed image is often started as
        f"docker-pullable://chemclaw/core@{_NEW}",  # a runtime prefix
    ],
)
def test_every_spelling_of_the_current_image_counts_as_current(image_id: str) -> None:
    """`imageID` is a repo digest or an image id depending on how the image reached the node."""
    pods = {
        "items": [_pod("svc", [("c", "chemclaw/core:kind")], [{"name": "c", "imageID": image_id}])]
    }
    assert kind_stale_images.stale_deployments(pods, _current()) == []


def test_containers_are_matched_to_their_statuses_by_name_not_position() -> None:
    """The worker runs two `chemclaw/` containers, and Kubernetes promises no order between lists.

    Here the statuses arrive reversed: matching by position would compare the sidecar's status
    against the worker's spec. Only the sidecar is old, and the pod is stale because of it.
    """
    containers = [
        ("background-worker", "chemclaw/core:kind"),
        ("knowledge-sync", "chemclaw/core:kind"),
    ]
    statuses = [
        {"name": "knowledge-sync", "imageID": f"docker.io/chemclaw/core@{_OLD}"},
        {"name": "background-worker", "imageID": f"docker.io/chemclaw/core@{_NEW}"},
    ]
    pods = {"items": [_pod("worker", containers, statuses)]}
    assert kind_stale_images.stale_deployments(pods, _current()) == ["worker"]

    statuses[0]["imageID"] = _NEW_ID
    assert kind_stale_images.stale_deployments(pods, _current()) == []


def test_what_cannot_be_compared_is_not_counted_stale() -> None:
    """No status, no `imageID` yet, a tag the node could not be asked about, a non-Deployment pod.

    Each would otherwise read as "not on a current digest" and restart pods for nothing.
    """
    core = [("c", "chemclaw/core:kind")]
    old = [{"name": "c", "imageID": f"docker.io/chemclaw/core@{_OLD}"}]
    unowned = _pod("job", core, old)
    unowned["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "migrate-abc12"}]
    pods = {
        "items": [
            _pod("creating", core, []),
            _pod("pulling", core, [{"name": "c", "imageID": ""}]),
            _pod("ui", [("ui", "chemclaw/ui:kind")], [{"name": "ui", "imageID": f"x@{_OLD}"}]),
            _pod("postgres", [("pg", "postgres:16")], [{"name": "pg", "imageID": f"x@{_OLD}"}]),
            unowned,
        ]
    }
    assert kind_stale_images.stale_deployments(pods, _current()) == []


def test_only_chemclaw_images_are_asked_about() -> None:
    """The node is asked once per distinct `chemclaw/` tag, never about a third-party image."""
    pods = {
        "items": [
            _pod("a", [("c", "chemclaw/core:kind"), ("s", "chemclaw/core:kind")], []),
            _pod("b", [("pg", "postgres:16"), ("ui", "chemclaw/ui:kind")], []),
        ]
    }
    assert kind_stale_images.chemclaw_images(pods) == ["chemclaw/core:kind", "chemclaw/ui:kind"]


def test_the_stale_image_check_needs_nothing_but_the_standard_library() -> None:
    """`up.sh` runs it as a file on the host's `python3`, where no dependency is installed."""
    tree = ast.parse(Path(kind_stale_images.__file__).read_text(encoding="utf-8"))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        for name in (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
    assert "src/chemclaw/cli/kind_stale_images.py" in _UP
