# D-2026-09-16-a-buffer-bounded-against-a-dead-database-is-not-bounded-against-a-slow-one — the audit write buffer gets a write-side bound

`PostgresAuditSink.record` appends to an in-process list and returns, which is the whole point of it:
`D-2026-08-27-a-refusal-is-not-a-crash` took the database off the tool-call path, and a 30-step turn's
~90 rows now land as a handful of `executemany` transactions that overlap the model's own work.

Nothing bounded that list on the write side, and the reason it looked bounded is written in
`_flush_all`'s own docstring:

> A failed batch is logged and *dropped* — re-queueing it would make a broken database grow the
> buffer without bound

That is correct, it is load-bearing, and it covers a database that is **down**. It says nothing about
one that is merely **slow**. `record` suspends nowhere, so a producer running at ~90 rows a turn
outruns a drain that has started taking seconds, inside a pod `values.yaml` limits to 1 GiB, with
nothing else in the class that ever shrinks the list. The sentence that made the hole feel covered is
the same shape as the one `api/budget.py` carries for its own gap — "one turn cannot loop forever";
it cannot *loop*, it can *spend* — which is `D-2026-08-29-an-iteration-cap-is-not-a-cost-cap`'s
finding, arriving a second time in a different module.

## The decision

`agent_audit_buffer_max_events` (default 50,000, `0` disables) bounds the buffer. Past it, the
**oldest** events are shed.

**Oldest rather than newest, and the choice is not symmetric.** Both ends lose a row. The end worth
keeping is the recent one, because an operator reaching for this trail is asking what just happened,
and a gap in the middle of an old turn is cheaper than a missing account of the current one.

**Shed is counted on its own series.** `chemclaw_audit_events_shed_total` is deliberately not folded
into `chemclaw_audit_sink_failures_total`: "the database refused this batch" and "the database cannot
keep up with the producer" have different remedies — reachability against throughput — and pooled they
would read as one incident. The shed case also has no exception to log, so it would be the arm of that
counter with no traceback beside it.

**Nothing is lost silently.** Every event has already reached the stdlib log by the time it is
buffered; what shedding costs is durability and ordering in the queryable trail, which is the same
trade `agent/turn_cost.py` made for the same reason and the same trade `_flush_all` already makes for
a failed batch. The WARNING names the count and the bound.

50,000 is about 555 turns of backlog at the measured row rate and a few tens of MB at this row shape:
large enough that an ordinary slow patch never reaches it, small enough that it cannot be the thing
that ends the process. `0` restores the old unbounded behaviour for a deployment that would rather
take the OOM than the gap, and that path is asserted — a bound whose disable path is untested is a
bound nobody can opt out of.

## What keeps it true

- `tests/test_audit_store.py::test_the_write_buffer_sheds_the_oldest_rather_than_growing_without_bound`
- `tests/test_audit_store.py::test_a_zero_bound_restores_the_unbounded_buffer`
- `tests/test_config.py::test_env_example_documents_every_field`
- `tests/test_metric_declarations.py` (the new counter is declared where every metric is declared)
