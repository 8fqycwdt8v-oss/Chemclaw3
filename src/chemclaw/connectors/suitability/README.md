# `connectors/suitability` — USP <621> chromatographic system suitability

**A bundle this release declares and does not run**, and does not bind either
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). The capability is
`Chemclaw3-mcp`'s `servers/suitability`; what lives here is the manifest four validators resolve tool
names through, the judgment beside it, and the chart entry saying where to dial it.

Seven read-only tools over numbers a chromatogram already reported: replicate-injection RSD with
the compendial injection-count rule, plate count, tailing factor, resolution, retention and
separation factors, whether a proposed isocratic change stays inside what the general chapter
permits, and a whole-sequence report against declared criteria.

**Nothing here opens an instrument file**, finds a peak or assigns a baseline. And passing
suitability says the system performed at the moment of the run — it is neither method validation
nor evidence that the result is accurate, which is the sentence its bundled skill is mostly about.

## Turning it on

It is **off unless named**. `connectors_enabled` empty — a fresh checkout, `make test`, CI — binds
every bundle *except* the ones declaring `default_enabled: false`, and this is one. A deployment
that wants it names it in `CHEMCLAW_CONNECTORS_ENABLED` (the chart's `connectors.suitability.enabled:
true`), provides `CHEMCLAW_SUITABILITY_TOKEN`, and points `connectors.suitability.url` at the served
address.

The reason is arithmetic rather than caution: every bound tool's schema rides ahead of the system
message on every model call, and this bundle and its four siblings are ~22,000 tokens together
against a prefix bound that both compaction thresholds are derived from. A deployment that never
asks a scale-up question should not pay a scale-up prefix.
