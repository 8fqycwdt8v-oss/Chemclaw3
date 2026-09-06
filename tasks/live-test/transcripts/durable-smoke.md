# Live durable-job smoke

Job `compute_reaction_energy` · workflow `calc-compute_reaction_energy-c9b4fa9363d7b66c` · launched in 11.1s
· Temporal `localhost:7233` · Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-09-06T15:00:25+00:00 |
| calculation cached in Postgres | PASS | 3 xtb* row(s) in calculation_results |
| job recorded in Postgres | PASS | calc/compute_reaction_energy by admin@localhost |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 4 → 4 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |

**5/5 checks passed.**
