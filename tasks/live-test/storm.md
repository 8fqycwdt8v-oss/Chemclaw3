# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `localhost:5432/chemclaw`

- **families planned / ran**: 8 / 8
- **mock requests served**: 390
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 814 s
- **disk free**: 19 GB

## Coverage

**8/8 planned families ran.**

| family | what it covers | checks |
| --- | --- | ---: |
| A | volume, and the admission cap swept end to end | 2 |
| B | tool bodies really ran, asked of the audit trail | 2 |
| C | the same call whole, fragmented, and in parallel | 3 |
| D | identical durable launches colliding | 2 |
| E | chaos — disconnects, killed workers, a bounced database, a dead broker | 4 |
| F | adversarial model output a real model will not produce on request | 8 |
| G | the front door's own limits, asked for deliberately | 2 |
| H | pathological data: bad chemistry, impossible arguments, unicode, injection | 4 |

## A · admission cap swept (SCALE-3)

Offered load held at 48 concurrent, 48 turns per step; the front door restarted at each cap.

| cap | accepted | shed/error | p50 s | p95 s | turns/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 6 | 42 | 5.9 | 6.1 | 6.65 |
| 4 | 8 | 40 | 6.6 | 7.1 | 6.46 |
| 8 | 15 | 33 | 8.2 | 10.4 | 4.59 |
| 16 | 22 | 26 | 11.6 | 13.3 | 3.59 |
| 32 | 32 | 16 | 19.2 | 19.5 | 2.45 |

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| C | c-whole: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-fragmented: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-parallel: announcements match results (6 expected) | PASS | announced/returned per turn: ['6/6', '6/6', '6/6'] |
| D | 12 simultaneous identical launches share one run | PASS | 0 distinct workflow id(s) announced across 12 turns |
| D | the collision computed at most one result set | PASS | calculation_results 113 → 113; job_records 14 → 14 |
| F | a truncated argument document is reported, not swallowed | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='Error: Argument parsing failed.' |
| F | LOAD-1's own shape is visible rather than counted as a call | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='Error: Argument parsing failed.' |
| F | a tool the system does not have fails loudly | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='Error: Requested function "tool_that_does_not_exist" not found.' |
| F | an empty function name (STREAM-1) does not kill the turn silently | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]=None |
| F | a 100 KB argument document is survived, not refused | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='[]' |
| F | forty parallel calls in one turn are survived | PASS | HTTP 200, answered=True, error=None, tools_failed=[], result[0]='[]' |
| F | a turn that writes nothing reports empty_answer | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='[]' |
| F | an upstream model outage reaches the asker as an error | PASS | HTTP 200, answered=False, error=internal, tools_failed=[], result[0]=None |
| G | a message over 100000 chars is refused | PASS | HTTP 422 |
| G | the per-user event-stream cap refuses with 429 | PASS | codes [200, 200, 200, 200, 200, 429, 429, 429] |
| H | unicode survives the round trip through Postgres | PASS | 1 session_messages row(s) hold the exact string; answered=True |
| H | an injection string is treated as a search string | PASS | audit_events 1793 → 1794 (a dropped table reads as 0) |
| H | an unparseable reaction SMILES does not kill the turn | PASS | HTTP 200, answered=False, error=empty_answer, result[0]='[]' |
| H | arguments that parse and cannot be true are refused, not answered | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['compute_reaction_energy'], result[0]="Error: the 'compute_reaction_energy' job ran and failed: ValueError: s" |
| A | every offered turn is accounted for at every cap | PASS | 5 cap(s) swept, 0 with unaccounted turns |
| A | the admission cap is load-bearing (throughput rises with it) | **FAIL** | cap 2: 6.65 turns/s → cap 32: 2.45 turns/s |
| E | a disconnected session accepts a new turn without waiting out the lease | PASS | accepted after 0.2s (lease is 60.0s); status codes [409, 200] |
| E | a job survives its connector worker being SIGKILLed mid-flight | PASS | at kill: RUNNING; after restart: COMPLETED 583s later (heartbeat timeout is 600s); job_records rows: 1 |
| E | the front door recovers from a Postgres restart without being restarted itself | PASS | 22/24 in-flight turns survived the bounce; a fresh turn answered 1.7s after it |
| E | a durable launch with no broker reaches the asker as an error, not as an answer | PASS | HTTP 200, answered=True, error=None, tools_failed=['compute_reaction_energy'], result[0]="Error: the 'compute_reaction_energy' job could not be confirmed as sta" |
| B | find_notes bodies ran | PASS | 1669 audited call(s) |
| B | gather_evidence bodies ran | PASS | 8 audited call(s) |

**26/27 checks passed**, over the families that ran.
