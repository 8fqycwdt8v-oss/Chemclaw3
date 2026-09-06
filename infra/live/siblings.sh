# Where the three companion checkouts are — one resolution, sourced by both live lanes.
#
# It was two guesses that disagreed and neither of which resolved. `processes.sh` defaulted to
# `$REPO_ROOT/../chemclaw3-mcp` and `e2e-full-stack/up.sh` to `/workspace/8fqycwdt8v-oss/…`, a
# directory that does not exist in the container this repository's own tooling provisions — so
# `make live-up` failed at the fleet checkout on a machine that *had* the fleet checkout, and
# `up.sh` exported its own answer into `processes.sh` precisely because the two differed. The
# workaround was in the tree; the disagreement was not.
#
# So: the variable if it is set, then each place a sibling is actually kept, and the checkout name
# in both the casing GitHub publishes (`Chemclaw3-mcp`) and the casing a clone usually lands in
# (`chemclaw3-mcp`) — the split that made a single hardcoded default wrong either way.
#
# Contract: the sourcing script defines `REPO_ROOT` first. Nothing here is `readonly`, because both
# lanes source this into their own `readonly` assignments.

# sibling_repo <ENV_VAR_NAME> <CanonicalCheckoutName>
#
# Prints a path. When nothing is found it prints the first candidate rather than failing, so the
# caller's own `die` can name a concrete place to clone into — each caller already has one, and
# each says something different about why it needs that repo.
sibling_repo() {
  local var="$1" name="$2" lower root candidate
  if [ -n "${!var:-}" ]; then
    printf '%s' "${!var}"
    return 0
  fi
  lower="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  local -a roots=(
    "$(dirname "$REPO_ROOT")"
    "$(dirname "$REPO_ROOT")/8fqycwdt8v-oss"
  )
  for root in "${roots[@]}"; do
    for candidate in "$root/$name" "$root/$lower"; do
      if [ -d "$candidate" ]; then
        printf '%s' "$candidate"
        return 0
      fi
    done
  done
  printf '%s' "${roots[0]}/$name"
}
