# D-2026-09-22-a-page-budget-counts-matching-time-not-wall-clock — the ELN regex page bound

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing. Closes the `BACKLOG.md` row *"A
per-cell regex budget does not add up to a page bound"*, which
`D-2026-09-21-a-pattern-that-cannot-be-timed-out-is-run-by-an-engine-that-can` opened as what it did
not do.

**This ADR is late.** The code, the tests and the configuration landed in wave 4
(`eln_regex_page_budget_seconds`, `expr.pattern_budget`, five call sites); the record did not, and
the row stayed in `BACKLOG.md` reading as live state while the bound it asks for was shipped. That
is the failure `CLAUDE.md` legislates against in as many words — "a row that outlives its closure
reads as live state" — and the wave's own plan file claimed both the ADR and the deletion were done.
Written now from the code, which is where the measurements live.

## Context

`eln_regex_timeout_seconds` bounds **one** `search`. `warehouse/adapter._read` runs one per reaction
field, per attribute, and per component and impurity *row*, so a page is `eln_sync_batch_size ×
cells_per_entry` matches and the per-cell ceiling multiplies.

**The row was wrong about which pattern reaches this**, and finding that changed what the fix had to
bound. It described the accumulation as per-cell timeouts adding up. They cannot: a cell that
exceeds its budget raises `PatternBudgetError`, which is in `durable/publish._BAD_DATA_TYPES` and so
is non-retryable, ending the page after **one** cell. The reachable case is the opposite — a pattern
that is slow and *completes*. Measured: `a*a*a*$` over a 6,000-character cell is **165 ms**, 66% of
the per-cell budget and never refused; twenty such cells across the shipped 100-entry batch is
**330 s**, past `eln_sync_timeout_seconds` and past the heartbeat, with 1,818 of 2,000 cells reached
before the activity's own deadline. `map_to_ord` is synchronous CPU work, so no asyncio timer
interrupts it and the retry runs the identical page.

The row called the fix "a decision rather than an edit, because it trades refusing an honest slow
pattern against bounding total work". **That trade is settled by measurement rather than argued.** An
honest cell is **0.0024 ms** warm, so an honest page of 2,000 cells is **0.0048 s** against a 150 s
ceiling — about 31,000x of headroom, with the pathological pattern about 68,000x an honest one. Four
orders of magnitude apart, a budget generous enough never to touch an honest page still bounds the
pathological one well inside the activity deadline.

## Decision

**One budget per page, charged in matching time, opened where the loop is.** Four choices, each with
a rejected alternative:

- **Per *page*, not per entry or per activity.** The row named three. Per-entry gives a binding at
  twenty cells of 12 ms each, which is inside the honest-page headroom above and therefore refuses
  honest work; per-activity cannot be read from inside `map_to_ord`, which is where the spending
  happens. `eln_regex_page_budget_seconds` defaults to **half of `eln_sync_timeout_seconds`**, and
  that is stated as a *split* rather than a measurement, because the page also writes and nobody has
  measured that.

- **An accumulator of charged matching time, not a `monotonic()` deadline.** The first spelling was
  a deadline, and a review measured what it charged: the manager is opened *around* the page loop,
  whose body awaits five stores per entry and, in `durable/memory_jobs.read_corpus`, every
  `fetch_new_entries` of every page of every source. So a "budget for every transform together" was
  billing Postgres and the source — **1.13 ms** of actual matching exhausted a 500 ms budget, and
  the refusal then told the site to simplify patterns costing microseconds. Worse than a wrong
  message: because `PatternBudgetError` is non-retryable, a page that used to reach
  `eln_sync_timeout_seconds` and be **retried** would instead fail permanently at half of it with no
  cursor advanced. `_PageBudget` now holds `spent` and `searches` and is charged per search.

- **Each search clamped to what the page has left** (`_cell_budget`), so the last one cannot overshoot
  by a whole cell budget. Driven: +0.001 s of overshoot at a 2 s budget, against 32.5 s unbounded.
  The clamp is also why a spent page must never hand the engine a negative timeout — `regex` reads
  `timeout=-1.0` as *no* timeout, which would turn the bound into its opposite.

- **Three refusals, not two.** A page spent *before* a search, a page that ran out *inside* one, and
  a pattern that blew its own unclamped ceiling. The two-way version's pattern arm was unreachable
  by construction: a clamped search is given exactly what the page has left, so it times out at the
  instant the page runs dry, and over 39 clamped remainings against `(a+)+$` the pattern arm fired
  **0** times — every catastrophic pattern was reported under a sentence claiming no transform had
  exceeded its ceiling. The clamped arm now says the pattern's own cost is *not established*.

Opened at **five** sites — `ingest/eln/sync.py::sync_entries`, `durable/memory_jobs::read_corpus`,
`ingest/eln/validate.py`, and both `cli/live_data.py` loops — rather than at each activity, for the
reason `turn_caps` was extracted: the bound belongs where the loop is, and an activity cannot see how
many pages it will run.

## Consequences

**A derived guard finds the sites rather than a list.** A module with a `map_to_ord` call *inside* a
loop must open a budget; it found the `validate.py` site, which the first three did not include. A
companion test pins the four modules the guard matches so it cannot pass vacuously, and its first
spelling over-matched three modules that map one entry at a time — the guard asks whether the call
is inside the loop.

**Re-entrant, and that is the conservative direction.** A nested `pattern_budget` keeps the outer
budget, because the outer one is the bound that matters and a page is not made cheaper by being
processed in parts.

**A figure in here was wrong and the correction made the argument stronger.** The first version of
the honest-cell measurement said 0.472 ms and ~160x, and in the same table also said 0.042 s for the
page — two numbers 22x apart for one quantity, neither right. The 0.472 ms was the *first* call,
including the module's `lru_cache` compile miss, which is 0.3 ms on its own. The corrected 0.0024 ms
is what gives the four-orders-of-magnitude headroom the decision rests on, so the wrong number was
never load-bearing; it is recorded because a number that survived into an argument once will again.

**Revisit when:** the honest-page headroom stops being four orders of magnitude — a warehouse binding
whose patterns are legitimately expensive, or a batch size raised far past 100. The file that would
show it is `expr.pattern_budget`'s own docstring, which carries the honest-cell and honest-page
measurements; re-measure them against the deployment's real manifest rather than this one's fixtures,
because the whole decision is that ratio and nothing else.
