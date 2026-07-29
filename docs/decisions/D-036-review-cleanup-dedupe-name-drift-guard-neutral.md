# D-036 — Review cleanup: dedupe, name-drift guard, neutral config names, doc refresh

**Context.** The review's lower-severity cleanups, batched.

- **Tool-name drift.** `bo_tools`' docstring told the model to call `find_similar_reactions`,
  but the agent's actual MCP tool is `similar_reactions`. Fixed, and `test_agent` now asserts
  every tool the instructions name is in the agent's advertised surface (registered function
  tools + allowed MCP tools) — a regression guard against this class of bug.
- **Duplicated `_WIKILINK`.** The identical regex in `kg.note` and `report.retrievers` is now
  one public `kg.note.WIKILINK`, imported by the report layer.
- **Scattered hashing.** `report.harness._report_id` used a bare `hashlib.sha256`; it now uses
  the shared `chemclaw.ids.stable_hash` (report ids stay ref-safe and unique — the test checks
  properties, not the exact digest).
- **Neutral config name.** `report_excerpt_chars` → `note_excerpt_chars`: both the report
  harness and the memory layer excerpt note bodies with it, so the name no longer implies the
  budget is report-only (one knob, cannot drift).
- **`search_tools`** is documented as the in-process example/test seam that is NOT registered on
  the live agent (which uses the MCP capability servers); the two must stay in sync.
- **Docs refresh.** `agents/__init__.py` and `agents/README.md` no longer claim the tools are all
  MAF↔Temporal adapters or that the package is "empty until Phase 1"; the `evals.metric` (singular
  interface/registry) vs `evals.metrics` (plural functions) split is called out in both headers.
- **ESOL coefficients stay inline** (`calc.solubility`): the Delaney (2004) model is a fixed,
  published closed form, so its five coefficients are a deliberate, documented exception to
  "config, never magic numbers" (unlike the pKa calibration, which is tunable and lives in
  config). Recorded here so it stops resurfacing in review.
- The ADR convention (`DECISIONS.md` = terse running log, `docs/adr/` = long-form when a rationale
  outgrows a paragraph) was already documented in `docs/adr/README.md` and is left as-is.
