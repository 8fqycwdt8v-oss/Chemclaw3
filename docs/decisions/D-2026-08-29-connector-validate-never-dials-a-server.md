# D-2026-08-29-connector-validate-never-dials-a-server — the gate named as the control cannot see the drift

**Status:** accepted · **Date:** 2026-08-29

## The claim, and the measurement that disproves it

Five documents in this tree say, in almost identical words, that a drift between a declared tool
surface and the server that answers is caught by "`make connector-validate` against a running
server":

- `src/chemclaw/connectors/chem/connector.yaml`
- `src/chemclaw/connectors/safety/connector.yaml`
- `D-2026-08-15-a-bundle-we-declare-is-not-a-bundle-we-run`
- `D-2026-08-15-capability-moves-judgment-and-declaration-stay`
- `D-2026-08-15-safety-is-a-tool-not-a-gate`, and again in
  `D-2026-08-25-the-loop-is-a-composite-not-a-template`

**It never dials one.** `cli/validate_connectors.py` imports no HTTP client and opens no socket.
Its served-versus-declared rule resolves `server_tools_module(manifest.name)` — the bundle's own
`server/` package, *in this tree* — and calls `server.list_tools()` in-process. For a bundle that
ships no such module the rule returns `[]` before asking anything, and the names are reported as
`unverified_tool_surfaces`. `chem` and `safety` are exactly those bundles (D-2026-08-09). So the
sentence names, as the control that catches a drift, the one gate that is structurally incapable of
seeing it for the two bundles it was written about.

The module's own docstring has said so correctly the whole time — "`chem` and `safety` declare an
endpoint and ship no `server/` here, so their `tools:` lists are unverifiable offline". The prose
above it drifted from the code below it, which is the failure this repository keeps finding in
itself: a claim that a control exists, propagated by copy.

## What actually checks it

Two things, and both already exist:

- **`make live-template-args`** (`cli/validate_template_args_live.py`) opens the real connectors and
  checks each template step's *arguments* against what the running server advertises. Its Makefile
  comment already records that "the row that asked for this proposed `connector-validate`, which is
  inside `ci` and would have answered `[]` for exactly those bundles" — the same finding, reached
  once and not carried back into the five documents above.
- **`Chemclaw3-mcp`'s `assert_manifest_matches`**, against its own running server, for the
  served-versus-declared direction in the repository that owns the server.

Measured on 2026-08-29, with `Chemclaw3-mcp`'s `servers/chem` (8858) and `servers/safety` (8859)
running: `make template-validate` reports exactly seven steps across six templates as
name-checked/argument-unchecked, and `make live-template-args` checks all seven green
(`enumerate_bond_cleavages`, `enumerate_degradants`, `enumerate_protonation_states`,
`enumerate_stereoisomers`, `enumerate_tautomers`, and `screen_hazards` twice). The two steps it
reports as unreached — `calc`'s `compute_thermochemistry` and `molfp`'s `similar_molecules` — are
in-tree bundles that were never in the unchecked set.

The `calc` backend was measured in the same session and is a third case again, covered by neither
check: its physics is reached by `connectors/calc/remote.py` rather than advertised as a connector,
so no validator sees the tool names core sends it. Driven live against `servers/calc` (8860),
`compute_fukui_at` answers `ensemble_property`'s exact argument shape
(`{"structure": …, "solvent": …}`) and `calculation_key` returns a derivable key, so `cached_remote`
caches it rather than refusing.

## Decision

The two manifests are corrected to name the checks that exist. The four merged ADRs keep their
text — a merged ADR is never edited — and this ADR supersedes that one sentence in each.

The general rule, which is the part worth carrying: **a gate's name is not evidence about its
reach.** "`X` against a running server" is a claim about a socket, and the only thing that
establishes it is reading `X` for a client, or watching it connect.

## Consequences

- `make connector-validate` is unchanged. It is correct for the six bundles whose server is in this
  tree and honest about the two whose is not; only the prose around it was wrong.
- Nothing new runs in `ci`, which must stay offline.
- The check for an endpoint bundle we do not run costs a running server, so it stays in the live
  lane. That is a real gap between commits and it is now named rather than believed closed.
