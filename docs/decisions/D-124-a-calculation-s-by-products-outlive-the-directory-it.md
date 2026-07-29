# D-124 — A calculation's by-products outlive the directory it ran in

`calc/xtb_cli.py` runs xtb inside a `tempfile.TemporaryDirectory`. It writes `input.xyz`, the
binary writes `hessian`, `vibspectrum`, `xtbopt.xyz`, `xtbout.json`, `_collect` parses them into a
`CliResult`, and then the `with` block ends and every file is deleted. The system kept one JSON
summary per calculation and threw away everything else it had paid for.

That is affordable for a single point. It is not affordable for a Hessian. D-092 measured one at 26 s
on 76 atoms through the binary and 218 s through finite differences, and `ThermoSpec` puts
`temperature_k` in the cache key — so asking the *same* molecule for thermochemistry at 350 K after
298 K is a cache miss that recomputes the Hessian, a quantity that does not depend on temperature at
all. The expensive half was being recomputed to answer a question about the cheap half.

### Two tables, because a blob and its role are different facts

`artifact_blobs` is keyed by the SHA-256 of the artifact's **uncompressed** bytes.
`calculation_artifacts` maps `(calc_key, name)` to that hash. Content addressing gives dedup for
free — two runs that converge to the same geometry write one copy of it — and the link row is what
makes a blob reachable *from* a calculation rather than only by its hash. The split is DataJoint's
hash-addressed model, and it is what keeps the design open: a DFT wavefunction or SCF restart file
is another `(calc_key, name)` row over the same blob table, not a new mechanism.

The address is over the uncompressed bytes deliberately, so it does not change when the compression
level does. `ON DELETE CASCADE` from blob to link is load-bearing: evicting a blob removes the rows
that point at it, so `list_for` can never hand back a ref whose bytes are gone.

### Postgres `BYTEA`, and why not the three alternatives

The artifacts this system actually produces are kilobytes to a few megabytes — a Turbomole `hessian`
is repetitive numeric text that deflates several-fold. That is squarely `BYTEA` territory, and
Postgres is the only durable store the deployment already has.

*Not an object store.* It adds an infrastructure dependency, a fourth secret to the three-secret
model, a client library, and a bucket-endpoint host literal that muddies `tests/test_no_egress.py`
for no gain at this size. *Not a shared filesystem CAS.* The service and the workers are separate
pods, so it needs an RWX volume no OpenShift storage class guarantees, plus its own GC and backup
story. *Not `hpc_artifact_store_url`* — that is a **read** endpoint the Nextflow launcher fetches
finished-run blobs from, not a store this system writes to.

The `ArtifactStore` Protocol is the seam. When DFT lands and a wavefunction is 200 MB rather than
2 MB, a third backend is one class and no caller changes.

zlib rather than zstd: Python 3.11 has no stdlib zstd, and a dependency for the remaining ~15% on
text is not a trade this codebase makes. The codec is recorded per row, so changing it later is a new
value, never a migration. Compression that does not shrink a payload is not applied — an
already-compressed artifact would otherwise be stored *larger* than it arrived.

### An artifact is optional by construction

`put` returns `None` — it does not raise — when the store is disabled or the payload exceeds
`artifact_max_bytes`, and the capture path `stat`s every file before reading it so an outsized one
never enters memory. When the *store itself* fails, `run_cached_with_artifacts` logs a warning and
returns the result anyway.

This is the whole contract, and it is deliberate in both directions. Losing an artifact costs a
future recomputation. Propagating the failure would discard a calculation that had already succeeded
and was already in the result store — trading a cheap loss for an expensive one. Capturing a
by-product must never be able to fail the thing it is a by-product of.

Capture happens *after* `_collect` succeeds, so a parse failure raises exactly as it did before this
existed. The cost is that the raw files are then unavailable for a post-mortem on a parse failure.
Plumbing bytes onto an exception to fix that is not worth it; the trade is recorded rather than
hidden.

### The capture manifest is derived, not restated

`_REQUIRED_OUTPUTS` already declares what each task must leave behind for its run to have succeeded.
`_CAPTURED` is that same map minus `sp`, so the two cannot drift — adding a task declares both facts
once.

`sp` is the one exclusion, and the reason is exact: its `xtbout.json` is parsed *in full* into
`CliResult.properties`, which lands in the cached JSON result. Storing the file too would be a second
copy of the cache with none of the value.

### The cost policy `retention.py` asked for, and the eviction it unblocks

`workflows/retention.py` refuses to age-prune `calculation_results` and says why: a cache is bounded
by cost policy, not by a retention clock, and evicting a cached result silently converts a hit into a
recomputation. It then names the policy it would need — "LRU by access, or by compute cost" — and
declines to invent it.

`cached_compute` now times every miss and stores `compute_seconds`. That is the missing number, and
it resolves the tension rather than reopening it: **eviction targets blobs, never results.** The JSON
result is the *answer*, and evicting it would void D-011. A blob is a *by-product* from which the
answer can be regenerated, so evicting one costs recompute time on a future reuse and nothing else.
`retention.py`'s refusal therefore stays literally true.

There is deliberately no `last_access_at` on `calculation_results`. Nothing evicts it, so nothing
would keep the column current, and an access stamp on the cache-hit path is a write on the hottest
read in the system. On `artifact_blobs`, where eviction does need it, the stamp is refreshed lazily —
only once the recorded value is already older than `artifact_access_stamp_seconds` — so a read on the
reuse path stays a read.

### What this does not yet do

The store is wired into the thermochemistry path, which is the one that pays for it. The optimizer
and the conformer ensemble capture their files but do not yet persist them, and the eviction sweep
is designed here but not built. The reuse that makes the stored Hessian *worth* storing — thermo at a
second temperature without recomputing — is D-125's, and is the reason this landed first.

The end-to-end assertion that a captured Hessian reparses to the same matrix is written and
`@needs_xtb`-gated; it does not run where the binary is absent, which includes this session's
environment. It is a real test of a real property, and it is unverified here.
