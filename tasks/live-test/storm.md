# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `localhost:5432/chemclaw`

- **families planned / ran**: 8 / 8
- **mock requests served**: 270
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 227 s
- **disk free**: 19 GB

## Coverage

**8/8 planned families ran.**

| family | what it covers | checks |
| --- | --- | ---: |
| A | volume, and the admission cap swept end to end | 3 |
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
| 2 | 6 | 42 | 6.2 | 6.2 | 0.79 | 6.28 |
| 4 | 9 | 39 | 6.8 | 7.0 | 1.08 | 5.78 |
| 8 | 16 | 32 | 8.4 | 10.7 | 1.48 | 4.44 |
| 16 | 22 | 26 | 11.2 | 13.0 | 1.68 | 3.67 |
| 32 | 33 | 15 | 16.6 | 16.9 | 1.87 | 2.72 |

The last column is not throughput — it counts a shed turn as a drained one, so refusing fast reads as going fast. `answered/s` is the measurement.

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| C | c-whole: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-fragmented: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-parallel: announcements match results (6 expected) | PASS | announced/returned per turn: ['6/6', '6/6', '6/6'] |
| D | a completed job is findable through find_past_jobs afterwards | PASS | 1 tool result(s); result[0]='[{"job_id": "calc-compute_reaction_energy-049b59f9ee290636", "connecto' |
| D | 12 simultaneous identical launches produce exactly one run | PASS | 1 job_records row(s) written across 12 simultaneous turns |
| D | the collision computed at most one result set | PASS | calculation_results 157 → 157 (one cold run writes ~3-6 rows) |
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
| H | an injection string is treated as a search string | PASS | audit_events 92 → 93 (a dropped table reads as 0) |
| H | an unparseable reaction SMILES does not kill the turn | PASS | HTTP 200, answered=False, error=empty_answer, result[0]='[]' |
| H | arguments that parse and cannot be true are refused, not answered | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['compute_reaction_energy'], result[0]="Error: the 'compute_reaction_energy' job ran and failed: ValueError: s" |
| A | every offered turn is accounted for at every cap | PASS | 5 cap(s) swept, 0 with unaccounted turns |
| A | the admission cap is load-bearing (goodput rises with it) | PASS | cap 2: 0.79 answered/s → cap 32: 1.87 answered/s |
| A | the sweep reached the knee rather than running out of range | **FAIL** | still improving at cap 32 — the sweep's top is a limit of the sweep, not of the system |
| E | a disconnected session accepts a new turn without waiting out the lease | PASS | accepted after 0.2s (lease is 60.0s); status codes [409, 200] |
| E | a job survives its connector worker being SIGKILLed mid-flight | PASS | at kill: RUNNING; after restart: COMPLETED 0s later (heartbeat timeout is 600s); job_records rows: 1 |
| E | the front door recovers from a Postgres restart without being restarted itself | PASS | 20/24 in-flight turns survived the bounce; a fresh turn answered 1.7s after it |
| E | a durable launch with no broker reaches the asker as an error, not as an answer | PASS | HTTP 200, answered=True, error=None, tools_failed=['compute_reaction_energy'], result[0]="Error: the 'compute_reaction_energy' job could not be confirmed as sta" |
| B | find_notes bodies ran | PASS | 177 audited call(s) |
| B | gather_evidence bodies ran | PASS | 3 audited call(s) |
| B | expand_note bodies ran | PASS | 2 audited call(s) |

**29/30 checks passed**, over the families that ran.
