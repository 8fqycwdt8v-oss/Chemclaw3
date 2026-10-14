#!/usr/bin/env bash
# Render `Chemclaw3-mcp`'s per-server workloads for this kind cluster, to stdout.
#
#   render-fleet.sh <Chemclaw3-mcp checkout> <image tag> [server ...]
#
# **Derived, not rewritten.** The checkout is rendered from whichever description it carries, and
# the output is the same three objects per server either way (Deployment, Service, NetworkPolicy):
#
# - `deploy/helm/chemclaw-fleet` when it exists: `helm template` with values for what a kind
#   cluster has to differ in, then `kubectl kustomize` for the two things the chart has no value
#   for (the namespace, `enableServiceLinks: false`);
# - otherwise the legacy `servers/<name>/deploy/{deployment,service,networkpolicy}.yaml`, used
#   exactly as shipped and patched by kustomize alone.
#
# Both keep the fleet's probes, hardened security context, grace period derived from
# `connector.yaml` and default-deny NetworkPolicy, and change only what a workstation needs:
#
# - the image: the fleet's unresolvable placeholder `registry.invalid/chemclaw-mcp-<name>:unset` →
#   `chemclaw/mcp-<name>:<tag>`, loaded into the node, so `imagePullPolicy: Never`;
# - one replica (the fleet's floor of two is an availability property; this is a workstation);
# - `MCP_ALLOWED_HOSTS`, the Service address the server is dialled at. The MCP transport's
#   DNS-rebinding guard admits only loopback `Host` headers by default, so without it every
#   in-cluster call is answered `421 Misdirected Request` while `/healthz` stays green;
# - resource *requests* sized to the node. Limits are the fleet's own.
#
# The HPA, PDB, ServiceMonitor and KEDA objects are left out: autoscaling has no metrics source
# here and a ServiceMonitor is a prometheus-operator CRD. The chart path asks for the three
# templates it wants (`--show-only`); the legacy path copies the three files.
#
# The server list defaults to every `servers/*/` the checkout deploys. Service names and ports are
# the fleet's, which are what core's `<name>_server_url` settings and the chart's
# `connectors.<name>.url` values dial (`tests/test_helm_chart.py` holds those against the sibling).
set -euo pipefail

die() { printf 'render-fleet: %s\n' "$*" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: render-fleet.sh <Chemclaw3-mcp checkout> <image tag> [server ...]"
mcp_repo="$1"; tag="$2"; shift 2
[ -d "$mcp_repo/servers" ] || die "no servers/ under $mcp_repo — is that a Chemclaw3-mcp checkout?"

chart="$mcp_repo/deploy/helm/chemclaw-fleet"
if [ -d "$chart" ]; then layout=chart; deployed() { [ -f "$1/connector.yaml" ]; }
else layout=legacy; deployed() { [ -f "$1/deploy/deployment.yaml" ]; }
fi

# Every server directory of the checkout this layout deploys, whether or not it was asked for.
all_servers=()
for dir in "$mcp_repo"/servers/*/; do
  if deployed "${dir%/}"; then all_servers+=("$(basename "$dir")"); fi
done
[ ${#all_servers[@]} -gt 0 ] || die "no deployable server under $mcp_repo/servers ($layout layout)"

if [ $# -gt 0 ]; then servers=("$@"); else servers=("${all_servers[@]}"); fi

# Memory requests from the fleet's own model sizes: rxnpredict loads eleven predictors, rxnlabel a
# mapper, calc runs xtb. Everything else is a small stateless server.
memory_request() {
  case "$1" in
    rxnpredict) echo 768Mi ;;
    rxnlabel) echo 256Mi ;;
    calc) echo 256Mi ;;
    *) echo 128Mi ;;
  esac
}

# The name the server itself verifies, read from its app rather than assumed from the pattern. Both
# layouts read that variable from `chemclaw-secrets` under its own name, and `up.sh` provisions
# `CHEMCLAW_<NAME>_TOKEN` keys, so a server whose `token_env` drifted would reference a key nothing
# wrote and never start.
check_token_env() {
  local name="$1" token_env
  token_env="CHEMCLAW_$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')_TOKEN"
  grep -rqs "token_env=\"$token_env\"" "$mcp_repo/servers/$name/src" \
    || die "$name: its app does not declare token_env=\"$token_env\" — check servers/$name/src"
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

render_chart() {
  command -v helm >/dev/null || die "helm is not installed (the fleet ships a chart: $chart)"
  local name known args wanted
  # Values only, so the chart needs no kind-specific switch: the lane's image names, no pull, one
  # replica, requests sized to the node. A server not asked for is nulled out of the table.
  args=(--set-string "defaults.image.registry=chemclaw"
    --set-string "defaults.image.namePrefix=mcp-"
    --set-string "defaults.image.tag=$tag"
    --set-string "defaults.image.pullPolicy=Never"
    --set "defaults.replicas=1")
  # With no autoscaler rendered (the lane applies only the workload templates) the replica count has
  # to be the Deployment's own, so it is asked for where the chart can say it: a chart whose
  # `autoscaling.mode` accepts `none` omits `replicas` otherwise and leaves it to the cluster's
  # default. Detected from the chart's schema rather than from a version; a chart without the mode
  # always renders `replicas`, so `defaults.replicas=1` already does it.
  if grep -Eqs '"mode"[^}]*"none"' "$chart/values.schema.json"; then
    args+=(--set "autoscaling.mode=none")
  fi
  for name in "${servers[@]}"; do
    known=""
    for wanted in "${all_servers[@]}"; do [ "$wanted" = "$name" ] && known=1; done
    [ -n "$known" ] || die "$name: not a server of $mcp_repo (it deploys: ${all_servers[*]})"
    check_token_env "$name"
    args+=(--set-string "servers.$name.resources.requests.cpu=25m"
      --set-string "servers.$name.resources.requests.memory=$(memory_request "$name")")
  done
  for wanted in "${all_servers[@]}"; do
    known=""
    for name in "${servers[@]}"; do [ "$wanted" = "$name" ] && known=1; done
    [ -n "$known" ] || args+=(--set "servers.$wanted=null")
  done
  helm template chemclaw-fleet "$chart" --namespace chemclaw "${args[@]}" \
    --show-only templates/deployment.yaml \
    --show-only templates/service.yaml \
    --show-only templates/networkpolicy.yaml >"$work/rendered.yaml"
  # The chart emits no namespace (it installs into the release's), and a kind pod should not get a
  # Service-link environment for every Service in the namespace.
  cat >"$work/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: chemclaw
resources:
  - rendered.yaml
patches:
  - target:
      kind: Deployment
    patch: |-
      - op: add
        path: /spec/template/spec/enableServiceLinks
        value: false
EOF
  kubectl kustomize "$work"
}

render_legacy() {
  local name src port out
  for name in "${servers[@]}"; do
    src="$mcp_repo/servers/$name/deploy"
    [ -f "$src/deployment.yaml" ] || die "$name: no $src/deployment.yaml"
    check_token_env "$name"
    # The Service's own port, read from the file the cluster gets, so the allowed `Host` is exactly
    # the address core dials (`http://chemclaw-mcp-<name>:<port>/mcp`).
    port="$(awk '$1 == "port:" {print $2; exit}' "$src/service.yaml")"
    [ -n "$port" ] || die "$name: no port in $src/service.yaml"
    # The placeholder the `images:` rewrite below matches. A Deployment naming anything else would
    # pass through unrewritten and sit in ErrImagePull, so refuse it here, by name.
    grep -qs "image: registry.invalid/chemclaw-mcp-$name:" "$src/deployment.yaml" \
      || die "$name: $src/deployment.yaml does not use the registry.invalid/chemclaw-mcp-$name placeholder"
    out="$work/$name"
    mkdir -p "$out"
    cp "$src/deployment.yaml" "$src/service.yaml" "$out/"
    [ -f "$src/networkpolicy.yaml" ] && cp "$src/networkpolicy.yaml" "$out/"
    cat >"$out/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: chemclaw
resources:
$(for f in deployment.yaml service.yaml networkpolicy.yaml; do [ -f "$out/$f" ] && echo "  - $f"; done)
images:
  - name: registry.invalid/chemclaw-mcp-$name
    newName: chemclaw/mcp-$name
    newTag: "$tag"
replicas:
  - name: chemclaw-mcp-$name
    count: 1
patches:
  - target:
      kind: Deployment
      name: chemclaw-mcp-$name
    patch: |-
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: chemclaw-mcp-$name
      spec:
        template:
          spec:
            enableServiceLinks: false
            containers:
              - name: server
                imagePullPolicy: Never
                env:
                  - name: MCP_ALLOWED_HOSTS
                    value: "chemclaw-mcp-$name:$port"
                resources:
                  requests:
                    cpu: 25m
                    memory: $(memory_request "$name")
EOF
    echo "---"
    kubectl kustomize "$out"
  done
}

if [ "$layout" = chart ]; then render_chart; else render_legacy; fi
