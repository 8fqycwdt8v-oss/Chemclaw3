# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

- **families planned / ran**: 1 / 1
- **mock requests served**: 20
- **ANTHROPIC_API_KEY set**: False
- **wall clock**: 10 s
- **disk free**: 20 GB

## Coverage

**1/1 planned families ran.**

| family | what it covers | checks |
| --- | --- | ---: |
| F | adversarial model output a real model will not produce on request | 9 |

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| F | an unparseable argument document is reported, not swallowed | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['find_notes'], result[0]=None |
| F | a truncated argument document is completed and run — the tool sees the cut value | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | LOAD-1's own shape is visible rather than counted as a call | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['find_notes'], result[0]=None |
| F | a tool the system does not have fails loudly | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=['tool_that_does_not_exist'], result[0]=None |
| F | an empty function name (STREAM-1) does not kill the turn silently | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[''], result[0]=None |
| F | a 100 KB argument document is survived, not refused | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | forty parallel calls in one turn are survived | PASS | HTTP 200, answered=True, error=None, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | a turn that writes nothing reports empty_answer | PASS | HTTP 200, answered=False, error=empty_answer, tools_failed=[], result[0]='matches=[] total_matches=0 widened=False' |
| F | an upstream model outage reaches the asker as an error | PASS | HTTP 200, answered=False, error=internal, tools_failed=[], result[0]=None |

**9/9 checks passed**, over the families that ran.
