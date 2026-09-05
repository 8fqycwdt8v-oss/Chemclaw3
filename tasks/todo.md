# The capture half of the knowledge loop

Follow-on to `D-2026-09-04-a-ranker-that-sorts-alphabetically-is-not-a-ranker`, which closed the
retrieval half and said plainly what it had not done: **data is captured automatically, conclusions
are not.** All four review claims re-verified against `HEAD` before building — the tree had moved
twice — and all four held.

## Done

- [x] 1. **Nine `run_*` procedures wrote no durable record.** `record_job` had one caller in the
      tree (`durable/connector_job.py`), so a template run left no `job_records` row: never
      findable by `find_past_jobs`, and `get_durable_job_status` answered for its id only until
      Temporal retained its history away. A *failing* run left nothing anywhere.
      `TemplateWorkflow` now records on both paths. Proven on a real broker, not just by the
      builders: removing the success-path call makes `tests/test_template_job_record.py` report
      "got 1" instead of 2.
- [x] 2. **Two docstrings asserted the opposite in the present tense** (`agent/durable_tools.py`) —
      both true of connector jobs alone. Corrected, and `find_past_jobs` now documents the
      `connector="template"` filter.
- [x] 3. **A correction was recorded as a confirmation.** `memory/interaction.py` rendered
      `A (confirmed):` unconditionally while three docstrings and the system prompt said
      "confirmed **or corrected**". `corrected_from` carries what the system had said; empty means
      confirmed.
- [x] 4. **The recording rule had no trigger.** "A computed value that matters beyond the
      conversation" named no moment; now a comparison whose margin *clears* the stated uncertainty
      does, with the inside-the-error-bar case pointed at the ceiling section.
- [x] 5. **Nothing graded the write-up after a calculation.** `propose_knowledge_note` is named by
      fourteen probes across seven files and by none in `durable.yaml` or
      `multistep-calculation.yaml`. Two new probes, `ms-18` and `ms-19`.

## Rejected, with the reasoning kept

- [x] 6. **An automatic `publish_to_graph` over calc's twelve durable jobs.** Designed, reviewed and
      **not built** — two of its premises were false (`job-result` *is* minted, by
      `propose_knowledge_note`; the record does *not* stop at the cache, `_publish_result` runs for
      every job), `skills/computational-evidence` already forbids it in as many words, roughly half
      the notes would have read "this calculation could not distinguish them" at GFN2-xTB's ±3
      kcal/mol, and neither default is defensible. The ADR keeps the whole argument so it is not
      re-proposed from scratch.

## Two things measurement changed

**The eval fix as proposed would have made the probes weaker.** The recommendation was to add
`propose_knowledge_note` to `expects_tools` on `ms-07`/`ms-08`. `evals/live.py` scores that field
with `any()`, so a second name makes a probe pass on *either* tool — `ms-07` would then have been
satisfied by a turn that recorded a note and never ranked anything. Separate probes instead.

**`turn_costs` already is the per-turn outcome row**, so the "no end-of-turn record" finding was
half wrong: `tool_calls`, `tool_failures`, `jobs_started` and `outcome` are written every turn.
What is missing is the knowledge dimensions (did this turn retrieve, cite, capture) — a much
cheaper change than the new table that was proposed, and queued rather than rushed at the end of
this one.

## Cost, stated

The context floor moves 43,063 -> 43,316 against the unraised 43,500 ceiling: **184 tokens of
headroom**, from one optional argument on `record_confirmed_answer`. That is tight enough to be the
next person's problem, and the reclaim is already a `BACKLOG.md` row.

---

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

All seven workstreams landed. `make lint` and `make type` are green over 804 files in `Chemclaw3`;
`Chemclaw3-mcp` is 1634 passed / 7 skipped; `Chemclaw3_ui` is 855 passed over 87 files.

### What moved, measured

| | before | after |
|---|---|---|
| Dense note query, 100k chunks (generic plan) | 1,280 ms | 9 ms |
| Seven connector clients per turn (CA parse) | 110 ms | 0.26 ms |
| One MCP tool call, output-schema validation | 16.89 ms p50 | 3.96 ms p50 |
| `job_records` ILIKE miss, 500k rows | 1,036 ms | 1.09 ms |
| `reaction_records`, 50 ids | 152.3 ms / 217 MB | 0.80 ms / 203 buffers |
| `all_records` sort, 200k rows | 2,228 ms (136 MB spill) | 10.7 ms |
| Retention DELETE, 300k rows under a 5 s timeout | 0 rows removed | all 300,000 in 11.04 s |
| Loop lag during a graph build | 52.2 ms | 16.6 ms |
| KG scan loop lag at concurrency 8 | p50 418 ms | p50 26 ms |
| Frontend cold load per user | 797,679 B | 268,615 B |
| BFF healthz with 600 streams held | never answered | 11 ms |

### What was declined, and why

- **Fingerprint HNSW restructure.** 14x faster and returns a *different result set* for 22 of 60
  queries — ties, not recall. Exact-versus-approximate is a decision, not a patch; it wants an ADR.
- **Two derived queue bounds as settings.** A setting could name a bound above the budget it must
  stay below; the derivation cannot express that.
- **A lazy helper roster.** Saves nothing on a delegating turn and turns a build-time check into a
  promise. Building the whole graph off the loop was taken instead.
- **Cross-process single-flight.** Its `DEFERRED.md` trigger is not tripped at two processes, and
  the obvious remedy starves the pool (8 advisory locks -> `PoolTimeout` in 5.00 s).
- **Lowering `CHEMCLAW_CREST_THREADS`.** Doubles an hours-long search to free one slot.

### Where the review itself was wrong

Six of the seven agents corrected a claim in it, and in every case the error was mine: a figure
carried from a track report into the synthesis without being re-derived. The *magnitudes* held —
880x, 424x, the 42,730-vs-75,695 prefix gap — while three of the **mechanisms** did not.

- "Nothing prunes by default" — the chart *refuses to render* without a retention posture.
- "Input and structured output are validated" — input validation is off; the win is output only.
- "A 16x timeout mismatch" — no reader; the real defect was worse, all three budgets shipped *equal*.
- "A pod running CREST is busy at low CPU" — it draws ~4 cores, so CPU does lead saturation there.
- "Key the validators by identity" — unsafe; addresses are reused across `list_tools` calls.
- "Moving the build off the loop takes all 42.5 ms" — it takes about two thirds.

Corrections are in the review document, in place, each with the measurement that overturned it.

### Costs a human must accept

- Front-door CPU request 500m -> 1 (3 -> 6 CPUs reserved at max replicas).
- MCP fleet baseline ~0.7 CPU / 2 Gi -> **8 CPU / 10.5 Gi**, HPA ceiling 23 CPU / 26.5 Gi.
- Postgres must be provisioned for 288 connections; the old 136 was never real (~208 already).
- Fleet turn ceiling 48 -> 72, **and the HPA can now reach it** — real LLM spend.
- ~112 MB of new index; UI image +13 MB.
- A saturated-backend calc job now takes ~28 min of backoff before failing instead of failing at once.

### Still open

- An ADR for the exact-versus-approximate fingerprint decision.
- Four `bo` tools are 12,055 tokens of the prefix — four inlined copies of one model.
- `CHEMCLAW_FRAMING_ENVELOPE_SECRET` must be set for server-side prefix caching to hit across pods.
- `_DELETE_BATCH_ROWS` is a module constant; promote to config if the batch size should be tunable.
- A `serverConfig.test.ts` flake in the UI, pre-existing, ordering-dependent.
