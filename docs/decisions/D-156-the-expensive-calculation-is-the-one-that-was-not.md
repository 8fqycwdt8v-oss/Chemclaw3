# D-156 — The expensive calculation is the one that was not cached

## Status

Accepted.

## Context

D-011 says a result is computed once and never recomputed, and `calculation_results` is never
evicted. The `calc` bundle obeys it: every xTB energy, geometry, pKa and solubility it produces goes
through `run_cached`, keyed by a versioned `CalculationKey`. The `qm` bundle did not obey it at all
— it touched no store. Grep its whole directory for a store import and the only hit was a docstring
about fetching artifacts over HTTP.

So the economics were exactly inverted. A two-second semiempirical energy was cached forever; a
multi-hour DFT run on the cluster was cached nowhere. Its only durable homes were:

1. **Temporal's event history**, bounded by the cluster's retention, and not a queryable scientific
   store in any case; and
2. **a PR-gated `job-result` note**, which exists only if a human merges the pull request.

Both are conditional. If the PR sat unmerged and the execution aged out of retention, the result was
gone. Worse, the only thing preventing a *repeat* run was the deterministic workflow id
(`ALLOW_DUPLICATE_FAILED_ONLY` on `qm-<hash>`), and Temporal frees that id when the execution ages
out — so the byte-identical request re-ran hours of cluster time and, had persistence existed, would
have overwritten an identical row. Real money, spent twice, silently.

There was a second, quieter half. `Note.calc_refs` and the whole of `chemclaw.kg.crosslink`
(`calc_ref_index`, `cited_calculations`, `notes_for_calculation`) have existed since D-133 to answer
"which notes rest on this calculation" — and **nothing in `src/` ever wrote a `calc_ref`.** Only
tests did. The read side was complete and permanently unreachable. `connectors/qm/knowledge.py` even
names the underlying problem, having closed the other direction of it:

> every computed result was a graph island — the calculation store and the knowledge graph, the two
> halves of the system's memory, could not reference each other in either direction (STO-7).

## Decision

**A durable connector result is persisted in the shared calculation store like any other
computation, and the note that describes it cites the row.** Concretely, in the `qm` bundle:

- `chemclaw.connectors.qm.cache` derives a real `CalculationKey` for a QM job. It is *not*
  `qm_job_key`, which is a bare 16-character digest and fails `Note`'s `_CALC_REF` shape check
  outright. Following the `calc` convention, the **molecule** is the input, **method and basis set**
  are parameters, and the **pipeline version** is the calculator version — because a pipeline change
  is the thing that makes a stored number stale.
- Two activities on the bundle's own queue: `lookup_qm_result` before submission and
  `persist_qm_result` after parsing. A hit returns the stored result and skips submit/poll entirely.
- `note_from_qm_result` takes the key and records it in `calc_refs`, making the QM job the **first
  producer** for the crosslink read side.
- `qm_persist_to_calc_store` (default **on**) is the escape hatch for a deployment whose qm worker
  has no reachable Postgres.

### Why the flag defaults on, against the usual convention here

New flags in this repo default off, and the reason is always that they need a prerequisite nobody
has provisioned — a table, a subscription, a credential. This one has no such prerequisite: the
table has existed since migration 001, the write is an idempotent content-addressed upsert, and
D-011 already *requires* the behaviour. This is not a new opt-in capability; it is one bundle
starting to comply with an existing rule. Defaulting it off would ship the bug and call it a
setting.

### Why the cache lookup is a separate activity rather than a workflow-side check

The workflow must stay deterministic and sandbox-safe, and deriving the key canonicalizes the SMILES
through RDKit. `QmCacheLookup` therefore carries the key *and* the optional result, so the workflow
has one shape to handle on hit and miss alike and never re-derives anything.

### Why the flag is read inside the activity, not the workflow

A workflow that branched on config would decide differently on replay if an operator flipped the
setting mid-run. One activity round-trip when the feature is disabled is the price, and it is
nothing next to a DFT run.

### A cache hit is re-attributed to the current requester

`requested_by` rides on `QMJobResult` but is deliberately **not** part of the key — the energy of a
molecule does not depend on who wanted it, which is exactly why `qm_job_key` excludes it and why
identical science shares one entry across users. Returning the stored value on a hit therefore
credits every future reuse to whoever computed it first, and because that string becomes the note's
`source`, the GxP audit trail would name a chemist who never requested the run.

So `lookup_qm_result` rebinds it: the science comes from the store, the attribution comes from this
request.

This was found by CI rather than locally, and the mechanism is worth recording. The workflow tests
skip without the Temporal test server, which a network-restricted sandbox cannot download — so
locally the cache path was only ever exercised against an in-memory store, one test at a time.
Against CI's real Postgres, one test's persisted result was served to the next test's
differently-attributed request and the pre-existing memo test failed. `test_qm_persistence.py` now
pins the behaviour offline, where it can be caught without a server.

The same run exposed a second, smaller problem: the memo test shared a molecule with the envelope
test, so once the cache existed it passed through the cache instead of the cluster path it is named
for. It now uses its own molecule and stays on a miss.

### Why both store steps are non-fatal

Temporal has already retried each activity `activity_max_attempts` times before the workflow sees an
error, so what is left is a persistently unreachable store. Losing a cache entry is worth far less
than a completed six-hour calculation: the job still returns, still pushes back, still publishes its
note. A lookup failure costs the recompute we would have done anyway; a persist failure costs the
next identical request. Both log at WARNING — visible, because a silent version of this is precisely
the regression this ADR exists to end.

## Consequences

- An identical QM request is served from Postgres regardless of Temporal retention, which is where
  the real money is.
- A DFT result survives an unmerged PR.
- `crosslink` gains its first writer, so "which notes rest on this calculation" becomes answerable
  in production rather than only in tests.
- `calculation_results` now holds a `dft` calc type alongside the xTB family. `calc_type` has no
  registry — it is a free string — so this needed no central edit.
- The sanitized `calc_version` slug is display-only; the raw pipeline version also rides in the
  parameter hash, so two pipeline versions that sanitize to the same slug still key apart. A
  cosmetic rule can never become a wrong cache hit.
- The reuse path is asserted with a sentinel energy no mock run can produce, so the test cannot pass
  by accident if the lookup regresses.

## Alternatives considered

**Reuse `qm_job_key` as the store key.** Rejected: it is not a valid `calc_refs` reference, and it
folds molecule, method, basis and pipeline into one opaque digest, so the `(calc_type, calc_version)`
index that exists to make results queryable would have nothing useful in it.

**Persist without a lookup.** This was the first implementation and it is only half the fix: it makes
the result durable but never reused, leaving the recompute-after-retention hole exactly as it was.

**Put method and basis in `calc_version`.** They are free-text fields the model authors, and the
reference format forbids whitespace and colons in a version. Sanitizing them risks collapsing two
distinct levels of theory onto one version string. As parameters they are hashed, so they cannot
collide.
