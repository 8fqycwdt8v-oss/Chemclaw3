# Live durable-job smoke

Job `compute_reaction_energy` · workflow `calc-compute_reaction_energy-2a9ab04251e358d3` · launched in 1.3s
· Temporal `localhost:7233` · Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-08-28T17:13:20+00:00 |
| calculation cached in Postgres | PASS | 11 xtb* row(s) in calculation_results |
| job recorded in Postgres | PASS | calc/compute_reaction_energy by admin@localhost |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 15 → 15 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |

**5/5 checks passed.**
