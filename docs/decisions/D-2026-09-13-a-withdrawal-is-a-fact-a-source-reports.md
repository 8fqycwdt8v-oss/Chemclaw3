# D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports — a retraction rides the delta, and it is not real until every reader honours it

`D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry` built the storage half of a
retraction and then **removed it**, which is the reason this one could be built at all. What it left
behind is migration `066`'s `reaction_records.retracted_at` — kept, unread, with `068` restating its
comment to say so — and a measurement: with a producer wired and the readers absent, `is_current`
was `False`, `eligible()` was empty, and the withdrawn reaction **still came back** from the
ordinary `gather_evidence` sweep and from the agent's own `similar_reactions`. A tombstone three of
whose four readers ignore is not a control; it is a claim that one exists.

So this ADR starts from the readers, and adds the two halves that ADR's own post-mortem named as
the expensive parts: a producer that can actually be written, and a reader set that moves together.

## What was measured

Against `HEAD` before any change, on a real database:

- **`retracted_at` had no producer anywhere.** `RawEntry` carried no such field, so no adapter could
  set it and the column was unwritable by construction — the shape
  `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deleted three modules for.
- **A withdrawal is byte-identical prose.** A source that withdraws an entry re-exports it with a
  tombstone on it and nothing else changed, and `sync_entries` skipped an entry whose stored body
  matched. Driven end to end with the field added and that check untouched: the second sync booked
  the entry as `skipped_existing` and `records.retracted()` stayed empty. The producer reached the
  sync and stopped one line short of the row.
- **The live connector could not name the column.** `EntryBinding` has `created_at:` and
  `modified_at:` and nothing else that is a timestamp, so the warehouse ELN — the one connector with
  a real tenant — had no way to say where a site's withdrawal lives.
- **And it would not have been fetched if it could.** The cursor's watermark is
  `COALESCE(modified, created)`. A site that stamps a retraction column without touching its
  amendment column leaves the row behind the cursor for ever: the tombstone is written at the site
  and fetched by nobody.

## The decision

**A withdrawal is a fact the source reports, on the channel it already amends entries through.**

- **The producer is `RawEntry.retracted_at`**, an explicit field, *never* an entry's absence from an
  export. An ELN fetch is a delta, so "not seen this run" is the normal state of every entry ever
  ingested, and reading it as a withdrawal would retract the whole corpus on the first quiet pass.
  Riding the delta is also what makes `BACKLOG.md`'s (b) moot: that row required
  `durable/eln_sync.py::_BoundedIngest` to expose a public `inner` so a capability walk could find a
  `fetch_retractions`, and a *field* passes through that wrapper untouched. There is no capability
  probe, no second fetch and no corpus sweep.
- **It is part of the fetch window.** `entry_window` takes it as a third stamp, and a declared
  warehouse `retracted_at:` joins the SQL watermark as `GREATEST(W, COALESCE(retracted, W))` — the
  nesting because warehouses disagree about `GREATEST` over a NULL and a propagating one would move
  every un-retracted row's watermark to NULL and stop the source dead.
- **The row is what the source last said, in both directions.** The upsert refreshes `retracted_at`
  like every other column, so re-publishing an entry without a tombstone lifts the withdrawal. A
  `COALESCE(reaction_records.retracted_at, EXCLUDED.…)` would make one bad export permanent on a
  tier whose whole rule is that an amendment overwrites.
- **Five readers move with it, and the asymmetry between them is the design.**
  `ReactionRecord.is_current`, `eligible()`, the **unfiltered** retrieval sweep and
  `connectors/rxnfp` `similar_reactions` all drop a withdrawn run. `read()` and
  `agent.graph_tools.expand_note` keep serving it *and say so* — a row is the only readable form of
  an ELN run, so a campaign note that already cites a withdrawn one must expand into "this was
  withdrawn" rather than into "no note with that id", which is indistinguishable from a typo. The
  notice is system speech (`SYSTEM_SPEECH_MARK`) outside the framed source body, and `valid_to`
  carries the same fact in the structured half.
- **The unfiltered leg asks a different question from the filtered one.** `eligible()` drops a match
  whose record is missing — deliberately, because a record nobody can read cannot be shown to
  satisfy a narrowing — and an unfiltered sweep must still surface every structural hit the index
  holds. So `retracted()` is the *positive* form, over the page of candidate ids, answered by
  `066`'s partial index. An unindexed record is not a withdrawal.

`097` corrects `068`'s deployed comment, because a DBA reads `\d+ reaction_records` and it said
"Reserved and unread. Nothing writes this column".

## What this does not do

`ord_adapter` does not produce a retraction: ORD is a published bulk corpus with no withdrawal
concept, and inventing one would be exactly the inference this ADR refuses. A retraction is also
**not** a `valid_to` on the record: a result does not expire on its own, it is superseded, which is
a claim a human makes in a note. Different fact, different column — `066` argued that and it stands.

## What keeps it true

- `tests/test_eln.py::test_a_withdrawn_entry_leaves_the_evidence_set_on_every_reader` — the
  acceptance test, over real Postgres, driving the replay path a real withdrawal arrives on. It
  asserts the entry **was** served first, so its later absence is a difference rather than an
  emptiness.
- `tests/test_eln.py::test_a_source_that_republishes_an_entry_un_retracts_it` — the reversal, so
  nobody "fixes" the upsert into a one-way door.
- `tests/test_eln.py::test_a_json_export_stamped_withdrawn_is_fetched_and_carries_its_tombstone` —
  the file-drop producer, and the fetch window that makes it reachable.
- `tests/test_warehouse_adapter.py::test_a_site_that_withdraws_a_row_reaches_the_record_without_touching_its_amendment_column`
  — the live connector's producer, through the workflow's own chunk loop over `_BoundedIngest`.
- `tests/test_warehouse_adapter.py::test_a_declared_withdrawal_column_is_in_the_cursor_and_an_undeclared_one_is_not`
  — the emitted clause, in both directions, because the fake warehouse mirrors the watermark's
  semantics rather than parsing it.
- `tests/test_rxnfp_server.py::test_a_withdrawn_reaction_is_not_served_as_a_precedent` — the tool a
  chemist asks directly, which was the reader furthest from the record.
- `tests/test_reaction_records.py::test_expanding_a_withdrawn_record_resolves_and_says_it_was_withdrawn`
  — the reader that must *not* stop serving.
