# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `localhost:5432/chemclaw`

- **mock requests served**: 517
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 46 s
- **disk free**: 19 GB

## A · admission sweep

| offered | accepted | shed/error | p50 s | p95 s | turns/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 16 | 0 | 2.4 | 3.8 | 1.47 |
| 16 | 16 | 0 | 7.6 | 9.3 | 1.71 |
| 48 | 16 | 0 | 7.6 | 9.1 | 1.75 |

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| C | c-whole: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-fragmented: announcements match results (1 expected) | PASS | announced/returned per turn: ['1/1', '1/1', '1/1'] |
| C | c-parallel: announcements match results (6 expected) | PASS | announced/returned per turn: ['6/6', '6/6', '6/6'] |
| D | 8 simultaneous identical launches share one run | PASS | 0 distinct workflow id(s) announced across 8 turns |
| D | the collision computed at most one result set | PASS | calculation_results 109 → 109; job_records 13 → 13 |
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
| B | find_notes bodies ran | PASS | 707 audited call(s) |
| B | gather_evidence bodies ran | PASS | 6 audited call(s) |

**17/17 checks passed.**
