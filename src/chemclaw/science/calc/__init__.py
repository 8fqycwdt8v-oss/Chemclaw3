"""The calculation cache, the calibration ledger, and the shapes both are about.

The xTB/CREST calculators are `Chemclaw3-mcp`'s `servers/calc`, called as individually keyed
primitives; composites are decomposed and composed one layer up (`connectors/calc`) so every step is
cached. Here: the D-011 cache (`store`, `postgres_store`), the calibration ledger, the artifact and
geometry stores, the wire models, and the arithmetic that depends on inputs the server never saw
(`thermo`, `logd`). This side's `CALCULATION_EPOCH` composes with the server's rather than matching
it.
"""
