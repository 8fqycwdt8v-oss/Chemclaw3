# D-057 — Four more engine gaps closed (KM-5, KM-14 retrieval half, AG-14, AG-15)

**Context.** After D-055/D-056, five gap-doc findings remained. Each carried a design decision that
had been left un-guessed. Directed to implement four of them (AG-13 stays deferred — see below), each
with a **defensible default** documented here rather than a new config knob per open question.

**Decisions.**
- **KM-5 — rank-before-truncate.** `EvidenceChunk` gains an optional `score` in [0,1]; `gather_evidence`
  sorts by it (stable) before applying `gather_evidence_max_chunks`, so a truncated sweep keeps the
  best-supported evidence, not an arbitrary disk slice. Scoring is per-retriever in its own terms —
  graph hits score by the note's `confidence` (`retrieval_default_confidence` when absent, wiring the
  previously-unread field), structural hits by their Tanimoto similarity. It is a within-sweep
  ordering heuristic, **not** a calibrated cross-source probability (documented on the field). Finer
  lexical relevance is deliberately skipped: the graph filter is whole-substring, so every returned
  note already contains the full query — a lexical-overlap term would be vacuous until KM-4 lands.
- **KM-14 — retrieval-path cache (not the clustering half).** `load_notes` caches the parsed notes
  per directory behind a cheap stat fingerprint (`(path, mtime_ns, size)` per file); any add/edit/
  delete busts it, so retrieval stays **always-live** while skipping the re-parse when nothing
  changed. Guarded by a lock (retrieval offloads to threads). `graph_cache_enabled` (default on) can
  disable it. The separately-deferred O(n²) *clustering* half of KM-14 is untouched — it is a
  background job, not the per-query interactive path the gap flags as the sharper concern.
- **AG-14 — version provenance.** `AuditEvent` gains `revision`, stamped from `deployment_revision`
  (the deployment's Git SHA / image digest, "unknown" until F6 sets it) at middleware build time;
  migration `010_audit_revision.sql` adds the column (idempotent, `NOT NULL DEFAULT 'unknown'`, no
  backfill). A past result now ties to the exact version that produced it. The *behavioral* half of
  AG-14 (a pre-live gate) is AG-13, deferred.
- **AG-15 — admission control.** The front door holds a config-capped `asyncio.Semaphore`
  (`service_max_concurrent_turns`, default 8) for a turn's whole streamed run; a turn that cannot get
  a permit within `service_turn_admission_timeout_seconds` (default 5) is shed with **503** rather
  than piling onto the shared LLM endpoint. Only the LLM-bound message turn is gated (health and
  push-back streams are not). The cap is a conservative default to be tuned to the real endpoint's
  throughput — picking it does not need to wait for that number, only tuning does.

**Deferred (unchanged).** **AG-13** (agent-behavior / prompt / skill regression eval) stays in
`DEFERRED.md`: a faithful behavior eval must run the agent against the real internal LLM endpoint
(unreachable offline); a mock would only test the mock. It is the one genuinely infra-gated item.

**Contract / behavior notes.** `gather_evidence` now returns its cap's worth of *highest-scored*
chunks (order/content otherwise unchanged; an all-unscored corpus keeps disk order via the stable
sort). Retrieval reads may be served from the graph cache (busted on any note change — never stale).
The front door can now answer **503** on the messages route under load. Migration `010` must be
applied (`make db-migrate`); it is idempotent.

**Post-implementation review hardening.** An independent diff review found no live bug but five
latent/robustness items; four were fixed here, one consciously kept:
- *Graph cache — stat on a vanished file.* `_dir_fingerprint` now wraps `path.stat()` in
  `except OSError: continue`, so a note deleted between `rglob` and `stat` (a `git pull` under a live
  query) drops out of the fingerprint and busts the cache on the next read, instead of crashing the
  query — the resilience `_parse_notes` already had.
- *Graph cache — shared mutable notes.* `Note` is now `frozen=True`. The cache hands the same
  instances to every reader; immutability makes that sharing provably safe (no reader can corrupt a
  cached note), and no code mutated a note in place, so freezing is behavior-preserving.
- *Evidence score default.* `EvidenceChunk.score` defaults to a neutral **0.5** (was 0.0). Every
  current retriever sets it explicitly; the default only governs a future retriever that forgets to,
  and neutral keeps such a chunk mid-ranking instead of silently pinning it last-and-truncated.
- *Admission-permit release is now tested.* A test runs three sequential turns against a single
  permit and asserts all succeed and the permit returns — guarding the `finally: release()` whose
  regression would silently collapse capacity.
- *Kept as-is:* the permit is acquired in the handler (not inside the SSE generator) so a shed turn
  can return a clean **503** before the response starts — moving the acquire into the generator, as
  one suggested, would break that. The only leak path (response created but never iterated) needs an
  exotic failure between endpoint return and `response.__call__` under sse-starlette; accepted.

**Result.** `make lint type cov` green; `mypy --strict` clean. Tests: `test_research_tools.py`
(rank-before-truncate keeps the confident notes), `test_report.py` (`GraphRetriever` scores by
confidence), `test_graph.py` (cache reuse + fingerprint-bust + disable + vanished-file tolerance),
`test_note.py` (note is immutable), `test_audit.py` (revision stamped), `test_service.py` (503 at
zero capacity + permit released across sequential turns). New config: `retrieval_default_confidence`,
`graph_cache_enabled`, `deployment_revision`, `service_max_concurrent_turns`,
`service_turn_admission_timeout_seconds`; new migration `infra/sql/010_audit_revision.sql`.
