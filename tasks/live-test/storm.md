# Storm — mock-driven stress, chaos and adversarial pass

Front door `http://127.0.0.1:8000` · Temporal `localhost:7233` · 
Postgres `localhost:5432/chemclaw`

- **families planned / ran**: 1 / 1
- **mock requests served**: 808
- **ANTHROPIC_API_KEY set**: True
- **wall clock**: 603 s
- **disk free**: 21 GB

## Coverage

**1/1 planned families ran.**

| family | what it covers | checks |
| --- | --- | ---: |
| A | volume, and the admission cap swept end to end | 4 |

## A · admission cap swept (SCALE-3)

Offered load held at 24 concurrent, 48 turns per step; the front door restarted at each cap.

| cap | accepted | shed/error | p50 s | p95 s | answered/s | offered drained/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 10 | 38 | 6.0 | 6.7 | 0.71 | 3.41 |
| 4 | 17 | 31 | 7.0 | 9.1 | 1.02 | 2.87 |
| 8 | 24 | 24 | 8.1 | 11.8 | 1.14 | 2.36 |
| 16 | 37 | 11 | 13.4 | 20.9 | 1.27 | 1.62 |
| 32 | 48 | 0 | 18.7 | 31.9 | 1.30 | 1.30 |

The last column is not throughput — it counts a shed turn as a drained one, so refusing fast reads as going fast. `answered/s` is the measurement.

## Findings

| family | check | result | observed |
| --- | --- | --- | --- |
| A | every offered turn is accounted for at every cap | PASS | 5 cap(s) swept, 0 with unaccounted turns |
| A | the admission cap is load-bearing (goodput rises with it) | PASS | cap 2: 0.71 answered/s → cap 32: 1.30 answered/s |
| A | the sweep's own noise is small enough to read a knee against | **FAIL** | largest within-cap spread 28% over 3 sample(s) per cap |
| A | the sweep resolves the knee rather than running out of range | **FAIL** | no cap in (2, 4, 8, 16, 32) stops paying by more than the 28% noise floor — the sweep's top is a limit of the sweep, not of the system |

**2/4 checks passed**, over the families that ran.
