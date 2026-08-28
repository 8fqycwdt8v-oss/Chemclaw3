# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

- **families planned / ran**: 10 / 10
- **turns driven / mock requests served**: 819 / 599
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 1099 s
- **disk free**: 20 GB

## Coverage

**10/10 planned families ran.**

| family | what it covers | checks |
| --- | --- | ---: |
| A | volume, and the admission cap swept end to end | 4 |
| B | tool bodies really ran, asked of the audit trail | 3 |
| C | the same call whole, fragmented, and in parallel | 3 |
| D | identical durable launches colliding | 3 |
| E | chaos — disconnects, killed workers, a bounced database, a dead broker | 4 |
| F | adversarial model output a real model will not produce on request | 9 |
| G | the front door's own limits, asked for deliberately | 2 |
| H | pathological data: bad chemistry, impossible arguments, unicode, injection | 4 |
| T | every advertised tool, called once with arguments it would accept | 37 |
| M | the lane itself: every model call served by the mock, not by a real endpoint | 1 |

## A · admission cap swept (SCALE-3)

Offered load held at 48 concurrent, 48 turns per step; the front door restarted at each cap.

| cap | accepted | shed/error | p50 s | p95 s | answered/s | offered drained/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 4 | 44 | 6.0 | 6.2 | 0.58 | 6.98 |
| 4 | 8 | 40 | 6.8 | 9.5 | 0.83 | 5.25 |
| 8 | 8 | 40 | 6.9 | 7.8 | 0.96 | 5.74 |
| 16 | 16 | 32 | 8.9 | 13.8 | 1.15 | 3.45 |
| 32 | 32 | 16 | 25.7 | 26.2 | 1.21 | 1.82 |

The last column is not throughput — it counts a shed turn as a drained one, so refusing fast reads as going fast. `answered/s` is the measurement.

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| C | c-whole: exactly 1 call(s) announced, and all came back | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] (1 expected) |
| C | c-fragmented: exactly 1 call(s) announced, and all came back | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] (1 expected) |
| C | c-parallel: exactly 6 call(s) announced, and all came back | PASS | announced/returned per turn: ['6/6', '6/6', '6/6'] (6 expected) |
| D | a completed job is findable through find_past_jobs afterwards | PASS | 1 tool result(s); result[0]="[JobRecordSummary(job_id='calc-compute_reaction_energy-b649f72f3c8540b" |
| D | 12 simultaneous identical launches produce exactly one run | PASS | 1 job_records row(s) written across 12 simultaneous turns |
| D | the collision computed at most one result set | PASS | calculation_results 20 → 20 (one cold run writes ~3-6 rows) |
| F | an unparseable argument document is reported, not swallowed | PASS | HTTP 200, answered=False, error=empty_answer, announced=0/1 returned=0, tools_failed=['find_notes'], result[0]=None |
| F | a truncated argument document is completed and run — the tool sees the cut value | PASS | HTTP 200, answered=False, error=empty_answer, announced=1/1 returned=1, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | LOAD-1's own shape is visible rather than counted as a call | PASS | HTTP 200, answered=False, error=empty_answer, announced=1/1 returned=0, tools_failed=['find_notes'], result[0]=None |
| F | a tool the system does not have fails loudly | PASS | HTTP 200, answered=False, error=empty_answer, announced=1/1 returned=0, tools_failed=['tool_that_does_not_exist'], result[0]=None |
| F | an empty function name (STREAM-1) is reported, not swallowed | PASS | HTTP 200, answered=False, error=empty_answer, announced=1/1 returned=0, tools_failed=[''], result[0]=None |
| F | a 100 KB argument document is survived — the call came back | PASS | HTTP 200, answered=False, error=empty_answer, announced=1/1 returned=1, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | forty parallel calls in one turn are survived — all forty came back | PASS | HTTP 200, answered=True, error=None, announced=40/40 returned=40, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | a turn that writes nothing reports empty_answer | PASS | HTTP 200, answered=False, error=empty_answer, announced=1/1 returned=1, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | an upstream model outage reaches the asker as an error of its own | PASS | HTTP 200, answered=False, error=internal, announced=0/0 returned=0, tools_failed=[], result[0]=None |
| G | a message over 100000 chars is refused | PASS | HTTP 422 |
| G | the per-user event-stream cap refuses with 429 | PASS | codes [200, 200, 200, 200, 200, 429, 429, 429] |
| H | unicode survives the round trip through Postgres | PASS | 2 session_messages row(s) hold the exact string; answered=True |
| H | an injection string is searched for, and audit_events is still there | PASS | audit_events 4653 → 4654; the search itself is the 1 new row(s) |
| H | an unparseable reaction SMILES does not kill the turn — the call came back | PASS | HTTP 200, answered=False, error=empty_answer, announced=1 returned=1, result[0]='chunks=[EvidenceChunk(content=\'<retrieved-note-8538bdbfd1003194 id="co' |
| H | arguments that parse and cannot be true are refused, not answered | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['compute_reaction_energy'], result[0]=None |
| T | t-chem-identity: all 4 declared call(s) came back | PASS | status=200 announced=4/4 returned=4 failed=[] |
| T | t-chem-species: all 4 declared call(s) came back | PASS | status=200 announced=4/4 returned=4 failed=[] |
| T | t-chem-degradation: all 2 declared call(s) came back | PASS | status=200 announced=2/2 returned=2 failed=[] |
| T | t-chem-batch: all 2 declared call(s) came back | PASS | status=200 announced=2/2 returned=2 failed=[] |
| T | t-safety-screen: all 3 declared call(s) came back | PASS | status=200 announced=3/3 returned=3 failed=[] |
| T | t-calc-properties: all 5 declared call(s) came back | PASS | status=200 announced=5/5 returned=5 failed=[] |
| T | t-calc-electronic: all 2 declared call(s) came back | PASS | status=200 announced=2/2 returned=2 failed=[] |
| T | t-calc-xtb-descriptors: all 2 declared call(s) came back | **FAIL** | status=200 announced=2/2 returned=0 failed=['compute_atomic_descriptors', 'compute_surface_potential'] |
| T | t-calc-geometry: all 2 declared call(s) came back | PASS | status=200 announced=2/2 returned=2 failed=[] |
| T | t-calc-ledger: all 3 declared call(s) came back | PASS | status=200 announced=3/3 returned=3 failed=[] |
| T | t-calc-record: all 1 declared call(s) came back | PASS | status=200 announced=1/1 returned=1 failed=[] |
| T | t-molfp-search: all 2 declared call(s) came back | PASS | status=200 announced=2/2 returned=2 failed=[] |
| T | t-rxnfp-similar: all 4 declared call(s) came back | PASS | status=200 announced=4/4 returned=4 failed=[] |
| T | t-rxnfp-precedent: all 3 declared call(s) came back | PASS | status=200 announced=3/3 returned=3 failed=[] |
| T | t-bo-inline: all 4 declared call(s) came back | PASS | status=200 announced=4/4 returned=4 failed=[] |
| T | t-memory: all 4 declared call(s) came back | PASS | status=200 announced=4/4 returned=4 failed=[] |
| T | t-watches: all 3 declared call(s) came back | PASS | status=200 announced=3/3 returned=3 failed=[] |
| T | t-knowledge-read: all 2 declared call(s) came back | PASS | status=200 announced=2/2 returned=2 failed=[] |
| T | t-scratchpad: all 4 declared call(s) came back | PASS | status=200 announced=4/4 returned=4 failed=[] |
| T | t-attachments: all 1 declared call(s) came back | PASS | status=200 announced=1/1 returned=1 failed=[] |
| T | t-unknown-reference: all 5 unresolvable reference(s) were tried, and the turn survived | PASS | status=200 announced=5/5 returned=1 failed=['read_attachment', 'get_durable_job_status', 'fetch_artifact', 'resume_campaign'] error=None |
| T | t-scratchpad-edit: all 2 unresolvable reference(s) were tried, and the turn survived | PASS | status=200 announced=2/2 returned=0 failed=['read_file', 'edit_file'] error=empty_answer |
| T | t-clarify: all 1 unresolvable reference(s) were tried, and the turn survived | PASS | status=200 announced=1/1 returned=1 failed=[] error=empty_answer |
| T | t-job-calc-screens: all 3 launcher(s) refused on a dry-run turn | PASS | status=200 announced=3/3 refused=3 failed=['compare_solvents', 'rank_species', 'rank_species_across_solvents'] |
| T | t-job-calc-conformers: all 3 launcher(s) refused on a dry-run turn | PASS | status=200 announced=3/3 refused=3 failed=['sample_conformers', 'refine_ensemble', 'compute_ensemble_property'] |
| T | t-job-calc-coordinates: all 2 launcher(s) refused on a dry-run turn | PASS | status=200 announced=2/2 refused=2 failed=['scan_coordinate', 'profile_rotation'] |
| T | t-job-calc-association: all 2 launcher(s) refused on a dry-run turn | PASS | status=200 announced=2/2 refused=2 failed=['predict_pka_ensemble', 'compute_interaction_energy'] |
| T | t-job-calc-bonds: all 1 launcher(s) refused on a dry-run turn | PASS | status=200 announced=1/1 refused=1 failed=['survey_bond_strengths'] |
| T | t-job-bo-campaign: all 1 launcher(s) refused on a dry-run turn | PASS | status=200 announced=1/1 refused=1 failed=['start_optimization_campaign'] |
| T | t-job-results: all 1 launcher(s) refused on a dry-run turn | PASS | status=200 announced=1/1 refused=1 failed=['republish_calculations'] |
| T | t-job-report: all 1 launcher(s) refused on a dry-run turn | PASS | status=200 announced=1/1 refused=1 failed=['request_development_report'] |
| T | t-knowledge-write: all 3 launcher(s) refused on a dry-run turn | PASS | status=200 announced=3/3 refused=3 failed=['propose_knowledge_note', 'record_confirmed_answer', 'record_failure'] |
| T | t-memory-synthesis: all 1 launcher(s) refused on a dry-run turn | PASS | status=200 announced=1/1 refused=1 failed=['synthesize_memory'] |
| T | t-template-species: all 3 launcher(s) refused on a dry-run turn | PASS | status=200 announced=3/3 refused=3 failed=['run_tautomer_resolution', 'run_microspecies_profile', 'run_stereoisomer_ranking'] |
| T | t-template-conformers: all 3 launcher(s) refused on a dry-run turn | PASS | status=200 announced=3/3 refused=3 failed=['run_conformer_refinement', 'run_ensemble_free_energy', 'run_regioselectivity_in_conformer'] |
| T | t-template-safety: all 2 launcher(s) refused on a dry-run turn | PASS | status=200 announced=2/2 refused=2 failed=['run_hazard_briefing', 'run_degradant_triage'] |
| T | t-template-bonds: all 1 launcher(s) refused on a dry-run turn | PASS | status=200 announced=1/1 refused=1 failed=['run_bond_strength_survey'] |
| A | every offered turn ended with an answer or a stated reason | PASS | 5 cap(s) swept, 0 turn(s) that neither answered nor reported why (dropped or silently empty) |
| A | the admission cap is load-bearing (goodput rises with it) | PASS | cap 2: 0.58 answered/s → cap 32: 1.21 answered/s |
| A | the sweep's own noise is small enough to read a knee against | PASS | largest within-cap spread 14% over 3 sample(s) per cap (ceiling 15%, minimum 2 sample(s)) |
| A | the sweep resolves the knee rather than running out of range | PASS | goodput stops improving at cap 16 (steps must beat the 14% noise floor over 3 samples) |
| E | a disconnected session accepts a new turn without waiting out the lease | PASS | accepted after 10.7s (lease is 60.0s, the detached turn thinks for ~8s); status codes [409, 409, 409, 409] |
| E | a job survives its connector worker being SIGKILLed mid-flight | PASS | at kill: RUNNING, activity held by 6785@vm; after restart: COMPLETED 584s later (heartbeat timeout is 600s); job_records rows: 1 |
| E | the front door recovers from a Postgres restart without being restarted itself | PASS | postmaster start time '2026-08-28 22:24:33.968004+00:00' -> '2026-08-28 22:50:59.148786+00:00' (restarted); 23/24 in-flight turns survived the bounce; a fresh turn answered 2.0s after it |
| E | a durable launch with no broker reaches the asker as an error, not as an answer | PASS | broker stopped; HTTP 200, answered=True, error=None, tools_failed=['compute_reaction_energy'], result[0]=None |
| B | find_notes bodies ran | PASS | 1 audited call(s) during this run (3285 → 3286 lifetime) |
| B | gather_evidence bodies ran | PASS | 1 audited call(s) during this run (55 → 56 lifetime) |
| B | expand_note bodies ran | PASS | 1 audited call(s) during this run (28 → 29 lifetime) |
| M | every model call this run made was served by the mock | **FAIL** | 599 mock request(s) served against 819 turn(s) driven |

**68/70 checks passed**, over the families that ran.
