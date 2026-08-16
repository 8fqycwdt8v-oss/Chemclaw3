# D-2026-08-16-a-result-too-big-for-its-row-is-an-artifact — the Hessian goes back to the artifact store

**Status:** accepted · **Date:** 2026-08-16 · Restores the design
[`D-124`](D-124-a-calculation-s-by-products-outlive-the-directory-it.md) established, which
`D-2026-08-16-the-physics-leaves-the-cache-stays` inverted without meaning to.

## Context

Before the capability migration, a Hessian's matrix lived in the content-addressed artifact store
and `calculation_results` held a small row addressing it. Two mechanisms depended on that shape:
`durable/artifact_eviction.py` reclaims artifacts by cost and idle time, and `durable/retention.py`
is able to refuse to prune `calculation_results` *at all* — which D-011 requires, since a persisted
result is never recomputed — precisely because those rows are small.

The migration moved the physics to `Chemclaw3-mcp`. The server returns its arrays base64-encoded
inside the tool payload, and the payload was cached whole. Nothing failed, nothing was logged, and
three things stopped being true at once:

- `science/calc/artifacts.py::put_all` — the artifact store's only writer — lost its production
  caller when `xtb_hessian.py` was deleted. It kept exactly one caller: `tests/test_artifacts.py`,
  calling it directly.
- The daily `ArtifactEvictionWorkflow` began running against a table nothing fills.
- `list_artifacts` and `fetch_artifact` stayed declared in `connectors/calc/connector.yaml` and
  answer nothing for any calculation performed after the split.

And the matrices — about 1.4 MB for a 120-atom molecule, measured — began accumulating in the one
table whose pruning is disabled by design. The ADR that moved the physics recorded the deletion of
`run_cached_with_artifacts` as "zero production callers"; what it did not notice is that `put_all`'s
real caller went at the same time, inside the module being deleted.

## Decision

**A calculation result too large to live in its row goes to the artifact store, and the row keeps a
content hash.** The policy is expressed as `science/calc/artifacts.py::ArrayOffloadingStore`, a
`ResultStore` that wraps another one, and `connectors/calc/compose.py::hessian` is the only caller.

Two rules carry it, both inherited from the pre-split implementation because the reasoning behind
them did not change:

1. **A hit is a hit only if the blobs come back.** Every reason they might not — the store disabled,
   the matrix reclaimed as cold, a database restored without its artifact table — is ordinary, so a
   missing blob is a *miss to recompute from*, never an error.
2. **The blobs are written first, and the row only if they all landed.** A row addressing an
   artifact that does not exist would be served as a hit forever and rejected on every read, which
   is strictly worse than not caching: it converts one recomputation into a permanent one.

## Why a `ResultStore` rather than a second caching path

The pre-split code answered this differently — `run_cached_hessian` was a bespoke cached path,
justified on the grounds that `run_cached` "decides hit-versus-miss from the result row alone, and
here the row is only half the result". That reasoning is sound and the conclusion no longer follows,
because the two decisions it names are *already* behind the store's interface: "is this a hit?" is
`get`, and "is this worth caching?" is `put`. Expressing the policy as a store means `cached_remote`
and every caller of it are untouched — a caller wraps its store and nothing else moves.

Writing it the other way was tried first and rejected on evidence rather than taste. A bespoke path
in `compose.py` has to open its own session to get the key before the lookup, which made `compose`
a *second* module that opens one — and `tests/calc_server_fake.py::install` patches
`connectors.calc.remote.calc_session` on the stated ground that it is "the one module that opens a
session". The heartbeat test went red immediately, dialling a real socket. That test failure is the
argument: an invariant the test infrastructure depends on is worth more than a slightly more direct
implementation.

## What this does not do

**It does not put a size threshold on caching.** The deleted `xtb_hessian_max_atoms` bounded what
could be *computed*, not what could be stored, and that ceiling now belongs to the server, which is
the only side that can enforce it. Refusing to cache a large Hessian here would be a silent D-011
exception for exactly the calculations most expensive to repeat.

**It does not rehydrate on `find`.** `find` answers "which calculations exist", which
`find_calculations` renders as a listing; restoring megabytes per row to build a table nobody reads
matrices from would make a listing the most expensive call in the system. The rows come back naming
their artifacts, which is what `fetch_artifact` takes.

## Consequences

- `list_artifacts`/`fetch_artifact` answer again for post-split calculations, and the eviction sweep
  has something to sweep.
- Evicting a cold matrix costs a recomputation. That is the trade D-124 made deliberately, and
  keeping every matrix forever was never a trade anyone chose — it was a side effect nobody saw.
- `run_cached` was deleted in the same pass. It had zero production callers and was kept alive by a
  test calling it directly, which is the shape `CLAUDE.md` names as a claim that something exists.
- Five tests pin the policy in `tests/test_artifacts.py`, each verified red against a deliberate
  mutation of the rule it protects: offloading removed, the dangling-row guard inverted, and a
  missing blob treated as a hit.
