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

# sibling_env_vars <CanonicalCheckoutName>
#
# Every environment variable that may name this checkout, most-specific first. One table, because
# **two names for one path, each honoured by one caller, is the same disagreement this file was
# written to end** — one directory deeper. `tests/test_context_floor.py` read
# `CHEMCLAW_MCP_CHECKOUT` and searched a single hardcoded path in a single casing while the live
# lanes read `CHEMCLAW_MCP_REPO` and searched four; the consequence was not a wrong answer but a
# missing one, on a machine that had the checkout — a control that skipped where it should have
# run. Adding a variable here reaches the shell lanes and `tests/siblings.py` in one edit.
sibling_env_vars() {
  case "$1" in
    Chemclaw3-mcp) printf '%s\n' CHEMCLAW_MCP_REPO CHEMCLAW_MCP_CHECKOUT ;;
    Chemclaw3_mock) printf '%s\n' CHEMCLAW_MOCK_REPO ;;
    Chemclaw3_ui) printf '%s\n' CHEMCLAW_UI_REPO ;;
    *) : ;;
  esac
}

# sibling_repo <ENV_VAR_NAME> <CanonicalCheckoutName>
#
# Prints a path. When nothing is found it prints the first candidate rather than failing, so the
# caller's own `die` can name a concrete place to clone into — each caller already has one, and
# each says something different about why it needs that repo. A caller that has to tell "found"
# from "the fallback" tests the printed path itself, which is what `tests/siblings.py` does.
#
# `$1` is consulted first and then `sibling_env_vars "$2"`, so the caller's own variable keeps its
# precedence and every other name for the same checkout is honoured too.
sibling_repo() {
  local name="$2" var lower root candidate
  # Unquoted on purpose: the table prints one variable name per line and each is a word.
  # shellcheck disable=SC2046
  for var in "$1" $(sibling_env_vars "$name"); do
    if [ -n "${!var:-}" ]; then
      printf '%s' "${!var}"
      return 0
    fi
  done
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
