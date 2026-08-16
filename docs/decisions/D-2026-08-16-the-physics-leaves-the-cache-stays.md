# D-2026-08-16-the-physics-leaves-the-cache-stays — `calc` splits by *composability*, not by speed

**Status:** accepted · **Date:** 2026-08-16 · Completes the capability migration begun by
`D-2026-08-15-capability-moves-judgment-and-declaration-stay`. Supersedes that ADR's implicit
assumption that a bundle splits along the fast/slow line.

## Context

Tranches 2 and 3 moved `chem` and `safety` whole: pure, stateless tools with no store behind them.
`calc` is the first bundle where that shape does not exist. It has fifteen tools, five durable
Temporal jobs, a Postgres result cache whose non-recomputation is D-011, and a calibration ledger
keyed exactly on `(calc_type, calc_version, input_hash)`.

The plan of record said: move the nine request/response tools, leave the six read/write tools and
the five durable jobs. **Measurement killed it twice.**

**First, the engine would have been copied rather than moved.** `reaction.py`, `xtb_scan.py`,
`conformers.py` and `complexes.py` — the four modules behind the durable jobs that were to stay —
transitively import `structure`, `xtb_engine`, `xtb_opt`, `xtb_thermo`, `xtb_hessian`, `xtb_spec`,
`crest_cli` and `anc`. That is nearly the whole engine. D-148 says `connectors.calc` is a wrapper
around `science.calc` and "never a second copy of it"; the split as planned would have made two
copies of the physics and called it a migration.

**Second, a composite tool cannot be cached by its caller.** `compute_thermochemistry` has no
top-level cache row today — its key would have to name the geometry its refinement loop *settles
on*, which is an output. Its economy is entirely the nested `xtb.opt`/`xtb.hess` entries, and a
single remote call swallows them:

| | cold | repeat, today |
|---|---|---|
| `CCO` | 0.816 s | **0.007 s** |
| `CC(=O)OCC` | 3.273 s | **0.012 s** |
| `predict_logd`, pyridine | 20.603 s | **0.005 s** |

Moving those whole would have converted every repeat into a full recompute on the most expensive
tools in the set — a D-011 violation in substance, arrived at by moving code rather than by
changing a rule.

## Decision

**Chemclaw3 keeps orchestration and the cache. `Chemclaw3-mcp` holds the physics, as individually
keyed primitives.** The line is *composability*, not speed:

- **A primitive** — one calculation whose identity is derivable from its inputs — moves. It is
  exposed by the server with a key, and this repository caches it.
- **A composite** — anything whose key would name an output — is **not shipped at all**. It is
  decomposed, and this repository composes the parts. `compute_thermochemistry` becomes remote
  optimise + remote Hessian + local RRHO arithmetic; `predict_logd` becomes a remote cached pKa
  plus a local Crippen sum. `reaction.py` contributed *no* primitive: it turned out to be pure
  composition over tools already exposed.
- **Durability stays here.** The five Temporal jobs keep their workflows and activities; their
  activities call remote primitives, each wrapped in `durable/heartbeat.py::beating`.

This also resolves the fast/slow tension that made the earlier plan look reasonable. The fleet's
informal "~20 s" expectation was the wrong criterion and has been rewritten in that repo's
`docs/adding-a-server.md`: **duration is not the property that fleet promises.** A server may be
slow; it may not be stateful.

## The rule that carries the most risk

**No code in this repository may derive a `calc_version`**, and the reason is that getting it wrong
is *silent*. The version is half the cache key and the primary key of the calibration ledger
(exact-match, D-139, no version pooling). It is built from `xtb --version`, distribution versions
and seven calibration settings. `binary_version()` answers the literal string `"absent"` rather
than raising when a binary is missing — so a pod deriving its own would produce a **well-formed**
version matching zero rows, `calculator_trust("pka")` would report a confident `UNCALIBRATED`, and
every historical residual would become unreachable at once. Nothing would look broken.

So the server returns the version on every result *and* through a `calculation_key` tool that
derives the identity without computing. That tool exists because `cached_compute` needs the key
**before** the compute: a result carrying its own key is necessary but not sufficient, since on a
hit there is no result to read one off.

`tests/test_calc_remote.py` asserts the rule statically, over the source, because a behavioural
test would pass while the defect sat one import away. Its `science/calc` half is `xfail(strict=True)`
until the in-process engines are deleted — so finishing the migration turns it XPASS, which pytest
reports as a failure, and the marker must be removed by whoever finishes.

## What this was verified against

Not asserted — measured against the merged server on 8860, with this repository deriving the same
values locally:

| | server | here |
|---|---|---|
| `xtb.sp` key | `xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:b41312b0cdc59ab7` | identical |
| `pka` version | `…/cal-0.28733:-29.3116/base-0.241396:-22.1843/u-1.6:1.0/opt-…` | identical |
| `solubility` version | `esol-delaney@2004/rdkit-2026.3.5/u-0.75` | identical |
| `developability` version | `rdkit-2026.3.5` | identical |
| `structure_id` for `CCO` | `st_739a222f45be0c3a` | identical |

and `calculation_key(tool, args)` returns exactly what the compute tool later stamps on its result,
checked over the wire on four tools. **One byte of disagreement here and every lookup misses
forever**, so this is the check the whole design rests on.

Then the client itself, against the same server: `predict_pka`, `compute_xtb_energy` and
`predict_solubility` each returned `cached=False` then `cached=True` — D-011 holding across the
wire — and `predict_logd` computed twice and stored nothing, which is correct because it never had
a row of its own.

## Two consequences worth stating rather than discovering

**A key is transported as four fields, never as the flat string.** A real `calc_version` contains
both delimiters — `esol-delaney@2004` carries the `@`, `cal-0.28733:-29.3116` carries the `:` — so
a client splitting `type@version:input:params` would reassemble a key that matches nothing, with no
error anywhere. This was found by looking at the strings, not by reasoning about the format.

**A cache hit now costs a round trip, measured at ~0.10 s against ~0.007 s in-process.** It is one
`calculation_key` call plus a session setup, and the session cannot be shared process-wide:
`connectors/identity.py` requires a connection to belong to exactly one caller, or concurrent
callers misattribute each other. Trivial against an SCF, and it is the number to watch if a caller
ever loops over thousands of cached lookups.

## Consequences

- The server's manifest must **not** go on `CHEMCLAW_CONNECTORS_DIR`. The agent still calls this
  repository's own `calc` tools; the server's seventeen tools are orchestrator-facing and would
  otherwise enter a prompt, and a partial port would win the `calc` name collision and take six
  read tools plus every durable job off the surface with no error.
- `CALCULATION_EPOCH` is the one constant both repositories must change in the same PR. The
  calculator settings, the RDKit build and the flat key format are each explicitly *not* on that
  list, because only the server reads or produces them now.
- `run_cached_with_artifacts` was deleted on the way here: zero production callers, and it carried
  the only coverage of a raising artifact store — for the *opposite* policy to the surviving one.
