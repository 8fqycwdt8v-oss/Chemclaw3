# Live durable-job smoke

Job `compute_reaction_energy` · workflow `calc-compute_reaction_energy-88ed85d025f6cb6d` · launched in 0.6s
· Temporal `localhost:7233` · Postgres `localhost:5432/chemclaw`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-08-04T06:24:22+00:00 |
| calculation cached in Postgres | PASS | 6 xtb* row(s) in calculation_results |
| job recorded in Postgres | PASS | calc/compute_reaction_energy by service-account |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 12 → 12 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |
| audit chain verifies | PASS | OK: the audit trail hash chain is intact |

**6/6 checks passed.**
