# D-2026-09-09-a-number-that-is-not-json-stops-a-corpus — a non-finite float is refused where it is produced, not where it is stored

**Status:** accepted · **Date:** 2026-09-09 · **Builds on:**
D-2026-08-25-an-eln-transcription-is-data-not-a-claim (the sync loop this wedges, and the
reject-and-continue that was supposed to survive it), D-2026-08-25-a-cache-is-not-a-record (the
publish outbox and its two failure counters), D-2026-08-04-the-schema-is-a-file (the binding
vocabulary a warehouse value passes through) · **Beside:** `chemclaw.core.jsonb`, wave 11's shared
`json_column` guard, which this decision is one of five call sites of.

## Context

`NaN` and `±Infinity` are floats to Python and are not JSON. Postgres says so only after the
statement reaches the server — as `psycopg.errors.InvalidTextRepresentation` naming a *token*
rather than a field:

```
psycopg.errors.InvalidTextRepresentation: invalid input syntax for type json
DETAIL:  Token "NaN" is invalid.
CONTEXT:  JSON data, line 1: {"temperature_c": NaN...
```

Measured on this tree, that costs something different on each path it reaches, and on the ELN path
it is an outage rather than an error.

**The ELN sync stops, permanently.** `sync_entries` rejects one bad entry and continues, catching
`(ChemclawError, ValidationError)` — the two types every adapter, mapper and model raises. A
`psycopg` error is neither, so it walked past the guard, aborted the pass and returned no summary.
The cursor is the return value, so nothing advanced it; the source is re-fetched from the same
`since` on the next scheduled run; the input is deterministic. **One entry therefore holds an
entire corpus at a fixed date forever**, and what a chemist sees is an ELN that stopped producing.
Driven end to end against the real column, three entries in:

```
sync_entries RAISED psycopg.errors.InvalidTextRepresentation
  caught by the per-entry guard? False
  -> no summary, no cursor advance; entry 3 never reached
```

**Three places let it through, and the guard was one of them.** `_number`, the transform whose
docstring says a boolean "is not a measurement", returned `float("NaN")` from the *string* `"NaN"`
and any non-finite float unchanged. `_clamp` — the transform whose entire job is holding a number
inside a range — returned `nan` from `max(nan, 0.0)`, because every comparison against NaN is
false: `clamp(nan, 0..100) = nan`. And `ProcessConditions` had no `allow_inf_nan=False`, so the
value reached `reaction_records.conditions`.

**Four of that model's five numeric fields were guarded by accident, and only three of them
fully.** `ge`/`le` reject NaN for the same reason clamping failed to hold it, so `yield_percent`,
`purity_percent`, `impurity_area_percent` and `time_h` were covered by bounds nobody chose for that
purpose — while `temperature_c`, the one field with no natural bounds to state, took it, and
`time_h`'s `ge=0.0` still admitted `+Infinity`. A guarantee that is a side effect of a range chosen
for something else disappears the day that range is widened.

**The publish outbox loses the good records beside the bad one.** `enqueue` looped every record
inside one transaction with one `except Exception` around the whole of it:

```
publish[enqueue:write]: could not queue 3 record(s) for postgres
enqueue returned: 0 ;  rows queued: []
```

`records_for` genuinely decomposes one payload into several — a solvent screen queues the aggregate
and its parts — so those siblings are one calculation's own facts. The counter could not say how
many good documents went with the bad one, and none of `publish.record`'s twelve models refused a
non-finite value at projection, where the failure would have been counted as the permanent shape
problem it is.

## Decision

**A non-finite number is refused at the boundary that produced it, in this process, naming the
field.**

1. `expr._number` refuses NaN and ±Infinity as `TransformError` — an `ElnMappingError`, which is
   what the reject-and-continue arm already catches — quoting the column's own text so a site can
   search its warehouse for what it holds. `_scale` and `_clamp` route through it, so the guard has
   one definition.
2. `ProcessConditions` sets `allow_inf_nan=False`. The identical value is now one rejected entry
   with `temperature_c` named in the ledger, and the pass finishes and advances its cursor.
3. `publish.record` gets one `_STORABLE` config used by all twelve models
   (`extra="forbid"`, `frozen=True`, `allow_inf_nan=False`), so a NaN is refused inside
   `records_for` and counted as `chemclaw_result_projection_failures_total` — the series whose
   declared meaning is a permanent gap in this release — instead of as a destination having a bad
   day.
4. `outbox.enqueue` queues each record inside its own **savepoint** and uses `json_column`, so one
   refused document costs one document, is logged by `calc_ref`, and is counted once.

## Why a savepoint rather than a `try` per record

Because the two failures are on opposite sides of the wire and only one survives a bare `try`.
psycopg refuses a NUL in its own text dumper and leaves the transaction healthy; Postgres refusing
a value aborts the transaction, so every later `INSERT` fails with `InFailedSqlTransaction` and the
final `COMMIT` takes the good rows with it anyway. `tests/test_publish_outbox.py` carries one of
each in a single batch, deliberately: a test with only the client-side poison passes the weaker fix.

## What was rejected

**Catching `psycopg.Error` in the sync's per-entry guard.** It would have fixed this symptom and
broken the loop's meaning: a transient database outage would then be recorded as a per-entry
*rejection*, with the cursor advancing past entries that were never bad. Rejection is a statement
about the data. Widening it to cover infrastructure makes it a statement about nothing.

**An opt-in for a non-finite value in a binding.** Nothing in the schema this engine maps onto has
a field an infinity is an answer to, and an option with no caller is the abstraction this
repository's own rules forbid. The boolean refusal beside it takes no option either.

**Tightening `OrdReaction` instead of `ProcessConditions`.** The refusal has to hold for anything
that reaches the column, and `ProcessConditions` is what is written into it. `OrdReaction` is a
transient mapping shape; guarding there would leave the column's own contract stated nowhere.

**Silently reading NaN as absence.** Tempting, because a missing float *is* NaN in a pandas-shaped
export — but not in this seam: every driver here returns `None` for a NULL, and a Spark `DOUBLE`
holds a stored NaN as a value. Reading it as silence would erase a temperature a chemist recorded
badly, with nothing in the ledger to say so.

## The three smaller things in the same commit

**`reaction_labels.confidence` was `REAL`.** The only single-precision column in the schema —
`grep -rn '\bREAL\b' infra/sql schema` matched one line — against a `ReactionLabel.confidence:
float`, which is a double. So `1/3` came back `0.3333333432674408` and `0.95` came back
`0.949999988079071`. Nothing compares it today, which is why it stayed invisible and is exactly why
it is worth fixing before something does: a facet query growing `WHERE confidence >= 0.95` would
silently exclude the row stored *as* 0.95. Migration 091 widens it. The rewrite takes an ACCESS
EXCLUSIVE lock, affordable because `infra/sql/README.md` already calls this table derived and
rebuildable.

**A number this system computed is rendered; a number the source reported is echoed.** The body of
an ELN record interpolated raw floats, so `195.15 K` — a dry-ice bath, which every chemist writes
as −78 °C — reached retrieval and a human reader as `-77.99999999999997 °C`. That tail is an
artefact of a conversion *this system* performed (K→°C, minutes→hours, g→mg), not something the
entry said. Yield, purity and impurity area are read verbatim out of the entry and are left alone:
their digits are the chemist's own.

`_measured` renders the computed ones at twelve significant figures rather than `:g`'s six, because
six is a real loss on the same lines: a kilo-scale charge published as `1.23457e+06 mg` throws away
digits a five-place balance measured. Twelve is past any instrument in this domain and short of
where the binary noise lives. **The stored value is untouched** — `conditions.temperature_c` keeps
the full double, which is what every comparison reads. The prose does not decide the record.

**And the charge sheet's docstring promised a consumer that does not exist.** It said the block
exists so "a reader (or a downstream consumer) can … recompute stoichiometry instead of taking a
derived figure on trust", while `grep` finds no parser of `## Charge` anywhere and the numbers had
already been through six significant figures. A column was the alternative and is declined:
`reaction_records` holds no per-species amount, and adding one is a design decision — what shape,
whose unit, keyed how — taken on behalf of a reader nobody has yet. The claim is withdrawn rather
than the column built, and the day something does ask for amounts, the honest answer is that column
and its migration, not a parser for prose.

**And `publish/properties.py` held the third copy of the hartree→kcal/mol conversion.** The
units/thermo review of the same day made `core.units.HARTREE_TO_KCAL` the one definition — three
copies existed and two were short enough to disagree, the registry's derived kcal/mol coming out
1.5e-08 relative low — and this file was the last restatement. It now imports it and derives the
reciprocal rather than writing a second literal. Measured before shipping, because this is a
publish path and a moved number would be a moved published value: both literals agree with the
imported constant **bit-for-bit**, so nothing this registry converts changes. `publish` importing
`core` is the layering that `tests/test_layering.py` already permits, checked rather than assumed.
