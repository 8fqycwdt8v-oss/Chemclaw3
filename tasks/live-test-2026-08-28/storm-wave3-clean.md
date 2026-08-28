# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

- **families planned / ran**: 9 / 9
- **mock requests served**: 627
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 579 s
- **disk free**: 20 GB

## Coverage

**9/9 planned families ran.**

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
| T | every advertised tool, called once with arguments it would accept | 36 |

## A · admission cap swept (SCALE-3)

Offered load held at 48 concurrent, 48 turns per step; the front door restarted at each cap.

| cap | accepted | shed/error | p50 s | p95 s | answered/s | offered drained/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 6 | 42 | 6.0 | 6.0 | 0.79 | 6.43 |
| 4 | 8 | 40 | 6.0 | 7.0 | 1.07 | 6.42 |
| 8 | 16 | 32 | 8.9 | 10.5 | 1.29 | 5.17 |
| 16 | 16 | 32 | 7.2 | 10.2 | 1.53 | 4.59 |
| 32 | 32 | 16 | 19.0 | 19.2 | 1.70 | 2.55 |

The last column is not throughput — it counts a shed turn as a drained one, so refusing fast reads as going fast. `answered/s` is the measurement.

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| C | c-whole: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-fragmented: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-parallel: announcements match results (6 expected) | PASS | announced/returned per turn: ['6/6', '6/6', '6/6'] |
| D | a completed job is findable through find_past_jobs afterwards | PASS | 1 tool result(s); result[0]="[JobRecordSummary(job_id='calc-compute_reaction_energy-6b6db76ea3d1cb6" |
| D | 16 simultaneous identical launches produce exactly one run | PASS | 1 job_records row(s) written across 16 simultaneous turns |
| D | the collision computed at most one result set | PASS | calculation_results 6 → 6 (one cold run writes ~3-6 rows) |
| F | an unparseable argument document is reported, not swallowed | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['find_notes'], result[0]=None |
| F | a truncated argument document is completed and run — the tool sees the cut value | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | LOAD-1's own shape is visible rather than counted as a call | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['find_notes'], result[0]=None |
| F | a tool the system does not have fails loudly | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['tool_that_does_not_exist'], result[0]=None |
| F | an empty function name (STREAM-1) does not kill the turn silently | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[''], result[0]=None |
| F | a 100 KB argument document is survived, not refused | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | forty parallel calls in one turn are survived | PASS | HTTP 200, answered=True, error=None, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | a turn that writes nothing reports empty_answer | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | an upstream model outage reaches the asker as an error | PASS | HTTP 200, answered=False, error=internal, tools_failed=[], result[0]=None |
| G | a message over 100000 chars is refused | PASS | HTTP 422 |
| G | the per-user event-stream cap refuses with 429 | PASS | codes [200, 200, 200, 200, 200, 429, 429, 429] |
| H | unicode survives the round trip through Postgres | PASS | 2 session_messages row(s) hold the exact string; answered=True |
| H | an injection string is treated as a search string | PASS | audit_events 496 → 497 (a dropped table reads as 0) |
| H | an unparseable reaction SMILES does not kill the turn | PASS | HTTP 200, answered=False, error=empty_answer, result[0]='chunks=[EvidenceChunk(content=\'<retrieved-note-ca0c64a0e27385a4 id="co' |
| H | arguments that parse and cannot be true are refused, not answered | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['compute_reaction_energy'], result[0]=None |
| T | t-chem-identity: every announced call came back | PASS | status=200 announced=4 returned=4 failed=[] |
| T | t-chem-species: every announced call came back | PASS | status=200 announced=4 returned=4 failed=[] |
| T | t-chem-degradation: every announced call came back | PASS | status=200 announced=2 returned=2 failed=[] |
| T | t-chem-batch: every announced call came back | PASS | status=200 announced=2 returned=2 failed=[] |
| T | t-safety-screen: every announced call came back | PASS | status=200 announced=3 returned=3 failed=[] |
| T | t-calc-properties: every announced call came back | PASS | status=200 announced=5 returned=5 failed=[] |
| T | t-calc-electronic: every announced call came back | **FAIL** | status=200 announced=4 returned=2 failed=['compute_atomic_descriptors', 'compute_surface_potential'] |
| T | t-calc-geometry: every announced call came back | PASS | status=200 announced=2 returned=2 failed=[] |
| T | t-calc-ledger: every announced call came back | **FAIL** | status=200 announced=3 returned=2 failed=['calculator_outliers'] |
| T | t-calc-record: every announced call came back | PASS | status=200 announced=1 returned=1 failed=[] |
| T | t-molfp-search: every announced call came back | PASS | status=200 announced=2 returned=2 failed=[] |
| T | t-rxnfp-similar: every announced call came back | PASS | status=200 announced=4 returned=4 failed=[] |
| T | t-rxnfp-precedent: every announced call came back | PASS | status=200 announced=3 returned=3 failed=[] |
| T | t-bo-inline: every announced call came back | **FAIL** | status=200 announced=4 returned=3 failed=['campaign_progress'] |
| T | t-memory: every announced call came back | PASS | status=200 announced=4 returned=4 failed=[] |
| T | t-watches: every announced call came back | PASS | status=200 announced=3 returned=3 failed=[] |
| T | t-knowledge-read: every announced call came back | PASS | status=200 announced=2 returned=2 failed=[] |
| T | t-scratchpad: every announced call came back | PASS | status=200 announced=4 returned=4 failed=[] |
| T | t-attachments: every announced call came back | PASS | status=200 announced=1 returned=1 failed=[] |
| T | t-unknown-reference: an unresolvable reference does not kill the turn | PASS | status=200 returned=1 failed=['read_attachment', 'get_durable_job_status', 'fetch_artifact', 'resume_campaign'] error=None |
| T | t-scratchpad-edit: an unresolvable reference does not kill the turn | PASS | status=200 returned=0 failed=['read_file', 'edit_file'] error=empty_answer |
| T | t-clarify: an unresolvable reference does not kill the turn | PASS | status=200 returned=1 failed=[] error=empty_answer |
| T | t-job-calc-screens: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=3 refused=0 failed=['compare_solvents', 'rank_species', 'rank_species_across_solvents'] |
| T | t-job-calc-conformers: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=3 refused=0 failed=['sample_conformers', 'refine_ensemble', 'compute_ensemble_property'] |
| T | t-job-calc-coordinates: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=2 refused=0 failed=['scan_coordinate', 'profile_rotation'] |
| T | t-job-calc-association: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=2 refused=0 failed=['predict_pka_ensemble', 'compute_interaction_energy'] |
| T | t-job-calc-bonds: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=1 refused=0 failed=['survey_bond_strengths'] |
| T | t-job-bo-campaign: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=1 refused=0 failed=['start_optimization_campaign'] |
| T | t-job-results: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=1 refused=0 failed=['republish_calculations'] |
| T | t-job-report: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=1 refused=0 failed=['request_development_report'] |
| T | t-knowledge-write: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=3 refused=0 failed=['propose_knowledge_note', 'record_confirmed_answer', 'record_failure'] |
| T | t-memory-synthesis: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=1 refused=0 failed=['synthesize_memory'] |
| T | t-template-species: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=3 refused=0 failed=['run_tautomer_resolution', 'run_microspecies_profile', 'run_stereoisomer_ranking'] |
| T | t-template-conformers: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=3 refused=0 failed=['run_conformer_refinement', 'run_ensemble_free_energy', 'run_regioselectivity_in_conformer'] |
| T | t-template-safety: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=2 refused=0 failed=['run_hazard_briefing', 'run_degradant_triage'] |
| T | t-template-bonds: every launcher is refused on a dry-run turn | **FAIL** | status=200 announced=1 refused=0 failed=['run_bond_strength_survey'] |
| A | every offered turn is accounted for at every cap | PASS | 5 cap(s) swept, 0 with unaccounted turns |
| A | the admission cap is load-bearing (goodput rises with it) | PASS | cap 2: 0.79 answered/s → cap 32: 1.70 answered/s |
| A | the sweep's own noise is small enough to read a knee against | **FAIL** | largest within-cap spread 21% over 3 sample(s) per cap |
| A | the sweep resolves the knee rather than running out of range | **FAIL** | no cap in (2, 4, 8, 16, 32) stops paying by more than the 21% noise floor — the sweep's top is a limit of the sweep, not of the system |
| E | a disconnected session accepts a new turn without waiting out the lease | **FAIL** | accepted after 25.3s (lease is 60.0s); status codes [409, 409, 409, 409] |
| E | a job survives its connector worker being SIGKILLed mid-flight | **FAIL** | at kill: FAILED; after restart: FAILED 0s later (heartbeat timeout is 600s); job_records rows: 1 |
| E | the front door recovers from a Postgres restart without being restarted itself | PASS | postmaster start time '2026-08-28 16:42:13.326433+00:00' -> '2026-08-28 17:00:35.606945+00:00' (restarted); 23/24 in-flight turns survived the bounce; a fresh turn answered 1.8s after it |
| E | a durable launch with no broker reaches the asker as an error, not as an answer | PASS | HTTP 200, answered=True, error=None, tools_failed=['compute_reaction_energy'], result[0]=None |
| B | find_notes bodies ran | PASS | 713 audited call(s) |
| B | gather_evidence bodies ran | PASS | 4 audited call(s) |
| B | expand_note bodies ran | PASS | 2 audited call(s) |

**47/68 checks passed**, over the families that ran.
