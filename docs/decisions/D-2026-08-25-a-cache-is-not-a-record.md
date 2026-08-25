# D-2026-08-25-a-cache-is-not-a-record — every computed value is published as a queryable scientific record

**Status:** accepted · **Date:** 2026-08-25

## Context

A review of how computed values are stored asked whether *any* calculation this system performs —
single compound, multi-compound, reaction, conformer ensemble — could be found again by its
chemistry. Five stores hold computed values. None of them is a scientific record.

| Store | Migration | Shape |
| --- | --- | --- |
| `calculation_results` | 001·019·024·048 | `key TEXT PRIMARY KEY` → **opaque `result JSONB`** |
| `structures` | 047 | `structure_id` → `structure JSONB` |
| `artifact_blobs` + `calculation_artifacts` | 019 | byte-addressed `BYTEA`, LRU-evictable |
| `job_records` | 023·033·049 | `payload JSONB` + **opaque `result JSONB`** |
| `bo_campaigns` / `bo_suggestions` | 031·037 | `problem` / `candidates` / `observations` JSONB |

Four findings, each measured against the tree rather than inferred.

**1. The rich shapes exist, and are flattened at the storage boundary.** `science/calc/models.py`
already models everything precisely — `ReactionEnergyResult` with a per-species breakdown carrying
role, symmetry number, enthalpy and Gibbs; `ConformerEnsemble` with populations, degeneracies and a
conformational entropy; `InteractionResult` with two monomers and their complex;
`SolventComparisonResult`; `ThermochemistryResult`; `ScanResult`; `ElectronicProperties`;
`SiteReactivityResult`. Every one becomes `result JSONB`.

That is deliberate, and `science/calc/store.py` says so:

> There is deliberately no filter on the result's *value*. The payload is an opaque
> calculator-owned mapping — the store has been calculator-agnostic since D-011, and a
> `total_energy_hartree > x` predicate would put one calculator's schema inside the thing that
> persists all of them.

Right for a store whose job is exact-key lookup. **It is also why that store cannot be the
scientific record**, and why this is a second store rather than a change to the first.

**2. Composites are not persisted at all.** The cache holds *primitives*. After
`D-2026-08-16-the-physics-leaves-the-cache-stays` a composite whose key would name an output is
decomposed rather than cached — `connectors/calc/server/tools.py` states it: *"it has no cache row
of its own and never had one."* So `compute_thermochemistry`, `compute_reaction_energy`,
`compare_solvents`, `predict_logd` and the Boltzmann-weighted ensemble survive only as
`job_records.result` JSONB, and on the in-turn conversational path **nowhere at all** beyond a
TTL-swept trace blob. The shapes a chemist reasons about are the least recorded.

**3. Nothing publishes a computed value outward.** A sweep of every outbound path found the
PR-gate's `git push` (Markdown notes), an *inbound* merge webhook, a manual Phoenix transcript
publisher, OTLP spans, a Prometheus scrape, and internal session push-back. Every outbound `httpx`
client is a *fetch*. `calc_refs` is a citation chain, not an export.

**4. The precedent was already in the tree.** `D-2026-08-04-the-schema-is-a-file` built a generic
warehouse engine whose `Warehouse`/`WarehouseCursor` Protocols are dialect-neutral and, it turns
out, **already write-capable**: `execute(sql, params)` does not care whether the statement reads or
writes. The read-only-ness of that seam lives in its `sql.py`, not in its driver.

## Decision

**A computed value is published, as it is produced, into a canonical scientific record in a
database this system does not own.**

1. **`src/chemclaw/publish/` is a third manifest seam**, beside `connector.yaml` and
   `datasource.yaml` and built to their template. Both existing seams refuse this by rule:
   `cli/validate_connectors.py` bans a `write_`/`submit_`/`update_`-prefixed tool on an endpoint,
   and `ingest/sources/README.md` states that "a source cannot acquire a write path by declaring
   one". A connector produces, a source supplies, a sink **consumes what the system produced**.
2. **The schema is ours and the site creates it.** `schema/result-store/` ships the DDL;
   `make sink-schema` prints it plus a generated registry seed. Nothing here ever holds DDL
   privileges on the store it writes to — the split this repository already makes between
   `postgres_migration_dsn` and `postgres_dsn`, one level out.
3. **A calculation is an event with a subject, and results are typed facts about it.** A spine
   (`calculation`, `subject`, `subject_member`, `condition_set`, `theory_level`), a governed fact
   layer (`property_value`, `calculation_site_value`, `calculation_point_value`, `conformer`,
   `calculation_candidate`, `calculation_flag`), and `calculation_payload` carrying the original
   untouched — which is what makes the projection safe to be wrong.
4. **`property_definition` is the extension point.** Every fact's property is a foreign key into
   it, so a value cannot be written under a name nobody defined. A new calculator ships registry
   rows; the DDL does not move.
5. **A durable outbox, drained by Temporal.** `result_publications` (migration 050) is written in
   the same act that produces the record; `PublishResultsWorkflow` carries it with retries.
   `python -m chemclaw.cli.backfill_publications` covers everything computed before a sink was
   attached.
6. **Three enqueue points**: `cached_compute` on its miss branch (primitives),
   `ConnectorJobWorkflow._finish` (durable composites), and the in-turn composite tools.

## Three constraints that a simpler-looking schema violates

**A solvent dimension is mandatory, and this is measured.** `ALPB_SOLVENTS` accepts `thf` **and**
`tetrahydrofuran`; `hexane`, `n-hexane`, `nhexane`, `n-hexan` **and** `nhexan`; `ch2cl2`,
`dichloromethane`, `dichlormethane` **and** `methylenechloride` — 42 names for 25 solvents — and the
name reaches the calculation key verbatim. `SUGGESTED_SOLVENTS` is a canonical list of 16 whose own
docstring says *"the aliases are what is left out, deliberately"*, and **no alias→canonical mapping
existed anywhere in the tree**. A schema storing the given name answers "every reaction in THF" with
a confident subset and raises nothing. `tests/test_publish_sql.py` fails without the alias table.

**A calculation's identity excludes who asked for it.** `qm_job_key` already states the rule: the
result does not depend on the requester, so identical science shares one key. `calculation` and
`calculation_publication` are therefore two tables — one row of science, N of "who ran it, why".
The first sketch of this schema put the actor on the calculation, where two chemists asking the
same question would have collided on the primary key.

**Portability costs three Postgres habits.** No arrays (lineage is an edge table, and the index it
needs runs in the reverse direction an array cannot serve); no sequences (every key is a content
hash, which also makes redelivery converge and the database re-buildable); no partial or expression
indexes in the portable core.

## What was measured, not argued

- **Species were matched by list position, and that was silently wrong.** `species` and the
  equation's own `reactants`/`products` are two independently produced sequences — a `quick`-level
  run returns no species at all. A two-species breakdown over a three-member equation put
  cyclohexane's free energy on butadiene. Both are plausible numbers in the same units, so nothing
  downstream would have noticed. Matching is now on `(role, molecule)`.
- **The two ensemble shapes carry different halves and neither carries both.** `EnsembleMember`
  (cached) has `energy_hartree` and no population; `Conformer` (returned) has `relative_kcal` and a
  population and no absolute energy. A projector requiring either would make half the ensembles in
  this system unpublishable.
- **A field-coverage check found three real gaps** that reading the models had not: a missing
  descriptor, an exotherm *flag* published without the threshold it was judged against (a
  deployment setting, so the stored boolean would have been uninterpretable after someone changed
  it), and a scan whose coordinate said "dihedral" without saying which atoms.

## Consequences

- The six questions the request named are SQL, and `tests/test_publish_sql.py` asserts each against
  a real database running the shipped DDL — including the negative cases, because without them the
  THF query passes on a partial answer.
- **Publishing is off by default.** `CHEMCLAW_RESULT_SINKS` is empty, and with no sink enabled the
  enqueue costs one list lookup and no database round trip. Discovery is not enablement (D-018);
  a system that began shipping science somewhere on a default would be the failure this seam
  exists to make deliberate.
- `SinkUnavailableError` is a `ConnectionError` and `SinkRejectedError` a `ChemclawError`, which is
  the retry contract rather than a taxonomy preference — the same split `warehouse/driver.py` makes.
- Two drivers ship (SQL and HTTP) plus a psycopg `Warehouse`. That is what keeps the seam from
  being an abstraction with one caller; if only one is ever used, it should be inlined.
- **What is still open**: whether `property_value` needs partitioning, and on what. At ~10 facts per
  calculation and 10⁶ calculations it is 10⁷ rows and a b-tree is fine; choosing a partition key
  before the row count is known would be guessing. The measurement is the growth curve per
  `calc_type` over the first quarter of publishing.

## What was rejected

- **A typed satellite table per result type.** A migration per tool, forever — and the failure is
  asymmetric across deployments: a site that has not run this quarter's migration is missing the
  table the new writer needs, so the new tool writes *nothing*, silently, because there is no row
  to be absent from.
- **Pure EAV.** It cannot hold ordered shapes (a scan profile is only meaningful point-to-point),
  it lets property names drift unchecked, and it collapses the planner's statistics: one `value`
  column spanning `total_energy` (−10³ Ha) and `qed` (0–1) makes every histogram useless.
- **One global SI unit.** Nobody writes `WHERE delta_g < -41840` J/mol. Canonical *per property*:
  absolute energies in hartree because they exist to be differenced, every difference in kcal/mol
  because that is the unit a threshold is stated in.
- **Reusing the warehouse seam's `ConnectionBinding`.** It is Snowflake-shaped — an account, a
  warehouse, a role, and **no host or port** — so a Postgres target would mean abusing `account` as
  a hostname or teaching one model to describe two products. The connection block is validated by
  the driver's own signature instead, the rule the data-source seam already applies to `config:`.
- **Copying artifact bytes outward.** A Hessian is megabytes and `artifact_blobs` is deliberately
  evictable (D-124) because the answer can be regenerated. What crosses is a reference.
- **A pending-publications gauge.** The backlog is
  `chemclaw_results_queued_total - chemclaw_results_published_total`, which is exact and free; a
  gauge would need a `COUNT(*)` on every scrape to say the same thing.

## References

- `docs/decisions/D-011-results-are-persisted-once-never-recomputed.md` — the cache this does not
  replace.
- `docs/decisions/D-2026-08-16-the-physics-leaves-the-cache-stays.md` — why composites have no
  cache row, which is what leaves them unrecorded.
- `docs/decisions/D-2026-08-04-the-schema-is-a-file.md` — the binding seam this borrows its
  credential and late-binding discipline from, and the model it deliberately does not reuse.
- `docs/decisions/D-157-a-durable-record-of-every-connector-job-what-ran.md` — `rationale`, carried onto
  the publication row for the same reason.
- `src/chemclaw/publish/README.md` and `schema/README.md` — what is written, and the shape.
