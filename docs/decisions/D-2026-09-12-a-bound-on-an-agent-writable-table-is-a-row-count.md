# D-2026-09-12-a-bound-on-an-agent-writable-table-is-a-row-count — A bound on an agent-writable table is a row count

**Status:** accepted · **Date:** 2026-09-12 ·
**Extends:** `D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask` (the
`ingest_rejections` shape this copies) ·
**Closes:** the `store` row of `docs/planning/BACKLOG.md`, and the six "nothing bounds it" entries in
`durable/retention.py::_NOT_PRUNED`

## Context

`durable/retention.py`'s disposal register is exhaustive by construction: every table is in exactly
one tier, and "**nothing bounds it**" is a recognised entry, because recording a finding is better
than inventing an answer. Six tables carried it. They are **five different decisions**, and reading
them as one was the mistake worth avoiding.

Two of the six are agent-writable, and those are the bug:

- **`store`** (`agent/scratchpad.py`) — a turn's durable memories. No size cap, no window, no clock,
  and the retention sweep deliberately does not touch it. Driven, 2,000 writes of 5 kB under one
  namespace left `(2000, '816 kB')` with nothing evicted.
- **`user_preferences`** (`agent/preferences.py`) — the register said "one row per person per key,
  and a preference has no age at which it stops being current". The second clause is right; the
  first is not a bound, because `remember_preference(key, value)` takes a **model-chosen** key. And
  the `SELECT … ORDER BY key` behind `recall_preferences` had **no `LIMIT`**, so every preference a
  chemist had ever set re-entered the prompt on every recall, in every later session, for the life
  of the row — behind a tool the model is instructed to call "early in a substantive answer".

**The runaway is not a looping turn**, and that matters because it is where the obvious mitigation
already exists. `harness_max_loop_iterations` (25) × `agent_max_parallel_tool_calls` (8) is a hard
ceiling of ≤200 store writes per turn. What nothing bounded is accumulation *across* turns, over a
deployment's life, because nothing ever removed a row.

## Decision

### The two agent-writable tables get a row cap, per actor

`agent_memory_max_files` (200) and `preferences_max_per_owner` (200). Past the cap the least
**recently updated** row is evicted, and each eviction is counted
(`chemclaw_memory_evictions_total`, `chemclaw_preference_evictions_total`) and logged at WARNING
naming what went — the way `ingest/rejections.py` logs its own, because an eviction is a chemist
losing something they were told was remembered.

**A row count, not a clock and not a byte budget.** A memory is written in order to persist, so age
is the wrong axis: the oldest is as likely to be the one worth keeping. Bytes are wrong too, because
"you hold fewer memories now because one of them was long" is not a rule anybody can keep in their
head. `updated_at` is therefore a *tiebreak* — the bound is the count, and when it binds something
has to go, and the store carries exactly one ordering that is not arbitrary.

**The recall limit is a second, separate bound** (`preferences_recall_limit`, 50), because it bounds
a different resource: the row cap is storage, the recall cap is prompt spend, and a deployment that
lowers the row cap still holds the rows it already wrote. Selected by recency and presented by key —
a truncation by key alone would drop what the chemist said five minutes ago in favour of a year-old
preference beginning with "a".

### Where each cap is enforced, and what each can promise

`user_preferences` evicts in the writer's own transaction (`DELETE … WHERE key NOT IN (… ORDER BY
updated_at DESC LIMIT n)`), so its invariant is exact.

`store` cannot. `BoundedStoreBackend.awrite` writes through upstream and then evicts, and the memory
store shares the checkpointer's **autocommit** pool — for three measured reasons `agent/checkpointer.py`
records, none of which this may undo — so the write and the eviction are two statements. The
invariant is therefore **eventual**: "at most the cap, plus whatever is in flight". Two turns writing
one namespace at the same instant can both evict one or neither can; the next write converges.
Saying so is the point. `ingest_rejections` can promise atomicity because its writer owns a
transaction, and claiming the same here would claim a property the pool cannot give.

### The exemption to the "no first-party `aput`/`adelete`" rule is narrowed, not deleted

`tests/test_scratchpad.py` asserts that no first-party module calls `aput`/`adelete` on a store,
because every memory write has to arrive as a `write_file`/`edit_file` *tool* call — that is what
crosses the `wrap_tool_call` chain and produces the audit row, the authorization decision and the
dry-run refusal. `agent/scratchpad.py` is now the one allowed file, named in that test. It does not
weaken the property: an eviction *removes* what the cap says may not stay, inside the same call the
tool made, so nothing enters the store outside the chain. One write path and a test that says which
is the `kg/record.py` idiom, and a second module acquiring the verb turns it red.

### The other four are recorded, and two of them were not what they looked like

- **`molecule_fingerprints` / `reaction_fingerprints` — bounded by construction, and *not* bounded by
  the corpus.** The write is an upsert on a structural key, so traffic cannot grow the table. But
  `094` deliberately put the fingerprint *definition* in that key — two generations of one
  standardization must not evict each other mid-rebuild — so the real bound is the corpus multiplied
  by every definition ever written, and **nothing reclaims the superseded generation**:
  `app_privileges.sql` grants these tables INSERT and UPDATE only, which is what makes this
  register's refusals enforced rather than intended. A `STANDARDIZATION_VERSION` bump is a permanent
  doubling — measured from the other side by
  `D-2026-09-09-a-rebuild-nothing-counts-reports-as-finished`. That is a decision about *bumps*, not
  a row cap, so none is proposed.
- **`predictions` — unbounded, accepted.** Keyed `(calc_type, calc_version, input_hash)`, so
  `calc_version` is *in* the key and a version bump forks the table for the same reason the
  fingerprints fork: a calibration compares versions, so the old rows are the comparison. Pruning one
  changes a calibration rather than reclaiming a cache. The bound that would be right is a
  `calc_version` retirement policy, which is a scientific decision and is not taken here.
- **`measurements` — unbounded, accepted.** Human-paced: one row per measurement somebody actually
  made.

## Consequences

- A chemist with more than 200 memories or 200 preferences loses the least recently touched. That is
  a real behavioural change, and it is why both are counted and logged rather than silent.
- `recall_preferences` no longer returns everything. Its docstring and the tool's `Returns:` say so,
  because a model told "every preference this chemist has set" over a truncated list would state it.
- The in-memory preference fallback holds the same two bounds. It is the *configured* store in
  memory mode, so a cap that existed only where a database did would be a cap that deployment does
  not have.
- `test_no_disposal_entry_offers_actor_erasure_as_what_bounds_a_table` gains a fourth accepted form:
  an entry may **name the knob**. Not the word "bounded" — "bounded by actor erasure" is the defect
  that test exists for and contains that word — but a `Settings` field name that exists, so a
  renamed setting turns it red rather than leaving a register sentence pointing at nothing. Mutated
  with `"bounded by its writer: a leaver sweep removes it per actor"`, it fails.

## What keeps it true

- `tests/test_scratchpad.py::test_the_memory_store_is_bounded_per_namespace` — driven against a real
  store through `BoundedStoreBackend.awrite`, the path the tool reaches. Mutated by dropping the
  eviction: `15 memories survived a 5-file cap`. Mutated by evicting the newest instead: the wrong
  five survive.
- `tests/test_scratchpad.py::test_evicting_a_memory_is_counted_and_logged`
- `tests/test_scratchpad.py::test_no_first_party_module_writes_to_a_store_directly` — the narrowed
  rule, naming the one exempt file.
- `tests/test_preferences.py::test_a_chemists_preferences_are_bounded_in_number` — mutated by
  removing both evictions: `12 preferences survived a 4-preference cap`.
- `tests/test_preferences.py::test_recall_is_bounded_so_a_chemists_preferences_cannot_grow_a_prompt_without_limit`
  — mutated by removing the slice: 10 where 3 are allowed.
- `tests/test_preferences.py::test_the_preference_cap_holds_against_a_real_table` — the SQL and the
  dict are written separately, so agreeing is asserted rather than assumed.
- `tests/test_retention.py::test_no_disposal_entry_offers_actor_erasure_as_what_bounds_a_table`
