# M12 · durable-launcher ordering

REV-6's claim, checked as an *ordering* rather than as a count: the outage has to be announced before the first token, or the model plans against a surface it will not get. Run this with the durable broker deliberately stopped.

- front door `http://127.0.0.1:8000`
- transcripts in `tasks/live-test/transcripts/degradation`

| probe | check | result | observed |
| --- | --- | --- | --- |
| dg-01 | the outage was announced | PASS | capability_degraded named ['durable-jobs (Temporal)'] |
| dg-01 | announced before the first token | PASS | degraded at event 1, first token/answer at 2 |
| dg-01 | the durable launcher was reached | PASS | called ['compute_reaction_energy']; expected any of ['compute_reaction_energy'] |

**3/3 checks passed.**
