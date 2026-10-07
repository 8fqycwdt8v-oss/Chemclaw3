"""The domain engines: pure computation, wrapped by the connector bundles of the same names.

`science.calc` is the cache, the calibration ledger and the statistical mechanics over calculation
results (the calculators are `Chemclaw3-mcp`'s); `science.bo` is the BoFire optimizer;
`science.fingerprints` is ECFP4/DRFP similarity. None imports Temporal, MCP or `chemclaw.agent`,
which keeps them testable without an orchestration stack.
"""
