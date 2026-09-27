# `connectors/thermalsafety` — Runaway and thermal-hazard arithmetic

**A bundle this release declares and does not run**, and does not bind either
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). The capability is
`Chemclaw3-mcp`'s `servers/thermalsafety`; what lives here is the manifest four validators resolve tool
names through, the judgment beside it, and the chart entry saying where to dial it.

Seven read-only tools that turn calorimetry a person measured into the quantities a cooling-failure
argument is made of: adiabatic temperature rise, MTSR, TMR_ad, the Stoessel criticality class,
jacket heat-removal capacity, a Semenov critical ambient, and an oxygen-balance screen.

**The server fits nothing and predicts nothing**, which is the property its bundled skill exists to
keep visible. Every input is a DSC, ARC or RC1 number, and the one thing this system can compute
cheaply — a GFN2-xTB reaction enthalpy — is *not* one of them. The agent's own system prompt denies
that unconditionally and correctly: a computed enthalpy is a ranking of alternatives, not a process
heat load.

## Turning it on

It is **off unless named**. `connectors_enabled` empty — a fresh checkout, `make test`, CI — binds
every bundle *except* the ones declaring `default_enabled: false`, and this is one. A deployment
that wants it names it in `CHEMCLAW_CONNECTORS_ENABLED` (the chart's `connectors.thermalsafety.enabled:
true`), provides `CHEMCLAW_THERMALSAFETY_TOKEN`, and points `connectors.thermalsafety.url` at the served
address.

The reason is arithmetic rather than caution: every bound tool's schema rides ahead of the system
message on every model call, and this bundle and its four siblings are ~22,000 tokens together
against a prefix bound that both compaction thresholds are derived from. A deployment that never
asks a scale-up question should not pay a scale-up prefix.
