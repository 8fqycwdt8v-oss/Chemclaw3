# `chemclaw.science` — the domain engines

**Responsibility:** the actual computation. `calc` is the physics (xTB/GFN2, conformers, pKa,
solubility, thermochemistry, the calculation cache), `bo` the BoFire optimizer, `safety` the hazard
screen, `fingerprints` ECFP4/DRFP and Tanimoto search.

**None of these import Temporal, MCP, FastAPI or `chemclaw.agent`.** That is the whole point: an
engine is importable and testable on its own, so a chemist can check the numbers without an
orchestration stack, and `tests/test_layering.py` keeps it that way.

## Why `science/calc` and `connectors/calc` both exist

They are a **pair, not a duplicate** — the single most confusing thing about this tree, and
deliberate:

| | `science/calc` | `connectors/calc` |
| --- | --- | --- |
| is | the engine | the wrapper |
| holds | the computation | the durable job + the MCP tool surface |
| imports | rdkit, xtb, numpy | Temporal, FastMCP, and the engine |
| runs | in a test, in a notebook | on a worker, in a pod |

Merging them would put Temporal imports inside the physics, which breaks the layering rule and
makes the engines untestable without a broker. The names read as a pair because before D-148 they
were `calc/` and `connectors/calc/`, where the distinction existed but was invisible.

Not every bundle has an engine here (`chem` is thin enough to sit on `core.chem`; `qm` dispatches
to HPC), and not every engine has exactly one bundle. The invariant is the other direction:
**capability code lives in a bundle or in `science/`, nowhere else** — true without exception since
D-156 moved `fingerprints` out of the old `chemclaw.mcp`.
