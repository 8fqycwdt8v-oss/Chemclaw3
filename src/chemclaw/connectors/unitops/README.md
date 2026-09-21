# `connectors/unitops` — Scale-up and unit-operation sizing

**A bundle this release declares and does not run**, and does not bind either
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). The capability is
`Chemclaw3-mcp`'s `servers/unitops`; what lives here is the manifest four validators resolve tool
names through, the judgment beside it, and the chart entry saying where to dial it.

Seven read-only correlations: agitation carried between vessels on matched P/V or tip speed,
Zwietering's just-suspended speed, a jacketed vessel's heat-transfer time constant,
Fenske-Underwood-Gilliland shortcut distillation, the crystallisation yield two measured
solubilities permit, constant-pressure cake filtration time, and two-period batch drying.

**It holds no data at all** — no vessel register, no VLE table, no solubility curve, no cake
resistance — so every tool refuses to default the measured number its answer is made of. That is
also why it is the most expensive server in the fleet per tool: several required physical
quantities each, each stating its units.

## Turning it on

It is **off unless named**. `connectors_enabled` empty — a fresh checkout, `make test`, CI — binds
every bundle *except* the ones declaring `default_enabled: false`, and this is one. A deployment
that wants it names it in `CHEMCLAW_CONNECTORS_ENABLED` (the chart's `connectors.unitops.enabled:
true`), provides `CHEMCLAW_UNITOPS_TOKEN`, and points `connectors.unitops.url` at the served
address.

The reason is arithmetic rather than caution: every bound tool's schema rides ahead of the system
message on every model call, and this bundle and its four siblings are ~22,000 tokens together
against a prefix bound that both compaction thresholds are derived from. A deployment that never
asks a scale-up question should not pay a scale-up prefix.
