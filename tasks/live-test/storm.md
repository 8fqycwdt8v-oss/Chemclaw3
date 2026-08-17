# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

- **families planned / ran**: 8 / 8
- **mock requests served**: 516
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 1306 s
- **disk free**: 21 GB

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
| 2 | 4 | 44 | 6.2 | 6.2 | 0.58 | 6.93 |
| 4 | 7 | 41 | 7.4 | 8.8 | 0.80 | 4.87 |
| 8 | 10 | 38 | 8.2 | 9.2 | 0.93 | 4.48 |
| 16 | 16 | 32 | 12.5 | 14.4 | 1.11 | 3.32 |
| 32 | 32 | 16 | 27.3 | 27.7 | 1.15 | 1.73 |

The last column is not throughput — it counts a shed turn as a drained one, so refusing fast reads as going fast. `answered/s` is the measurement.

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| C | c-whole: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-fragmented: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-parallel: announcements match results (6 expected) | PASS | announced/returned per turn: ['6/6', '6/6', '6/6'] |
| D | a completed job is findable through find_past_jobs afterwards | PASS | 1 tool result(s); result[0]="[JobRecordSummary(job_id='calc-compute_reaction_energy-b237f1649cb8326" |
| D | 12 simultaneous identical launches produce exactly one run | **FAIL** | 0 job_records row(s) written across 12 simultaneous turns |
| D | the collision computed at most one result set | PASS | calculation_results 51 → 51 (one cold run writes ~3-6 rows) |
| F | a truncated argument document is reported, not swallowed | **FAIL** | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='' |
| F | LOAD-1's own shape is visible rather than counted as a call | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['find_notes'], result[0]=None |
| F | a tool the system does not have fails loudly | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['tool_that_does_not_exist'], result[0]=None |
| F | an empty function name (STREAM-1) does not kill the turn silently | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[''], result[0]=None |
| F | a 100 KB argument document is survived, not refused | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='' |
| F | forty parallel calls in one turn are survived | PASS | HTTP 200, answered=True, error=None, tools_failed=[], result[0]='' |
| F | a turn that writes nothing reports empty_answer | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='' |
| F | an upstream model outage reaches the asker as an error | PASS | HTTP 200, answered=False, error=internal, tools_failed=[], result[0]=None |
| G | a message over 100000 chars is refused | PASS | HTTP 422 |
| G | the per-user event-stream cap refuses with 429 | PASS | codes [200, 200, 200, 200, 200, 429, 429, 429] |
| H | unicode survives the round trip through Postgres | PASS | 2 session_messages row(s) hold the exact string; answered=True |
| H | an injection string is treated as a search string | PASS | audit_events 718 → 719 (a dropped table reads as 0) |
| H | an unparseable reaction SMILES does not kill the turn | PASS | HTTP 200, answered=False, error=empty_answer, result[0]='[EvidenceChunk(content=\'<retrieved-note-128be78aef65c4e4 id="compound-' |
| H | arguments that parse and cannot be true are refused, not answered | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['compute_reaction_energy'], result[0]=None |
| A | every offered turn is accounted for at every cap | PASS | 5 cap(s) swept, 0 with unaccounted turns |
| A | the admission cap is load-bearing (goodput rises with it) | PASS | cap 2: 0.58 answered/s → cap 32: 1.15 answered/s |
| A | the sweep's own noise is small enough to read a knee against | PASS | largest within-cap spread 13% over 3 sample(s) per cap |
| A | the sweep resolves the knee rather than running out of range | PASS | goodput stops improving at cap 16 (steps must beat the 13% noise floor) |
| E | a disconnected session accepts a new turn without waiting out the lease | PASS | accepted after 0.2s (lease is 60.0s); status codes [409, 200] |
| E | a job survives its connector worker being SIGKILLed mid-flight | **FAIL** | at kill: RUNNING; after restart: FAILED 597s later (heartbeat timeout is 600s); job_records rows: 0 |
| E | the front door recovers from a Postgres restart without being restarted itself | PASS | 24/24 in-flight turns survived the bounce; a fresh turn answered 2.1s after it |
| E | broker outage | **FAIL** | the check itself raised RuntimeError: bootstrap.sh start-temporal failed (1): [31m[live] temporal did not become healthy within 60s — see /home/user/Chemclaw3/.live/temporal.log[0m |
| B | find_notes bodies ran | PASS | 366 audited call(s) |
| B | gather_evidence bodies ran | PASS | 151 audited call(s) |
| B | expand_note bodies ran | PASS | 145 audited call(s) |

**27/31 checks passed**, over the families that ran.
