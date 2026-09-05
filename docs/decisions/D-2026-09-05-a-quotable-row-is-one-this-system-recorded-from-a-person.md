# D-2026-09-05-a-quotable-row-is-one-this-system-recorded-from-a-person — a shape rule died on `make db-migrate`

## Status

Accepted. Revisits the `session_messages` half of
`D-2026-09-04-a-quote-is-evidence-about-a-person-not-about-a-turn`, which is not edited. Its
central security property — that model prose, a tool result and a mid-turn job push-back cannot
reach the `stated` haystack — was re-driven end to end on a compiled graph and holds on both
writers.

## Context

`require_quotes_are_verbatim` exists so that a `basis="stated"` slot in a structured experiment
request is evidence about **a person**. The haystack it checks against is built by
`agent/session_store`, which read the rows a session recorded and excluded any whose
`message_shape` was not `langchain`, with a comment saying so in the present tense:

> an unstamped row is MAF … so what carried the `user` role there is not the set this system can now
> say a person typed. Excluding them is the conservative half of a rule about evidence.

That is a rule about a **shape**, and the shape is not stable. `message_migration.convert_stored_messages`
— which `make db-migrate` runs, and which the Helm chart's post-upgrade Job runs — rewrites every
Microsoft-Agent-Framework row into `message_to_dict(HumanMessage(...))` and stamps it `langchain`.

Measured against a live Postgres, seeding one MAF `role: user` row carrying tool-shaped text:

```
before conversion:  ('maf', 'message', message_original IS NOT NULL = False)   quotable: []
conversion:         ConversionOutcome(converted=1, refused=())
after  conversion:  ('langchain', 'human', message_original IS NOT NULL = True)
                    quotable: ['{"tool": "screen_hazards", …}']
```

So the exclusion held only on a database that had **not** run the migration the release ships with.
The earlier ADR's supporting claim — that `session_messages` has exactly one human-row producer,
`_record_transcript` — is false on the same grounds: the conversion is a second producer of
`type: human` / `shape: langchain` rows.

`test_an_unstamped_legacy_row_is_not_offered_as_the_chemists_own_words` pinned precisely the state
in which the rule holds and never varied the axis that breaks it.

## Decision

**The rule is about provenance, not shape.** A quotable row is one this system recorded *from a
person* — LangChain-shaped **and** never rewritten from another engine's bytes. The read adds
`AND message_original IS NULL`, the column the conversion's own single statement writes:

```sql
SET message_original = message, message = %s, message_shape = 'langchain'
WHERE id = %s AND message_shape = 'maf'
```

One statement, so the column marks every converted row and nothing else; the insert path never
writes it, so no genuine row is falsely excluded. It is `IS NULL` on a nullable column — null-bitmap
only, no detoast, and the planner has statistics for it.

The rendering promise is untouched: `get_messages` still returns converted rows, because a chemist
reading their own history and a guard deciding what may be quoted are different questions about the
same table.

## Consequences

**The residual is stated rather than discovered.** The exclusion now rides on the rollback column,
so an operator who runs migration 067's documented `SET message_original = NULL` gives up this
exclusion with it. The alternative — a third `message_shape` value — is a migration and a CHECK
constraint, and is not taken here.

The new test runs the **real** conversion and asserts the row actually moved before asserting it is
still unquotable, so it cannot pass on a conversion that never happened. Mutating the predicate away
turns it red while both pre-existing tests stay green, which is the finding demonstrated rather than
asserted.

**A second sentence in the same file was backwards and is corrected.** It implied the tool-result
JSONB is not detoasted by this read. Measured with `BUFFERS` on a replica carrying the real indexes,
`message->>'type'` fully detoasts the datum, so every *same-session* row the scan passes is
detoasted — 4 buffers against 324 for twenty 40 kB incompressible payloads. What the `session_id`
predicate buys is that **other** sessions' rows are never detoasted, because it is the cheap qual
and is evaluated first.

**One risk is filed rather than fixed**, because the fix is a decision nobody has the production
evidence for: Postgres has no statistics for the expression `message->>'type'`, so in a table with
many sessions the planner walks the primary key backwards instead of the session index — measured,
119,740 rows filtered to return 20, on every turn, growing with the whole table rather than the
session. `CREATE STATISTICS`, an expression index, and hoisting the type test out of SQL are three
different bets, and an index added to force a plan is a cost every write pays forever.
`docs/planning/BACKLOG.md` carries it with the row counts rather than the milliseconds, because two
measurements agreed on the counts and disagreed 50x on the clock.

A fourth exclusion for `HumanMessageChunk` was considered and **declined**: it is a `HumanMessage`
subclass and would pass, but no producer writes one and none can — the durable path rebuilds rows
through `messages_from_dict`, which mints `HumanMessage` and cannot mint a chunk. A control that
cannot fire is the `reject_widening` shape this repository has already deleted once.
