# Tranche 4 — `calc`: physics moves, the cache and the durable jobs stay

Status: **planned, not started.** Blocked on `#185` (tranche 3) merging, per the sequencing rule
that each tranche proves the pattern before the next begins.

## What the investigation changed

Two findings that the earlier plan got wrong, both worth stating before any code moves.

### 1. "9 move, 6 stay" omits five tools and two authorization gates

`connectors/calc/connector.yaml` declares 15 `endpoint.tools` — that part was right. It also
declares a **`jobs:` block with five more agent-facing tools**, none of them in either list:

| tool | params_model | gate |
|---|---|---|
| `compute_reaction_energy` | `specs:ReactionJobSpec` | precondition `require_supported_solvents` |
| `compare_solvents` | `specs:SolventScreenJobSpec` | — |
| `scan_coordinate` | `specs:ScanJobSpec` | — |
| `sample_conformers` | `specs:EnsembleJobSpec` | `expensive: true` |
| `compute_interaction_energy` | `specs:ComplexJobSpec` | `expensive: true` |

These are the durable Temporal half of the bundle. **They stay**, and not as an exception — the
user's binding rule is that the split within science is by *runtime*: request/response compute
becomes a stateless MCP server, long-running orchestration stays a durable job here. A multi-step
scan or a CREST ensemble is orchestration. The point is that the plan never said so, and two
`expensive: true` declarations feed `authz.expensive_actions` — a silent omission would have moved
a role gate.

### 2. `calc_version` fails **silently** after the split, and that is the whole difficulty

It is not one seam but two with different lifetimes: half the cache key, *and* the primary key of
the calibration ledger (`predictions`, unique on `(calc_type, calc_version, input_hash)`,
exact-match by D-139 with no version pooling).

Six producers, every one reading local state:

| producer | reads |
|---|---|
| `XtbSpec.calc_version` | subprocess `xtb --version`, `tblite`/`rdkit` dist versions, `_HAMILTONIAN_REVISION` |
| `CrestSpec.calc_version` | subprocess `crest --version` |
| `ComplexSpec.calc_version` | both binaries |
| `pka.calc_version` | tblite/rdkit + **seven** `settings.*` constants + a nested `XtbSpec.calc_version` |
| `solubility.calc_version` | model constant + rdkit dist + `solubility_rmse_log` |
| `descriptors._calc_version` | rdkit dist |

**The failure mode.** `binary_version()` returns the literal string `"absent"` when the binary is
missing (`science/calc/xtb_cli.py:217`, `crest_cli.py:125`) — it does not raise. Two tools that
*stay in this repo* re-derive the version from scratch: `calculator_trust` and
`calculator_outliers`, through `_CALIBRATED`/`_calibrated` (`connectors/calc/server/tools.py:433`,
used at `:480` and `:585`). On a post-move pod with no xtb binary, those produce a syntactically
valid version string matching **zero** ledger rows, and `Calibration.verdict` renders a confident
`UNCALIBRATED: … Its accuracy is unknown, not good` — the exact state D-139/REV-12 built this
machinery to distinguish, reached by a mechanism neither decision anticipated. Every historical
residual becomes unreachable at the same time, and `pka.calc_version`'s own docstring prices the
recovery: the ledger refills only at the rate the calculator is re-run, per molecule.

So the rule is stronger than "transport it": **no code in this repository may derive a
`calc_version`.** The server returns it on every result, and the two staying tools read it from a
server-side lookup rather than computing it. A test must assert that no local derivation survives,
because the defect is invisible without one.

## The other three, now measured

- **`structure_id`** is a clean either/or, but a real one. The property is pure
  (`stable_hash` over elements/positions/charge/multiplicity, positions rounded first), so it ports
  trivially. Getting a `Structure` from a SMILES is not: `structure_from_smiles` runs RDKit
  `parse_molecule` + seeded ETKDG + optional MMFF. Every `xtb.*` and `geometry.*` key routes through
  `XtbSpec.cache_key`, which takes `structure.structure_id`. So either this repo keeps RDKit
  embedding, or the server returns the key. **Take the second**: RDKit's version is already inside
  `engine_version()`, so a client/server RDKit skew would fork the cache — keeping RDKit here does
  not avoid the coupling, it hides it.
- **`run_cached_with_artifacts` has zero production callers** — confirmed. Every reference in `src/`
  is its own definition or a docstring; all real callers are `tests/test_artifacts.py`. **Delete it**
  rather than port it. `run_cached_hessian` stays hand-rolled for the reason its docstring gives:
  the row is only half the result, and `_persist` refuses to write the row if either `.npy` blob did
  not land (three refusal paths, `xtb_hessian.py:270-289`). A 76-atom Hessian is 228×228 float64
  ≈ 416 kB; the 32 MiB `artifact_max_bytes` bound is what makes base64-over-JSON-RPC even arguable.
- **`science/bo`** is lighter than feared: two async cached-compute calls, one request model
  instantiated (`SolubilityInput`), one concrete store constructed (`PostgresStore` in
  `_solubility_max`, resolved by name because a Temporal workflow cannot carry a callable). No
  physics constants, no subclassing. `featurize` needs the returned `key` string — which is
  blocker 1 again.
- **`connectors/calc/results.py`'s five models all cross the Temporal boundary**: they are optional
  fields of `XtbJobResult`, the declared return type of the `run_xtb_calculation` activity, dumped
  into `ConnectorJobResult.data`. Their shapes are pinned by **in-flight and replayed workflow
  histories**, not just by code. They move with the workflow or are duplicated as versioned wire
  schemas — a durable-job payload cannot lose its type.

## Steps

- [ ] Wait for `#185` to merge.
- [ ] mcp side: `servers/calc` with the 9 compute tools, returning `calc_version` **and** the full
      `CalculationKey` string on every result.
- [ ] Chemclaw3 side: the twelve `run_cached_*` wrappers move from the sync `run_cached` onto
      `cached_compute` (`store.py:285`) with a remote closure — it is the only wrapper whose callback
      is already async and returns a plain dict.
- [ ] Delete `run_cached_with_artifacts`.
- [ ] Replace `_CALIBRATED`/`_calibrated` with a server-side version lookup; add the test that no
      local derivation survives.
- [ ] Keep: the cache, `store.py`, `postgres_store.py`, `postgres_artifacts.py`, `calibration.py`,
      `uncertainty.py`, `specs.py`/`solvents.py`, the 6 read/write tools, and all five durable jobs.

## Verify

`make eval-strict` before and after (the science regression gate); a cache hit/miss measurement
proving a persisted result is still never recomputed (D-011) — the whole reason the cache stays;
`tests/test_qm_persistence.py` and `test_qm_workflow.py` green, since `qm` is a cache client that
must not notice the split; and a live turn against the running server.
