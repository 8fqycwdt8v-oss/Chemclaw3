# D-2026-08-06-the-warehouse-boundary-and-a-probe-that-measured-the-wrong-call — The warehouse boundary, and a probe that measured the wrong call

**Status:** accepted · **Date:** 2026-08-06

## Context

Three rows from the security sweep's data-plane lane, all in the engine that reads a site's ELN out
of its own warehouse (D-2026-08-04-the-schema-is-a-file):

- **[L] `vector.server_embed_function` reaches the SQL text unchecked**, so the module's "only
  checked identifiers are written" invariant is false. Distinct from the documented `where:` trust
  boundary.
- **[L] A warehouse row key is interpolated into a filesystem path** with no slug validation.
- **[M] `SnowflakeWarehouse._connect` classifies every client error as retryable `ConnectionError`**,
  and no test executes any function body in the module.

## Decision

### The one value written into SQL without a check

`sql.py`'s header states the invariant — *"Every value is bound; only checked identifiers are
written"* — and `server_embed_function` is interpolated straight into the statement text. Every
relation and column in a binding goes through `_check_identifier`; this one did not. Reproduced by
constructing a `VectorBinding` whose function is `(SELECT secret FROM credentials) -- ` and watching
it validate.

A function name is a dotted identifier (`SNOWFLAKE.CORTEX.EMBED_TEXT_768`), so it takes the same
rule the rest of the binding takes rather than needing one of its own.

The `where:` clause stays raw, deliberately: it is SQL an operator writes, and it announces itself as
such. The difference is that `server_embed_function` announced itself as an identifier.

### The path barrier belongs where paths are built

`Note` validates `id` and `type` as slugs, which covers every *write* — the PR-gate constructs a
`Note` before it writes anything. The warehouse retriever asks a different question ("has this row
already been merged?") and answers it by `stat`-ing a path built directly from a raw warehouse key,
bypassing the model that carries the barrier.

So the rule now also guards `note_relative_path`, the choke point the PR-gate and every reader share,
and is exposed as `is_note_slug` for the caller that wants an *answer* rather than an exception: a
hostile key on one row must not fail a chemist's whole query, and "no note could ever have that id"
is the honest answer to the question being asked.

### The measurement was wrong twice before it was right, and the correction is the point

The first probe used `Path.resolve()`:

```
'../../../../etc/passwd' -> /var/lib/chemclaw/note-repo/etc/passwd.md   escaped=True
```

**The code does not call `.resolve()`.** It calls `.is_file()`, and the OS will not walk `..` through
a component that does not exist — `knowledge/reaction/reaction-../../x.md` needs a real directory
named `reaction-..`, and there is none. That key escapes nothing.

Measured on the primitive the code actually calls, the finding is real and *conditional*: with a
directory under `knowledge/reaction/` for the traversal to stand on, `is_file()` returns **True** for
a file outside the knowledge tree. That is why the row is [L] — notes are files, so the stepping
stone is not normally there, and it is a `stat` either way.

Two tests were unfalsifiable on the way, and mutation is what caught both. The first asserted `False`
against paths that do not exist, where "refused" and "not found" are the same answer. The second
created a probe file at the `.resolve()` target — a location the real traversal never reaches — and
still passed with the guard removed. The shipped test builds the stepping stone, asserts the
traversal reaches the probe file *before* asserting the refusal, and fails under mutation.

### Connection failures split by what a retry could change

Every client error became `ConnectionError`, which this package's own split reserves for "the
warehouse is unreachable, retrying may work" (`WarehouseQueryError` documents the other half). A
wrong password, an unknown account and a missing role are none of those: they fail identically every
time, so the sync burned its Temporal retry budget before an operator saw a message that then said
*"cannot connect"* about a credential problem.

The split follows the DB-API 2.0 hierarchy the client implements: `InterfaceError` (the client or the
call) and `ProgrammingError` (a request the server understood and refused — where authentication and
authorization land) become `WarehouseQueryError`, which `durable.publish` already marks non-retryable
by class name. The operational family keeps `ConnectionError`.

**What is not verified, stated rather than implied**: the mapping from Snowflake's own error codes
onto those classes needs a real tenant. Until one exists this is the documented contract rather than
a measured one — and it is strictly better than the previous behaviour either way, since every one of
these was retryable before, including a typo'd password.

### The module now has executed tests

The sweep's second finding about `snowflake.py` was that no test ran any function body in it, which
is why `_connect`'s classification could be wrong unnoticed. The client is not a dependency of this
repository, so the tests drive it through the existing `_client()` seam with a fake exposing the
DB-API hierarchy — the same shape the engine itself was proven with. They cover the classification in
both directions, connect-once, and that the timeouts and `paramstyle="qmark"` reach the client, which
is what makes every value in this engine a bound parameter.

## Consequences

- A binding naming a non-identifier embedder is refused at load, with the same message shape as every
  other identifier in the file.
- A hostile warehouse key answers "not ingested" instead of stat-ing outside the tree; a non-slug
  segment raises from `note_relative_path`, which is a second barrier below `Note`'s own rather than
  a duplicate of it.
- A refused credential now fails fast with a message naming what to check.
- `tests/test_warehouse_boundary.py` is the first executed coverage of the Snowflake module.

## Alternatives rejected

- **Validating the row key inside the retriever alone.** Leaves the next caller that builds a path
  without a `Note` unprotected, which is exactly how this one arose.
- **Raising from the retriever on a hostile key.** One bad row would fail a chemist's whole query.
  The question asked is "is this already ingested", and "no" is both true and safe.
- **Enumerating Snowflake error codes.** Cannot be verified without a tenant, and a table of guessed
  codes is worse than the standard hierarchy: it would look measured.
- **Treating `where:` as an injection too.** It is documented raw SQL under an operator's control;
  changing it would break the feature and misplace the boundary.
