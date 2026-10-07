"""One wrapper for every value this system writes into a `jsonb` column.

Non-finite floats are not JSON, and `jsonb` rejects them only once the statement reaches the server,
as an `InvalidTextRepresentation` naming a token rather than a field — which escapes
reject-and-continue boundaries, fails cache waiters, stalls sync cursors and drops batched records.
`allow_nan=False` raises a `ValueError` in this process instead, at the write that holds the value.

The check belongs at the write, not on the model: tightening a persisted field would strand
in-flight records at replay, and payloads this system did not author are permissive by necessity.
`tests/test_jsonb_boundaries.py` derives the call sites.
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
