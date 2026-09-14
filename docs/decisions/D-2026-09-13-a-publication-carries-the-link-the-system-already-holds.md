# D-2026-09-13-a-publication-carries-the-link-the-system-already-holds — the note is carried, the reaction is not recorded anywhere to carry

A queue row read: *a published calculation names no reaction, note or compound context*, and asked
for an ADR. Driving it found the claim is three claims with three different answers, and only one of
them is a gap this repository can close today.

## What was measured

**The compound context exists.** `subject_member.compound_id` is `core.chem.compound_id` — the same
`compound-<hash>` string the knowledge graph uses as a compound note id, the fingerprint search
cites and the QM notes carry. `publish/project.py::_identify` computes it and both that function and
`publish/record.py` say why in their docstrings. A published result already meets the note about the
same compound with no second naming scheme.

**The calculation ↔ note link exists in one direction.** `calculation.calc_ref` is the flat cache
key, and a knowledge note's `calc_refs` cites that exact string — `kg/validate.py::calc_citations`
walks it. Starting from a note, the calculation is named.

**The note the run produced was held and dropped.** `job_records.note_id` is written by
`durable/connector_job.py::finished_job_record` from the `ConnectorJobResult` a finished connector
job returns. Both publish paths had that value in hand and carried neither:
`ConnectorJobWorkflow._publish_result` is handed the same `result` object, and `backfill_jobs` reads
the very row the column sits in. So `calculation_publication` recorded the session, the job, the
actor, the correlation id and the rationale — four weak links to a run — and dropped the one
structured link to what the run produced.

**The reaction context is not dropped; it is never recorded.** No launcher argument, no
`job_records` column and no `calculation_results` row names the ELN entry that motivated a
calculation. The link does not exist anywhere in this system to be carried.

**And one test was measuring less than its docstring claimed.** `tests/test_publish_sql.py` says it
drives "a Postgres running the *shipped* DDL (`schema/result-store/`)" and applied `001_core.sql`
alone — so every migration after the first was outside the one lane that asks real SQL questions of
a real store. It applies the directory now.

## The decision

**A publication carries the link the system already holds, and does not mint one it cannot fill.**

- `Publication.note_id` and `calculation_publication.note_id` (`schema/result-store/003`), written by
  **both** producers, with a partial index for the direction a chemist reads it in: they have the
  note, they want the numbers.
- **No reaction id column.** A column for which ELN run motivated a calculation would be one nobody
  could fill — the shape `D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports` and
  `D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-instability` both refused inside a
  week, and the one this queue row's own warning is about. Recording it means the agent stating the
  motivating run when it launches a calculation, which is a new argument on a tool, a new column on
  `job_records` and a decision about whether a model-supplied id may be stored as provenance. That
  is a design question with no reader: `CHEMCLAW_RESULT_SINKS` names nothing in any shipped
  deployment. `DEFERRED.md` holds it with that trigger.

**Why the note field is not the dead column the reaction field would be.** It carries a fact that
already exists, into the table whose stated purpose is carrying exactly this kind of fact, beside
four fields in the same state of being written and not yet read. The reaction field would create
the fact as well as the column, and creating a fact to fill a column nobody reads is where the two
cases separate.

## What keeps it true

- `tests/test_publish_end_to_end.py::test_a_finished_job_publishes_the_note_it_produced` — the live
  producer, through the real `ConnectorJobWorkflow` on a real broker. A test that built a
  `Publication` itself would assert that a field it filled arrives, which is true of a field nothing
  fills.
- `tests/test_publish_backfill.py::test_the_jobs_walk_carries_the_note_the_run_produced` — the other
  producer, which is what re-publishes a deployment's whole history the day a sink is turned on. Two
  rows, one with a note and one without, so the assertion is a difference.
- `tests/test_publish_end_to_end.py::test_a_composite_reaches_an_external_database_and_answers_a_question`
  — the column, read back out of the shipped DDL in SQL.
