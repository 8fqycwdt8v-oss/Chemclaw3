# Live durable-job smoke

Job `compute_reaction_energy` · workflow `calc-compute_reaction_energy-f628843f52198ce9` · launched in 0.2s
· Temporal `localhost:7233` · Postgres `localhost:5432/chemclaw`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-08-12T12:42:40+00:00 |
| calculation cached in Postgres | PASS | 3 xtb* row(s) in calculation_results |
| job recorded in Postgres | PASS | calc/compute_reaction_energy by service-account |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 6 → 6 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |
| audit chain verifies | PASS | OK: the audit trail hash chain is intact (over 0 audit event(s)) |

**6/6 checks passed.**
