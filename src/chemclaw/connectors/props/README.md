# `connectors/props` — Solvent and pure-component properties

**A bundle this release declares and does not run**, and does not bind either
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). The capability is
`Chemclaw3-mcp`'s `servers/props`; what lives here is the manifest four validators resolve tool
names through, and the chart entry saying where to dial it.

Six read-only lookups over a vendored, checksummed table of process solvents, plus two closed-form
conversions (vapour pressure at a temperature, boiling point under vacuum) and a Hansen-ranked
swap shortlist.

**Why this bundle has no `skills/`.** The judgment about *which* solvent is already written and is
not this capability's: `skills/solvent-selection` holds it, and says in its own words that the
green-chemistry and safety constraints usually bind before the free-energy comparison does. This
bundle is what that skill was missing — the constraints themselves, as numbers. A second skill here
would be the same judgment addressed to a different table.

## Turning it on

It is **off unless named**. `connectors_enabled` empty — a fresh checkout, `make test`, CI — binds
every bundle *except* the ones declaring `default_enabled: false`, and this is one. A deployment
that wants it names it in `CHEMCLAW_CONNECTORS_ENABLED` (the chart's `connectors.props.enabled:
true`), provides `CHEMCLAW_PROPS_TOKEN`, and points `connectors.props.url` at the served
address.

The reason is arithmetic rather than caution: every bound tool's schema rides ahead of the system
message on every model call, and this bundle and its four siblings are ~22,000 tokens together
against a prefix bound that both compaction thresholds are derived from. A deployment that never
asks a scale-up question should not pay a scale-up prefix.
