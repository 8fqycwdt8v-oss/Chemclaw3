"""One wrapper for every value this system writes into a `jsonb` column.

**Non-finite floats are not JSON, and `jsonb` rejects them** — but only after the statement
reaches the server, as an `InvalidTextRepresentation` naming a token rather than a field. Wave 11
measured what that costs on five independent paths, and it is different damage every time:

- the calculation cache raised the driver error out of `store.put` *inside* `cached_compute`, so
  every concurrent waiter on that key failed with it and the value recomputed forever;
- the ELN sync's error was neither `ChemclawError` nor `ValidationError`, so it escaped the
  per-entry reject-and-continue, aborted the pass and **never advanced the cursor** — one site row
  holding an entire corpus at a fixed date, deterministically, on every scheduled run after it;
- the publish outbox lost the good records batched beside the poison one;
- a campaign's `record()` succeeded against the in-memory oracle and raised against Postgres, from
  one input, failing the tool call *after* the candidates had been computed.

The rule was already known here and applied by hand: `protocols/models.py` sets
`allow_inf_nan=False` on ten models, and `science/bo/campaign_record_store.py` wrote the argument
this docstring inherits. A rule applied by hand is applied unevenly, which is why it is one
function now and why `tests/test_jsonb_boundaries.py` derives the call sites rather than listing
them.

**The check belongs at the write, not on the model.** Tightening a persisted field would strand an
in-flight campaign at replay (`require_names_do_not_clash` makes the same argument), and a payload
this system did not author — a calculator's result, a site's ELN row — is permissive by necessity.
So the store owns its own boundary: `allow_nan=False` turns the wall into a `ValueError` raised in
this process, at the column holding the value, with a stack naming the caller.
"""

import json
from functools import partial
from typing import Any

from psycopg.types.json import Jsonb

#: `partial` rather than a lambda so psycopg's dumper cache keys on a stable object.
STRICT_JSON = partial(json.dumps, allow_nan=False)


def json_column(value: Any) -> Jsonb:
    """Wrap a value for a `jsonb` column, refusing non-finite floats here, not at the wall."""
    return Jsonb(value, dumps=STRICT_JSON)
