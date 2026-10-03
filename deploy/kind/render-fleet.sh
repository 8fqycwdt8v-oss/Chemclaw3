#!/usr/bin/env bash
# Render `Chemclaw3-mcp`'s own per-server manifests for this kind cluster, to stdout.
#
#   render-fleet.sh <Chemclaw3-mcp checkout> <image tag> [server ...]
#
# **Derived, not rewritten.** Each server's `servers/<name>/deploy/{deployment,service,
# networkpolicy}.yaml` is used exactly as the fleet ships it — its probes, its hardened security
# context, its grace period derived from `connector.yaml`, its default-deny NetworkPolicy — and
# `kubectl kustomize` applies only what a kind cluster has to differ in:
#
# - the image: `chemclaw3/chemclaw-mcp-<name>:latest` → `chemclaw/mcp-<name>:<tag>`, loaded into
#   the node, so `imagePullPolicy: Never`;
# - one replica (the fleet's floor of two is an availability property; this is a workstation);
# - the server's bearer, from `chemclaw-secrets` under the variable its `app.py` names as
#   `token_env` — the same key core's pods send from, so the two halves cannot disagree;
# - `MCP_ALLOWED_HOSTS`, the Service address the server is dialled at. The MCP transport's
#   DNS-rebinding guard admits only loopback `Host` headers by default, so without it every
#   in-cluster call is answered `421 Misdirected Request` while `/healthz` stays green;
# - resource *requests* sized to the node. Limits are the fleet's own.
#
# The HPA, PDB, ServiceMonitor and KEDA objects beside them are left out: autoscaling has no
# metrics source here and a ServiceMonitor is a prometheus-operator CRD.
#
# The server list defaults to every `servers/*/` that has a `deploy/deployment.yaml`. Service names
# and ports are the fleet's, which are what core's `<name>_server_url` settings and the chart's
# `connectors.<name>.url` values dial (`tests/test_helm_chart.py` holds those against the sibling).
set -euo pipefail

die() { printf 'render-fleet: %s\n' "$*" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: render-fleet.sh <Chemclaw3-mcp checkout> <image tag> [server ...]"
mcp_repo="$1"; tag="$2"; shift 2
[ -d "$mcp_repo/servers" ] || die "no servers/ under $mcp_repo — is that a Chemclaw3-mcp checkout?"

if [ $# -gt 0 ]; then
  servers=("$@")
else
  servers=()
  for dir in "$mcp_repo"/servers/*/; do
    [ -f "$dir/deploy/deployment.yaml" ] && servers+=("$(basename "$dir")")
  done
fi
[ ${#servers[@]} -gt 0 ] || die "no server with a deploy/deployment.yaml under $mcp_repo/servers"

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

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for name in "${servers[@]}"; do
  src="$mcp_repo/servers/$name/deploy"
  [ -f "$src/deployment.yaml" ] || die "$name: no $src/deployment.yaml"
  token_env="CHEMCLAW_$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')_TOKEN"
  # The name the server itself verifies, read from its app rather than assumed from the pattern:
  # a server whose `token_env` drifted from `CHEMCLAW_<NAME>_TOKEN` would otherwise boot healthy
  # and refuse every call.
  grep -rqs "token_env=\"$token_env\"" "$mcp_repo/servers/$name/src" \
    || die "$name: its app does not declare token_env=\"$token_env\" — check servers/$name/src"
  # The Service's own port, read from the file the cluster gets, so the allowed `Host` is exactly
  # the address core dials (`http://chemclaw-mcp-<name>:<port>/mcp`).
  port="$(awk '$1 == "port:" {print $2; exit}' "$src/service.yaml")"
  [ -n "$port" ] || die "$name: no port in $src/service.yaml"
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
  - name: chemclaw3/chemclaw-mcp-$name
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
                  - name: $token_env
                    valueFrom:
                      secretKeyRef:
                        name: chemclaw-secrets
                        key: $token_env
                resources:
                  requests:
                    cpu: 25m
                    memory: $(memory_request "$name")
EOF
  echo "---"
  kubectl kustomize "$out"
done
