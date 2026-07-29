# D-072 — CHECKMATE campaign 2026-07: adversarially-verified review, hardening, and refactor pass

**Context.** A full-codebase review campaign (13 reviewer agents; 73 raw findings; every finding
adversarially verified by an independent skeptic, 23 refuted; 50 confirmed + 10 of 11
orphaned-verifier findings confirmed by the orchestrator) followed by per-package fix waves. No S1
found; 13 S2 correctness bugs, the rest hardening/simplification.

**Decision (highlights beyond D-067…D-071).**
- *Chemistry correctness*: `run_xtb` rejects charge/SMILES-formal-charge mismatches and
  odd-electron species (fail-fast beats a silently guessed doublet — SMILES carries no spin);
  `predict_pka` rejects net-charged inputs (v1 calibration is neutral-acid-only); cached compute
  runs on the same canonical form its cache key hashes; `engine_version()` embeds the RDKit build
  so geometry/descriptor stacks invalidate stale cache entries per D-011.
- *Retrieval correctness*: one eligibility gate (`_eligible_notes`: type/tag + KM-7 currency)
  feeds graph, vector, and lexical retrieval; filters push into the index query
  (`NoteIndex.search_*(within=…)`) so top-k slots are never spent on ineligible neighbors; graph
  hits rank best-first for RRF; index scores survive into evidence chunks.
- *Layering*: the embedding seam moved to `chemclaw/embeddings.py`; `report/` depends only on the
  kernel, enforced by `tests/test_layering.py` (fresh-interpreter import guard). `Settings` was
  restructured into 18 cohesive mixin sections with zero call-site churn (160 fields byte-identical).
- *Front door bounds*: per-session turn serialization (409 on concurrent POST), streamed turns
  bounded by `service_turn_timeout_seconds`, per-user SSE stream cap, one connection per stream,
  kind-scoped event claims, LRU-bounded budget counters, JWKS validation off the event loop.
- *Config fail-fast*: Entra enforcement requires a resolvable JWKS source; nextflow completeness
  and poll-vs-heartbeat pairs validated at startup; `_redact` strips passwords from all libpq DSN
  forms before they can reach persisted error messages.

**Consequence.** The review's verified-findings queue is fully drained (59 fixed, 1 refuted);
coverage rose from 88.43% to 89.60% with 108 new behavior tests (616 passing), all proven against
real RDKit/BoFire/Postgres where applicable.

**Result.** Branch `claude/code-review-refactor-plan-wm34wc`, commits `2e7148c`…`4afbada`;
`make lint type test` + `make cov` green at every landed cluster.
