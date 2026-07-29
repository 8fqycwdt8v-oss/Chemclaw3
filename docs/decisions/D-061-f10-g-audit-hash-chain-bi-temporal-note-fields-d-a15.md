# D-061 — F10-G: audit hash-chain + bi-temporal note fields (D-A15)

**Context.** D-034 left the audit hash-chain "for Phase 6"; `architektur.md` §10.4 proposed
bi-temporal note fields but never schematized them. Both are low-complexity, GxP-relevant.

**Decision.**
- **F10-G1:** `011_audit_hash_chain.sql` adds `prev_hash`/`row_hash` to `audit_events`.
  `PostgresAuditSink.record` computes `row_hash = chain_hash(prev_hash, event)` (reusing
  `chemclaw.ids.stable_hash`, one hashing scheme — D-033) under a transaction advisory lock so
  concurrent appends cannot fork the chain. `scripts/verify_audit_chain.py` (`make audit-verify`)
  walks the rows and reports the first broken link; legacy empty-hash rows are skipped.
- **F10-G2:** `kg/note.py` gains optional `valid_from`/`valid_to` with a validator rejecting
  `valid_to < valid_from`; retrievers may filter on them later (no premature consumer).

**Consequence.** Tampering with any audited row is detectable; notes can record what was known and
when it was valid. The `NullAuditSink` default is unaffected.

**Result.** `make lint type test` green. Tests: `test_audit_chain`, `test_note`, `test_kg_validate`.
