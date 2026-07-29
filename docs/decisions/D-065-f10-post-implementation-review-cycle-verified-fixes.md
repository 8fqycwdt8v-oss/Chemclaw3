# D-065 — F10 post-implementation review cycle: verified fixes

**Context.** After F10 (A–G) landed, an adversarial review — five agent teams over the new features
and the whole codebase — surfaced real plan-vs-code and correctness gaps. The most severe: F10-B's
`verifier_confidence_threshold` was defined but never read (dead config), so the ticket's headline
*confidence routing* was not actually wired; several docstrings over-claimed behavior that did not
exist (a D-032 hold, "any deleted row breaks the chain").

**Decision.** Fixed each confirmed finding in-branch rather than deferring:
- **F10-B routing wired.** `AnswerEvent.review_required` is set when `confidence <
  verifier_confidence_threshold` — the config is now consumed and low/high confidence are
  distinguishable. Over-claiming docstrings corrected (the durable D-032 hold is deferred, not built).
  Wikilink extraction unified into `kg.note.cited_ids` (strips targets, one definition); a citation
  miss now reports the *unresolved* id, not `citations[0]`.
- **F10-D report durability.** A failed section degrades to a visible `retrieval_failed` marker
  (never silently dropped from a GxP draft); the redundant child-level `BAD_DATA_RETRY` was removed
  (the activity is the single retry boundary); `fan_out` re-raises `CancelledError` instead of
  logging it as a drop, and guards `max_parallel >= 1`.
- **F10-F drift honesty.** `detect_drift` uses a *relative* band (scale-appropriate across
  heterogeneous metrics); `DriftAlert.vanished` disambiguates an absent metric from a 0.0 score; the
  alert now rides a *must-deliver* `notify_session` (a dropped regression alert fails the run); the
  scheduled job is documented as a deployment-consistency tripwire (live-retriever drift deferred).
- **Shipped retrieval/audit.** `PostgresNoteIndex.search_dense` now applies the positive-similarity /
  zero-vector guard the tested InMemory reference already had (backends no longer diverge);
  `GraphRetriever` uses the shared `note_text` haystack; RRF is 1-based (canonical); the audit chain
  gained a genesis anchor (catches prefix truncation) with docstrings corrected to the true guarantee
  (tip truncation needs an external count anchor — deferred).

**Consequence.** Two of F10-B's three CHECKMATE claims that were aspirational in the merged code are
now real (routing) or honestly deferred with a trigger (report prose verification). Three deferrals
are recorded in DEFERRED.md (F10-B3, live-retriever drift, audit tip-truncation anchor).

**Result.** `make lint type test` green. New/updated tests: `test_runner` (threshold routing),
`test_report`/`test_report_workflow` (failed-section marker), `test_eval_drift` (relative band +
`vanished`), `test_audit_chain` (prefix-truncation), `test_memory_jobs` (fan-out registration),
`test_config`.
