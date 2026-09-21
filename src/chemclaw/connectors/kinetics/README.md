# `connectors/kinetics` — Isothermal rate and ideal-reactor arithmetic

**A bundle this release declares and does not run**, and does not bind either
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). The capability is
`Chemclaw3-mcp`'s `servers/kinetics`; what lives here is the manifest four validators resolve tool
names through, the judgment beside it, and the chart entry saying where to dial it.

Six read-only tools over kinetic parameters the chemist already has: Arrhenius transfer of a rate
constant, an exact activation energy from two measured points, ideal-batch conversion and time,
PFR against CSTR at equal residence time, and the accumulation profile of a constant-rate
semi-batch addition.

**It refuses the question people will ask it most.** There is no regression here, so "here is my
concentration-against-time data, what is the rate law" is not something this server answers — a gap
recorded in `docs/archive/IDEATION-2026-09-20-process-development-hte-and-protocol-prediction.md`
against the fleet, not against this bundle. The bundled skill says so rather than letting the model
discover it by trying.

## Turning it on

It is **off unless named**. `connectors_enabled` empty — a fresh checkout, `make test`, CI — binds
every bundle *except* the ones declaring `default_enabled: false`, and this is one. A deployment
that wants it names it in `CHEMCLAW_CONNECTORS_ENABLED` (the chart's `connectors.kinetics.enabled:
true`), provides `CHEMCLAW_KINETICS_TOKEN`, and points `connectors.kinetics.url` at the served
address.

The reason is arithmetic rather than caution: every bound tool's schema rides ahead of the system
message on every model call, and this bundle and its four siblings are ~22,000 tokens together
against a prefix bound that both compaction thresholds are derived from. A deployment that never
asks a scale-up question should not pay a scale-up prefix.
