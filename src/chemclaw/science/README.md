# `chemclaw.science` — the domain engines

**Responsibility:** the computation this repository still performs itself. `bo` is the BoFire
optimizer, `fingerprints` ECFP4/DRFP and Tanimoto search, and `calc` is **no longer the physics** —
after `D-2026-08-16-the-physics-leaves-the-cache-stays` it holds the D-011 calculation cache, the
calibration ledger, the geometry store, the RRHO and Crippen arithmetic, and the models the Temporal
wire carries, while xTB/GFN2, conformers, pKa and solubility answer from `Chemclaw3-mcp`'s
`servers/calc`. `safety` is
gone entirely: `D-2026-08-15-safety-is-a-tool-not-a-gate` made the hazard screen an ordinary MCP
server with no in-process caller left.

**None of these import Temporal, MCP, FastAPI or `chemclaw.agent`.** That is the whole point: an
engine is importable and testable on its own, so a chemist can check the numbers without an
orchestration stack. Two tests keep it that way, and until 2026-08-08 only one of the four
prohibitions was covered: `tests/test_layering.py` records an import edge only when the target
starts with `chemclaw`, so it enforced the `chemclaw.agent` half and nothing else — `import
temporalio` here would have passed every test in the repository.
`tests/test_third_party_layering.py` is the other half, and it declares exactly what `science`
may reach for (rdkit, the BoTorch stack, the calculation cache's Postgres driver) and nothing more.

## Three stores, and why they are three

`calc` holds three content-addressed stores, and the temptation to merge any two of them is a
mistake each one's key exists to prevent.

| Store | Addressed by | Why not one of the others |
| --- | --- | --- |
| `calculation_results` (`store.py`) | `calc_type@version:input_hash:params_hash` | The *answer*. Never pruned (D-011), because evicting one silently converts a hit into a recomputation |
| `artifact_blobs` (`artifacts.py`) | SHA-256 of the bytes | A *by-product* — a Hessian is megabytes — reached through a `(calc_key, name)` link and evicted by cost and idle time |
| `structures` (`structures.py`) | `structure_id` | A *geometry*, and its identity is narrower than its bytes: `smiles` and `origin` are excluded, so two identical geometries are one structure whatever produced them. A byte address would fork on the provenance the identity ignores, and a calculation-keyed link would make the producer part of the address (D-2026-08-21) |

The last one is what makes a computed geometry nameable. `science/calc/geometry.py` is the walker
that keeps a geometry on the way in and replaces it with its address on the way out — two halves of
one act, which is what makes "every `structure_id` the agent is shown resolves" a property of the
code rather than a convention.

## Why `science/calc` and `connectors/calc` both exist

They are a **pair, not a duplicate** — the single most confusing thing about this tree, and
deliberate:

| | `science/calc` | `connectors/calc` |
| --- | --- | --- |
| is | the cache and the arithmetic | the wrapper |
| holds | the store, the ledger, RRHO/Crippen, the wire models | the durable job, the MCP tool surface, the remote client |
| imports | rdkit, numpy, the Postgres driver | Temporal, FastMCP, and `science/calc` |
| runs | in a test, in a notebook | on a worker, in a pod |

Merging them would put Temporal imports inside the arithmetic, which breaks the layering rule and
makes it untestable without a broker. The names read as a pair because before D-148 they
were `calc/` and `connectors/calc/`, where the distinction existed but was invisible.

**Neither of them imports `xtb` or `tblite` any more**, and that is enforced rather than intended:
`tests/test_third_party_layering.py` declares what `science` may reach for and no row grants the
physics stack, so re-adding an in-process engine here turns it red.

Not every bundle has an engine here (`chem` is thin enough to sit on `core.chem`; `qm` dispatches
to HPC), and not every engine has exactly one bundle. The invariant is the other direction:
**capability code lives in a bundle or in `science/`, nowhere else** — true without exception since
D-156 moved `fingerprints` out of the old `chemclaw.mcp`.
