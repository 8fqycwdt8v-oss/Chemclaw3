# Live durable-job smoke

Job `compute_reaction_energy` · workflow `calc-compute_reaction_energy-328543f545e5b10b` · launched in 0.5s
· Temporal `localhost:7233` · Postgres `localhost:5432/chemclaw`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-08-04T16:52:19+00:00 |
| calculation cached in Postgres | PASS | 76 xtb* row(s) in calculation_results |
| job recorded in Postgres | PASS | calc/compute_reaction_energy by service-account |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 109 → 109 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |
| audit chain verifies | PASS | OK: the audit trail hash chain is intact (over 139 audit event(s)) |

**6/6 checks passed.**
