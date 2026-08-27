# Knowledge-system review: implement all findings (2026-08-27)

The plan for implementing `docs/archive/REVIEW-2026-08-27-knowledge-system-analysis.md` in full,
as one branch (`claude/knowledge-system-analysis-3xwwc3`) of ordered, per-theme commits.

## Work packages (all done)

- [x] WP1 — relation directions: `RELATION_SIGNATURES`, `validate()` enforcement, corpus edges
      re-authored compound-side, `test_seed_corpus` un-pins the inversion
- [x] WP2 — note schema: `extra="forbid"` on nested models, directory-matches-type check,
      surrogate walk into `conditions`, malformed-target naming
- [x] WP3 — corpus content fixes, `valid_from` dates, `knowledge/README.md` rewrite
- [x] WP4 — kg core: conflict scan rewritten output-sensitive (3111 ms → 11 ms at 4k dated
      notes), `_LAST_SCAN` stamped after the cache write, list copies out of the TTL cache,
      per-directory index locks
- [x] WP5 — report renderer: partial-failure sections render evidence plus the incomplete marker
- [x] WP6 — retrieval: `search_text` covers conditions/source, GraphRetriever ranks before the
      cut, `query_terms` floor honored in the fallback
- [x] WP7 — silent-zero class: `RetrieverSkip` third channel, `EvidenceSweep.sources` /
      `sources_skipped`, `note_reindex_effective` derivation, `find_notes` widening +
      `total_matches`, embedding batch chunking + `note_embedding_key`
- [x] WP8 — PR-gate honesty (`D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed`):
      gate-commit trailer + foreign-tip refusal, `GitRemoteError` retryable class, one open row
      per note (`superseded`, migration 057), `SubmissionOutcome`, dependencies never overwrite,
      cross-pod advisory lock, `make proposals-reconcile`
- [x] WP9 — memory loop (`D-2026-08-27-a-retirement-rides-its-replacement`): `SynthesisUnit`
      pairing, real `superseded-by` edge, partial-read retirement skip, store-seeded promotion
      dedup, truthful promotion summary, `make synthesize`
- [x] WP10 — grounding + existence: `groundable_ids` (document citations ground), `calc_refs`
      existence checked against the calculation store in `kg-validate`
- [x] WP11 — hygiene: warehouse-retriever tests hermetic (10 hard failures offline → 0), seed
      corpus stops citing calculations no store holds, BACKLOG rows closed/narrowed, ADRs +
      ledger, full gate, PR + auto-merge

## Review

- Verification ran with the infrastructure up (dockerd + `make up` + `make db-migrate`), so the
  Postgres- and Temporal-backed slices genuinely ran rather than skipping.
- `make lint`, `make type` (src + tests, 681 files) and the suite are green; every validator
  except `helm-validate` runs green locally — helm is not installed in this sandbox (documented
  live edge; the chart is untouched by this branch).
- Two flaky tests observed are pre-existing and unrelated (verified via `git stash`):
  `test_deploy_chart.py::test_the_fleet_ceiling_…` (order-dependent) and
  `test_connector_transport.py::test_a_bundles_startup_report_…` (timing under load).
- The new `calc_refs` existence gate immediately caught the seed corpus citing fabricated keys —
  the gate finding a real instance of the defect class it was built for; the corpus now states
  in prose why its refs are empty, which is the discipline a real note is held to.
