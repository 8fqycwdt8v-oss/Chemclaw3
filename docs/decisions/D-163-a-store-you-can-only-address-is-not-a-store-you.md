# D-163 — A store you can only address is not a store you can ask

## Status

Accepted. Implements W2.2 of the dataflow review's plan; the second half of the pair D-158 opened.

## Context

The calculation store has been content-addressed since D-011: give it the exact
`CalculationKey` — calculator, version, input, parameters — and it returns the result. That is
everything the compute cache needs, and it is the *only* thing the store could do. `ResultStore`
had two methods, `get` and `put`.

So the only way to reach a stored value was to ask for the identical calculation and be served
from the cache. For a sub-second xTB single point that is merely wasteful. D-158 made it a real
problem: it started persisting **DFT** results into the same table, so hours of HPC compute now
accumulate in a store nothing can look into. "Have we computed this molecule before?" — the
question a chemist asks *before* committing that time — had no answer at all, and neither did
"what do we already know about this compound", which is the same question asked earlier.

`001`'s indexes match what the store could do rather than what it holds: a primary key on the flat
`key` and one index on `(calc_type, calc_version)`. Nothing on `input_hash`, nothing on
`created_at` — the two columns any question filters or orders by — so both would have been
sequential scans over the one table the system never evicts, getting slower for as long as the
deployment ran.

## Decision

**`ResultStore` gains a third method, `find(CalculationQuery) -> list[StoredResult]`,** implemented
by both backends, exposed as the `find_calculations` MCP tool on the `calc` connector, and indexed
by migration `024`.

`StoredResult` gains `created_at`. `get` leaves it `None` — a cache hit does not care when the
value was computed — and `find` fills it, because "what do we have on this molecule" is
unanswerable without it.

### A molecule is found by hashing, never by scanning

`input_hash` is a hash of the calculator's input mapping and is not reversible. The query
therefore hashes the *query* molecule the same way a key is built and compares for equality —
`molecule_hash` is the single definition both backends use, so the shape cannot be right in one
store and wrong in the other. Canonicalisation happens inside it, so `CCO` and `OCC` find the same
rows.

### A molecule filter that cannot address a family is refused, not answered emptily

The input shape is not uniform across calculators. Most key on `{"smiles": <canonical>}` — pka,
solubility, descriptors, and the DFT results D-158 added. The xTB task family keys on
`(structure_id, charge, multiplicity)` and the geometry pointer on its whole subject model,
because a *3-D structure* is what those calculations are actually about, and a molecule does not
determine one.

So a molecule filter genuinely cannot reach them, and the tempting answer — return the empty list
— is the worst one available. `find_calculations` exists to be trusted when it says nothing was
found, and "that family cannot be looked up this way" rendered as "nothing has been computed"
would send a chemist to spend hours recomputing something already on file. `CalculationQuery`
rejects the combination at construction and names the alternative; the tool's docstring states the
limit rather than leaving the model to infer it from a silence.

The uniform fix — a `subject` column every calculator populates — is a larger change than this
one, touches every calculator's key construction, and does not reach rows already written. It is
the right next step *if* looking up xTB task results by molecule turns out to be wanted; it is not
required by anything today.

### No filter on the result's value

The plan sketched one. The payload is an opaque calculator-owned mapping and the store has been
calculator-agnostic since D-011 — a `total_energy_hartree > x` predicate would put one
calculator's schema inside the thing that persists all of them, and every calculator added later
would either fit that schema or be unfilterable. A caller filters the returned rows.

### The cap is the deployment's, not the model's

`calc_find_max_results` (default 50) clamps the tool's own `limit` argument. The store is never
evicted, so it is the one table that only grows; an uncapped browse is a full scan of it, and
every returned row spends the model's context.

## Consequences

- The expensive results D-158 started persisting become reachable — which is what makes persisting
  them worth anything. Before this, a DFT result could only be found by asking for the same DFT
  job again.
- A found result carries `calc_ref`, the same flat key a note's `calc_refs` cites, so an answer
  built on a stored value stays traceable to the run that produced it without a second lookup.
- Two indexes are added and `001` is untouched. An applied migration is never edited.
- `ResultStore` is `@runtime_checkable`, so a backend missing `find` still satisfies the Protocol
  at runtime and fails only where it is called. `tests/test_postgres_store.py` runs the same
  queries against both backends for exactly that reason: the SQL expresses the same predicate as
  the in-memory `_matches`, and nothing but a test keeps them equal.
- A result with no `created_at` fails a windowed query rather than passing it — the same rule
  D-162 applied to undated notes, for the same reason.
