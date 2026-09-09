# D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated — three holes in the migration guard, two closable by a pattern and one not

**Status:** accepted · **Date:** 2026-09-09 · **Revisits:**
D-2026-08-04-the-schema-only-goes-forward (the additive policy and the guard that carries it),
D-2026-08-08-a-rollback-that-is-not-a-schema-step (the two buckets) ·
**Corrects one reading in:** D-2026-09-07-a-claim-that-outlives-its-transaction-is-a-lease
(the lease decision stands; what it said about the previous image does not)

## Context

`tests/test_migrations_are_additive.py` asks three questions of every migration: does it destroy
data, does it leave the previous image able to write, and is it re-runnable. Each is asked by a
regular expression over statement starts, and each expression enumerates the statements its author
had in front of them. Three of the four findings below are what enumeration costs; the fourth is
what no enumeration reaches.

### 1. Re-runnability was asked of `CREATE` and answered for `CREATE`

The re-runnability check scans `CREATE [UNIQUE] TABLE|INDEX|SCHEMA|TYPE|VIEW` for `IF NOT EXISTS`.
`ALTER TABLE … ADD CONSTRAINT` is outside that pattern, and Postgres has **no** `IF NOT EXISTS`
spelling for a constraint at all — so the only re-runnable way to write one is to drop it first,
`IF EXISTS`. The four `ADD PRIMARY KEY` statements in the tree (041, 056, 063, 088) each sit behind
a `DROP CONSTRAINT IF EXISTS`. `046_review_hardening_indexes.sql` does not.

Measured on a scratch database carrying all 91 migrations, with `schema_migrations` emptied — the
logical restore whose recovery the check's own docstring describes:

```
psycopg.errors.DuplicateObject: constraint "session_messages_shape_known"
    for relation "session_messages" already exists
```

The runner sends the whole run as one transaction, so nothing after file 46 of 91 applies either.
The docstring it walks past says what that costs: *"Without it the recovery is 'work out which
statements already ran', by hand, under pressure."*

Replaying every file individually against the same database found **exactly one** other:
`058_note_proposal_superseded.sql` drops its `CHECK` without `IF EXISTS`, which replays fine
against a restore (the constraint is there) and fails on the *other* arm the same docstring
names — an operator re-pointing the runner at a database built by hand:

```
psycopg.errors.UndefinedObject: constraint "note_proposals_state_known"
    of relation "note_proposals" does not exist
```

### 2. The additive guard could not see a type change, in either bucket

A hypothetical `092` doing `ALTER TABLE turn_costs ALTER COLUMN duration_seconds TYPE REAL` matched
neither `_DESTROYS_DATA` nor `_BREAKS_PREVIOUS_IMAGE` — measured, both patterns return zero matches
for it. The loss is real and irreversible; on Postgres 16:

```
before        0.3333333333333333
after  REAL   0.33333334
widened back  0.3333333432674408
```

Both comment blocks enumerate what they cover and what they deliberately omit, and a type change is
in neither list. That is an omission rather than a decision — the tree's one type change (`091`) was
written after both.

### 3. The guard asks whether the previous image can *write*, never whether it honours the new column

`089_result_publication_lease.sql` adds one nullable column with no default. It is additive by every
reading in this repository, it passes both patterns, and its own ADR says approvingly that "the
previous image keeps writing the table."

It does. **Without the lease.** Driven against the migrated schema with the pre-089 `_CLAIM`
verbatim — no lease predicate, no `claimed_at` write — after a new-pod drain has claimed a row:

```
-- new pod holds the lease; delivery is in flight
 id |  state  | attempts | leased
  1 | pending |        1 | t
-- previous image claims the same row
 id |  state  | attempts | leased
  1 | pending |        2 | t
```

That is the double-delivery symptom 089 was written to close, reappearing during exactly the window
a rollback creates. The sink converges (every key in `schema/result-store/` is content-addressed),
so what is lost is the *attempt budget*: eight attempts empty after four real ones, and the row then
dead-ends `pending`, where — as 089's own comment sets out — no remedy matches it.

### 4. The README's rollback list was transcribed rather than derived

`infra/sql/README.md` said **four** reviewed rollback-breaking migrations and listed 041, 056, 058,
063. The set held five; the omitted one was **088**, the newest and the only one bearing on a
rollback of the current release. The same paragraph claimed the list was "derived from that set,
which is the only place they are maintained". An operator reading it before a `helm rollback` was
told the `turn_costs` primary-key move is not a rollback break. It is.

## Decision

**Re-runnability is a property of the file, not of its `CREATE`s.** A migration that adds a
constraint must first drop it `IF EXISTS`, on the same table and by the same name, *earlier in the
file* — co-occurrence is not the property, because a drop below the add replays no better than no
drop at all. 046 and 058 are merged and their statements are immutable, so they are carried in
`_REVIEWED_REPLAY_BREAKS` with the one statement an operator runs before a replay:

| Migration | Recipe |
| --- | --- |
| `046_review_hardening_indexes.sql` | `ALTER TABLE session_messages DROP CONSTRAINT IF EXISTS session_messages_shape_known;` |
| `058_note_proposal_superseded.sql` | `ALTER TABLE note_proposals DROP CONSTRAINT IF EXISTS note_proposals_state_known; ALTER TABLE note_proposals ADD CONSTRAINT note_proposals_state_known CHECK (state IN ('open', 'merged', 'rejected', 'failed', 'superseded'));` |

Both were verified end to end rather than reasoned about: with each applied, all 90 tracked files
replay clean against a database that already carries the whole schema. 046's constraint is
`NOT VALID`, so re-adding it costs no table scan. 058's recipe puts the constraint back so the file's bare
drop finds one, in the *post*-058 form — identical to what the file itself re-adds — because the
pre-058 form would reject any row already holding `superseded`. Both are unconditional and
idempotent, so an operator need not first work out which arm the database is in.

**`ALTER COLUMN … TYPE` joins `_BREAKS_PREVIOUS_IMAGE`, not `_DESTROYS_DATA`, and the reason is that
one of the two buckets has an exemption path.** A narrowing conversion destroys data outright; a
widening destroys nothing, and the tree has exactly one (`091_reaction_label_confidence_precision.sql`, `REAL` → `DOUBLE
PRECISION`, argued in the file). No pattern can tell them apart without tracking every column's current type across ninety
files, which is a type checker for SQL. Put in `_DESTROYS_DATA` — which refuses with no exemption at
all, deliberately, because rollback cannot bring rows back — the rule would refuse 091 with no way
to say yes, and the fix would be to weaken the one absolute rule in the file. So the statement goes
where a judgement can be recorded, and **the refusal moves into the failure message and into this
paragraph: a narrowing may not be exempted there.** 091 is exempted with its reading written down,
in the shape 058 already established — a row that exists because the guard matches the text and not
the semantics.

**A column that carries a coordination meaning is a reviewed break, and it is judged rather than
matched.** `_REVIEWED_SEMANTIC_BREAKS` is a second, separate register for migrations that end the
previous-image rollback for a reason no statement shape carries; 089 is its first member. It is
asserted disjoint from `_REVIEWED_ROLLBACK_BREAKS` and its members are asserted to be genuinely
unflagged by the pattern, so a later widening that reaches one moves the row rather than leaving it
reviewed twice under two procedures.

**It is deliberately not a pattern, and this is the part to read twice.** "Does this new column mean
something the previous image must honour?" is a question about the code on both sides of the
deploy, not about the SQL — `ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ` is the same eleven
tokens whether the column is a lease or a comment. A regex claiming to answer it would be the shape
this repository names elsewhere as a control that is really a claim, and it would be worse than
nothing, because the next author would trust it. A declaration required on the migration (`--
previous-image: safe`) was considered and rejected for the neighbouring reason: comments are
stripped from the checksum, so the marker is unverifiable prose the test would simply believe, and
retrofitting it across ninety merged files would buy ceremony rather than thought. What the register
buys is that a judgement, once made, is written where an operator planning a rollback reads it —
beside the ones a regex found.

**The rollback list is checked rather than transcribed.** `infra/sql/README.md` names every
migration in both registers, with no count beside it — a count is derivable from the list and is
exactly what went stale — and `tests/test_schema_inventory.py` checks the two against each other in
both directions, the same shape it already uses on the **Migration** column.

## Consequences

**One reading in a merged ADR is corrected without editing it.**
`D-2026-09-07-a-claim-that-outlives-its-transaction-is-a-lease` decided the lease and decided it
correctly; the sentence in `089_result_publication_lease.sql` about the previous image is what the
measurement above contradicts. 089's row in the new register points here, so the operator procedure
is findable from the migration.

**What an operator does instead of "deploy the previous image", for 089.** Quiesce publishing for
the duration of the rollback — no drain running while two images coexist is the whole of it, because
the lease is only contended while a delivery is in flight. Afterwards, a row whose attempts were
double-spent is returned to service with
`UPDATE result_publications SET attempts = 0, claimed_at = NULL WHERE state = 'pending';`, which
also resurrects any genuinely exhausted row — correct after a rollback that spent their attempts
twice, and worth stating rather than leaving to be discovered.

**The guard now says what it does not cover.** Its two comment blocks enumerate; this ADR is the
record that enumeration is the risk, and the register above is the record of the one hole that stays
open by design.

## What keeps it true

* `test_a_re_added_constraint_is_dropped_first` — per migration, with the two reviewed recipes.
* `test_the_replay_rule_reads_the_object_not_the_spelling` — the rule against synthetic SQL, because
  the tree's only two examples are both exempt and a rule validated on its own examples fits them.
* `test_the_two_patterns_say_what_they_mean` — the type change in both directions, and the four
  spellings of a table name.
* `test_a_migration_leaves_the_previous_image_able_to_write` — the exemption for
  `091_reaction_label_confidence_precision.sql`, exact.
* `test_a_judged_break_is_one_no_pattern_could_have_found` — the two registers stay disjoint and a
  judged break stays unmatched.
* `test_no_exemption_outlives_its_migration` — all three registers name files that exist.
* `test_the_rollback_note_names_every_reviewed_break` — the README against both registers, both ways.
