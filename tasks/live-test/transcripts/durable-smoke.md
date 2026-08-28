# Live durable-job smoke

Job `compute_reaction_energy` · workflow `calc-compute_reaction_energy-22c1d328a9716af6` · launched in 1.3s
· Temporal `localhost:7233` · Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-08-28T21:27:59+00:00 |
| calculation cached in Postgres | PASS | 6 xtb* row(s) in calculation_results |
| job recorded in Postgres | PASS | calc/compute_reaction_energy by admin@localhost |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 6 → 6 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |

**5/5 checks passed.**
