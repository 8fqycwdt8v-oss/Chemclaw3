"""The domain engines: pure computation, wrapped by the connector bundles of the same names.

`science.calc` is the physics (xTB/GFN2, conformers, pKa, solubility, thermochemistry);
`science.bo` is the BoFire optimizer; `science.safety` is hazard screening. None of them import
Temporal, MCP or `chemclaw.agent` — that is the point of the split, and it is what keeps them
testable without an orchestration stack. `chemclaw.connectors.calc` is the durable-job and
tool-surface wrapper around `chemclaw.science.calc`, never a second copy of it (D-141).
"""
