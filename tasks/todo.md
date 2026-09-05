# Implementing the 200-user performance findings

Source: `docs/archive/REVIEW-2026-09-04-performance-at-200-users.md`. Every item below names the
measurement that justifies it; a fix that does not move its number is not done.

## Done (lead, committed dc9d2a9)

- [x] **Generic plans on pooled connections.** `plan_cache_mode=force_custom_plan` in
      `core/db._merged_options`. Dense note query 1,280 ms -> 9 ms at 100k chunks; measured cost on
      a point lookup 135.7 -> 140.5 us. Both candidate remedies measured before choosing;
      `prepare_threshold=None` rejected because it discards the parse cache too. Checkpointer pool
      deliberately excluded (PK lookups, generic is correct and cheaper there).
- [x] **One TLS trust store per process.** `core.http.default_ssl_context`, wired into both client
      factories. 7 clients per turn: 110 ms -> 0.26 ms (up to 424x), blocking loop CPU.

## In flight (seven agents, disjoint file ownership)

- [ ] **CORE-A** floor ratchet blind to connector tools (42,730 measured vs ~74,700 shipped);
      compaction trigger floored to 1; prefix byte-stability for server-side caching; executor
      sizing vs 64-way fan-out; lazy helper roster.
- [ ] **CORE-B** saturation as a third retry category, both sides of the wire; `queue_wait_timeout`
      at the three bundle call sites; the 60 s `schedule_to_close` that drops push-back and job
      records; cross-process single-flight (assess).
- [ ] **CORE-C** `reaction_records(reaction_id)` index; agent-callable leading-wildcard ILIKE;
      fingerprint HNSW predicates; KG scan off the loop; retention's unbounded DELETE and its
      non-converging pass cap; keyset pagination in backfill.
- [ ] **CORE-D** HPA on permit occupancy not CPU (218 millicores at 100% saturation); the shed made
      alertable; the liveness cascade and the missing stream-vs-socket cross-check; fleet Postgres
      arithmetic counting 1 pool per process where a process holds 3; retention absent from the
      chart; capacity defaults.
- [ ] **MCP-A** memoised jsonschema validators (7.071 -> 0.008 ms); cgroup-aware default executor;
      session idle reaping; the request log's time-to-headers duration.
- [ ] **MCP-B** calc singleton (replicas/HPA/PDB/grace); fleet-wide missing PDB/grace/spread;
      timeouts that can never fire; pyexec rlimit vs pod limit; unbounded emptyDir; the false
      RDKit-GIL justification.
- [ ] **UI-A** BFF socket pool below the streams it holds (512 < 600); uncompressed assets
      (634,903 -> 194,190 B); the fixed-interval recovery herd; no pagehide turn cancel; visibility
      gating.

## Verification plan

- Each agent runs lint + `mypy --strict` + the tests covering its modules, and reports the exact
  result. Postgres and Temporal are up, so Postgres-backed tests actually execute.
- Lead then runs `make lint type test` per repo and reports what it skipped, per the repo rule that
  a local green line is not evidence about the Postgres-backed set.
- Re-measure the headline numbers after merge rather than trusting the per-agent claims.

## Review

_(to be written when the work lands: what moved, what did not, what was skipped and why)_
