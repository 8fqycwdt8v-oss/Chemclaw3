# Live durable-job smoke

Job `compute_reaction_energy` · workflow `calc-compute_reaction_energy-f229f7950e684de2` · launched in 1.9s
· Temporal `localhost:7233` · Postgres `user=chemclaw dbname=chemclaw host=localhost port=5432`

| check | result | observed |
| --- | --- | --- |
| workflow reached COMPLETED | PASS | COMPLETED, started 2026-08-17T18:27:37+00:00 |
| calculation cached in Postgres | PASS | 17 xtb* row(s) in calculation_results |
| job recorded in Postgres | PASS | calc/compute_reaction_energy by service-account |
| duplicate launch rejoins the same run | PASS | id matches; cache rows 25 → 25 |
| wedged worker yields a pending job | PASS | returned the id after 20s, then COMPLETED once resumed |

**5/5 checks passed.**
