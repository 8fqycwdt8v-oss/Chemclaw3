# `publish/` — computed results, sent outward as a scientific record

**What this package is for.** Every calculation this system performs is already persisted — but as
a *cache entry*. `calculation_results` is `key TEXT PRIMARY KEY` onto an opaque `result JSONB`, and
that opacity is deliberate: `CalculationQuery`'s own docstring refuses any predicate on the payload,
because "a `total_energy_hartree > x` predicate would put one calculator's schema inside the thing
that persists all of them". Right for a store whose job is exact-key lookup, and exactly why it
cannot also be the scientific record.

This package is the other shape: the same science, projected into a typed, queryable record and
delivered to a database outside this system. It answers questions the cache cannot — *every reaction
with ΔG below −10 kcal/mol run in THF at GFN2*, *every ensemble with more than five populated
conformers*, *everything that rests on this calculation* — and it does so for single compounds,
multi-compound systems, reactions and conformer ensembles alike.

## The modules

| Module | What it holds |
| --- | --- |
| `record.py` | The canonical shape: `ResultRecord`, its subject, conditions, and the six kinds of fact. The whole cross-system contract. |
| `properties.py` | The property registry — every quantity that may be published, with its canonical unit. **The extension point**: a new calculator adds rows here and the schema does not move. |
| `solvents.py` | Canonical solvent identity. Exists because the calculation layer accepts 42 names for 25 solvents and nothing else maps between them. |
| `project.py` | The one module that knows both vocabularies: a calculator's result model in, a `ResultRecord` out. |
| `dialect.py` | A record's rows, per table, and the upserts that write them. |
| `outbox.py` | The durable queue between a finished calculation and its destination. |
| `hooks.py` | The third publish hook: a **tool** composite, which no cache row and no job envelope can reach. |
| `manifest.py` / `registry.py` | The `sink.yaml` seam: discovery, enablement, late-bound driver construction. |
| `connect.py` | Building a driver's connection, with credentials named rather than carried. |
| `driver.py` | The `ResultSink` Protocol. Imports nothing. |
| `drivers/` | The two shipped implementations — SQL and HTTP — plus a psycopg `Warehouse`. |
| `sinks/` | Shipped `sink.yaml` manifests. Discovered; **enabled by nobody unless `CHEMCLAW_RESULT_SINKS` names them.** |

## Three decisions worth knowing before changing anything here

**A subject is one shape, not five.** A molecule, a geometry, an ensemble, a reaction and a complex
are all "an identity plus 1..N members with roles". `subject_id` is a content hash that deliberately
**excludes** solvent, temperature and method — which is what turns "compare this reaction across
every solvent we ran it in" into a `GROUP BY` on one column instead of a fuzzy join over two text
arrays. It identifies each member by that member's **own SMILES**, and falls back to `compound_id`
only for a member that carries none: `compound_id` hashes the *standardized* structure, so it
cannot tell a tautomer from its partner or an acid from its conjugate base, which is exactly the
distinction every species distribution turns on (`Subject.subject_id` carries the measurement).

**A calculation's identity excludes who asked for it.** Two chemists running the same calculation
share one `calc_ref` and produce two publication rows. Putting the actor on the calculation would
make identical science collide.

**Nothing here may fail a calculation.** Every entry point runs *after* the science is durable, so
enqueue is best-effort by construction and a destination being unreachable is counted and logged,
never raised. `SinkUnavailableError` is a `ConnectionError` (retryable) while `SinkRejectedError` is
a `ChemclawError` (not) — that split is the retry contract, not a taxonomy preference.

## What deliberately does not publish

Two things reach a publish hook and are dropped on purpose. Both are written down here because the
alternative — a hook that stays silent — is indistinguishable from the defect this package spent two
changes fixing.

**A BO campaign.** `connectors/bo/workflows.py` stamps `payload_kind="CampaignResult"`, and no
projector reads it. That is the decision, not an omission: a `CampaignResult` is a `best` and a
`history` of `Observation`s — a parameter set and an objective value — and it has no molecular
subject at all. Every `subject.kind` this schema accepts is structural (`molecule`, `geometry`,
`ensemble`, `reaction`, `complex`, `system`), and the parameters a campaign optimizes are as often
a temperature or a catalyst loading as a compound. A campaign's record is `bo_campaigns` and
`bo_suggestions`, which is where its history already lives and where a sequence *is* the record.
The `payload_kind` stays because it is a true statement about the payload and the backfill reads
it; if a campaign ever earns a projector, the routing is already correct.

**A Hessian's matrix.** The `xtb.hess` row itself publishes
(`D-2026-08-27-a-composite-needs-a-hook-not-a-projector`) — its electronic energy, its atom count,
and the `max_gradient` that says whether the geometry differentiated was a stationary point at all.
What is dropped is the packed `.npy` arrays: 1.4 MB of base64 at 120 atoms, in a queue nobody
prunes, already content-addressed in the artifact store, and re-projecting them could not produce a
frequency in any case — a wavenumber is an eigenvalue of the *mass-weighted* matrix and the row
carries no elements. The frequencies reach a store through the third hook instead, as part of
`ThermochemistryResult`.

## The three hooks

Every record reaches the outbox through exactly one of them, and which one is a property of the
result rather than of the calculator that produced it:

| Hook | Where | What it can see |
| --- | --- | --- |
| the cache | `science/calc/store.py::publish_stored_result` | a **primitive**, on a cache miss, routed by the `calc_type` the calculation server stamped |
| the job | `ConnectorJobWorkflow._publish_result`, in `durable/connector_job.py` | a **job composite**, off the envelope a finished Temporal job returns, routed by the `payload_kind` it carries |
| the tool | `connectors/server.py::_publish_tool_results` -> `publish/hooks.py` | a **tool composite** — assembled in one turn, so it has no cache row (its key would name its own output) and no envelope |

The third is installed once, on the boundary every tool result already crosses, so a new tool has
nothing to remember. What it publishes is `TOOL_COMPOSITES`, and
`tests/test_publish_reaches_the_hooks.py::test_every_projector_is_claimed_by_exactly_one_hook`
derives that set rather than trusting it: a projector reachable from no `calc_type` prefix and no
job envelope member is a tool composite by definition, and one that is not declared fails the
suite.

## Attaching a store

The schema is **shipped here and created by the site**; nothing in this system holds DDL privileges
on the database it publishes to.

```
python -m chemclaw.cli.sink_schema --all        # the DDL and the registry seed, to apply
export CHEMCLAW_RESULT_SINKS=postgres           # enable a discovered sink
python -m chemclaw.cli.backfill_publications    # queue everything computed before now
```

`schema/README.md` covers the schema itself and how it is versioned.
