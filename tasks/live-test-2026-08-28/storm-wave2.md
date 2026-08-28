# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

- **families planned / ran**: 8 / 8
- **mock requests served**: 513
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 1161 s
- **disk free**: 20 GB

## Coverage

**8/8 planned families ran.**

| family | what it covers | checks |
| --- | --- | ---: |
| A | volume, and the admission cap swept end to end | 4 |
| B | tool bodies really ran, asked of the audit trail | 3 |
| C | the same call whole, fragmented, and in parallel | 3 |
| D | identical durable launches colliding | 3 |
| E | chaos — disconnects, killed workers, a bounced database, a dead broker | 4 |
| F | adversarial model output a real model will not produce on request | 8 |
| G | the front door's own limits, asked for deliberately | 2 |
| H | pathological data: bad chemistry, impossible arguments, unicode, injection | 4 |

## A · admission cap swept (SCALE-3)

Offered load held at 48 concurrent, 48 turns per step; the front door restarted at each cap.

| cap | accepted | shed/error | p50 s | p95 s | answered/s | offered drained/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 4 | 44 | 5.8 | 5.9 | 0.61 | 7.17 |
| 4 | 6 | 42 | 6.5 | 6.6 | 0.90 | 5.82 |
| 8 | 8 | 40 | 6.7 | 7.3 | 1.01 | 5.39 |
| 16 | 16 | 32 | 8.0 | 11.9 | 1.32 | 3.95 |
| 32 | 32 | 16 | 23.1 | 23.5 | 1.36 | 2.03 |

The last column is not throughput — it counts a shed turn as a drained one, so refusing fast reads as going fast. `answered/s` is the measurement.

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| C | c-whole: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-fragmented: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-parallel: announcements match results (6 expected) | PASS | announced/returned per turn: ['6/6', '6/6', '6/6'] |
| D | a completed job is findable through find_past_jobs afterwards | PASS | 1 tool result(s); result[0]="[JobRecordSummary(job_id='calc-compute_reaction_energy-d09c2f31d8601d8" |
| D | 16 simultaneous identical launches produce exactly one run | PASS | 1 job_records row(s) written across 16 simultaneous turns |
| D | the collision computed at most one result set | PASS | calculation_results 6 → 6 (one cold run writes ~3-6 rows) |
| F | a truncated argument document is reported, not swallowed | **FAIL** | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
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
| H | an injection string is treated as a search string | PASS | audit_events 129 → 130 (a dropped table reads as 0) |
| H | an unparseable reaction SMILES does not kill the turn | PASS | HTTP 200, answered=False, error=empty_answer, result[0]='chunks=[EvidenceChunk(content=\'<retrieved-note-988eb71190380016 id="co' |
| H | arguments that parse and cannot be true are refused, not answered | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['compute_reaction_energy'], result[0]=None |
| A | every offered turn is accounted for at every cap | PASS | 5 cap(s) swept, 0 with unaccounted turns |
| A | the admission cap is load-bearing (goodput rises with it) | PASS | cap 2: 0.61 answered/s → cap 32: 1.36 answered/s |
| A | the sweep's own noise is small enough to read a knee against | **FAIL** | largest within-cap spread 26% over 3 sample(s) per cap |
| A | the sweep resolves the knee rather than running out of range | **FAIL** | no cap in (2, 4, 8, 16, 32) stops paying by more than the 26% noise floor — the sweep's top is a limit of the sweep, not of the system |
| E | a disconnected session accepts a new turn without waiting out the lease | **FAIL** | accepted after 10.4s (lease is 60.0s); status codes [409, 409, 409, 409] |
| E | a job survives its connector worker being SIGKILLed mid-flight | **FAIL** | at kill: RUNNING; after restart: FAILED 583s later (heartbeat timeout is 600s); job_records rows: 1 |
| E | the front door recovers from a Postgres restart without being restarted itself | PASS | postmaster start time '2026-08-28 15:44:54.292064+00:00' -> '2026-08-28 16:42:13.326433+00:00' (restarted); 23/24 in-flight turns survived the bounce; a fresh turn answered 2.0s after it |
| E | a durable launch with no broker reaches the asker as an error, not as an answer | PASS | HTTP 200, answered=True, error=None, tools_failed=['compute_reaction_energy'], result[0]=None |
| B | find_notes bodies ran | PASS | 350 audited call(s) |
| B | gather_evidence bodies ran | PASS | 2 audited call(s) |
| B | expand_note bodies ran | PASS | 1 audited call(s) |

**26/31 checks passed**, over the families that ran.
