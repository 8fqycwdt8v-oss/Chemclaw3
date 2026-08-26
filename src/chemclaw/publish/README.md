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
arrays.

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

**A Hessian.** `xtb.hess` is a `calc_type` the calculation server stamps and this package has no
prefix for. The scientific value of a Hessian is realised in `ThermochemistryResult` — frequencies,
ZPE, the RRHO corrections — and *that* has a projector with no hook: it is a tool composite, so it
is neither written to the calculation cache (composites are not cached, D-011) nor returned by a
job envelope. Publishing frequencies therefore needs a third hook rather than a projector, which is
a decision to take rather than a line to add. Tracked in `docs/planning/BACKLOG.md`;
`tests/test_publish_reaches_the_hooks.py::_PRIMITIVES_NOT_PUBLISHED` names it so the gap is
declared rather than merely absent.

## Attaching a store

The schema is **shipped here and created by the site**; nothing in this system holds DDL privileges
on the database it publishes to.

```
python -m chemclaw.cli.sink_schema --all        # the DDL and the registry seed, to apply
export CHEMCLAW_RESULT_SINKS=postgres           # enable a discovered sink
python -m chemclaw.cli.backfill_publications    # queue everything computed before now
```

`schema/README.md` covers the schema itself and how it is versioned.
