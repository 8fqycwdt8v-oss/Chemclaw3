"""The domain engines: pure computation, wrapped by the connector bundles of the same names.

`science.calc` is the cache, the calibration ledger and the statistical mechanics over what a
calculation returns — not the calculators, which are `Chemclaw3-mcp`'s;
`science.bo` is the BoFire optimizer; `science.fingerprints` is ECFP4/DRFP similarity. None of them
import Temporal, MCP or `chemclaw.agent` — that is the point of the split, and it is what keeps
them testable without an orchestration stack. `chemclaw.connectors.calc` is the durable-job and
tool-surface wrapper around `chemclaw.science.calc`, never a second copy of it (D-148).
"""
