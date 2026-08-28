# Feeder pipelines — keeping a corpus fresh from outside ChemClaw3

**Status:** the ChemClaw3 side is implemented and exercised offline; the feeder side is what this
directory specifies and no site has built one yet.
**Decision:** `docs/decisions/D-2026-08-28-a-feeder-writes-a-table-and-nothing-else.md`.
**Audience:** the data engineer who will build the pipeline, and the ChemClaw3 operator who will
attach it. Neither needs to read the other's half, except `01-contract.md`, which both do.

---

## What a feeder is

A **feeder** is a recurring job — daily, typically — that pulls reactions from somewhere upstream and
lands them in a database ChemClaw3 reads. Concretely, and this is the whole of it:

> new reaction SMILES + a stable reaction identifier + whatever metadata came with them + one
> embedding vector per reaction, written into a relation the site owns.

It runs **outside ChemClaw3**: a Databricks job, an OpenShift `CronJob`, the site's existing Airflow.
It imports nothing from this repository, calls no ChemClaw3 route, and holds no ChemClaw3 credential.
ChemClaw3 never learns it exists — it reads a relation, and a relation that was refreshed an hour ago
and one that was refreshed last quarter are the same relation.

## Why it is not in ChemClaw3

Three rules already in the tree, each written for a different reason, and each on its own sufficient:

| Rule | Where | What it forbids |
| --- | --- | --- |
| No request-time egress, ever, in any environment | `Chemclaw3-mcp/CLAUDE.md`, enforced at four layers | a fetcher in the MCP tool fleet |
| No runtime dependency on a third-party service (D-089) | `tests/test_no_egress.py` sanctions the ELN sources *because they are the deployment's own systems* | a nightly pull from a vendor URL inside a connector |
| Durability is Temporal's, and that queue is this system's (D-002) | `durable/`, `background-jobs` | a vendor's 502 presenting as an agent outage |

There is a plain operational argument too. A ten-million-row backfill is a compute-shaped,
restartable, hours-to-days job that wants a cluster which scales up and then goes away. The
`background-jobs` worker is a long-lived pod sized for light periodic work, and two recent ADRs are
about keeping that queue from wedging.

## The division of labour

```
  ┌──────────────────── outside ChemClaw3 (this directory) ───────────────────┐
  │                                                                           │
  │  1. ACQUIRE   pull the release / the delta from upstream, by URL          │
  │  2. NORMALISE reaction_id + reaction SMILES + metadata -> a typed table   │
  │  3. EMBED     one vector per reaction, with the *agreed* model            │
  │  4. PUBLISH   write the landing relation (+ the vector index)            │
  │                                                                           │
  └───────────────────────────────┬───────────────────────────────────────────┘
                                  │  the contract is this relation
  ┌───────────────────────────────▼───────────────────────────────────────────┐
  │                       inside ChemClaw3 (already built)                     │
  │                                                                           │
  │  datasource.yaml  a binding naming that relation      (D-120, zero code)  │
  │  reaction-corpus  Schedule, daily: keyset drain  ->  reaction_labels,     │
  │                   reaction_species, corpus_molecules (ECFP4 + pattern)    │
  │  reaction-labels  Schedule, hourly: atom map, named reaction, roles       │
  │                   via Chemclaw3-mcp:servers/rxnlabel                      │
  │  vector:          similarity search, run where the vectors live           │
  └───────────────────────────────────────────────────────────────────────────┘
```

**Molecule fingerprints are not the feeder's job.** `corpus_molecules` — ECFP4 bits plus the RDKit
pattern screen that makes substructure search sound — is written by ChemClaw3's own drain, from the
`smiles:` column, deduplicated by standardised structure. A feeder that computed molecule
fingerprints would be computing something nothing reads. The only vectors ChemClaw3 takes from
outside are the **per-reaction dense embeddings** the `vector:` block searches.

## The files

| File | What it answers |
| --- | --- |
| [`01-contract.md`](01-contract.md) | **Normative.** Exactly what the landing relation and the index must look like, and which module enforces each requirement. Read this one whichever platform you are on. |
| [`02-acquisition.md`](02-acquisition.md) | Pulling from an upstream database over a URL: watermarks, resume, checksums, licence, canonicalisation, dedup — and what makes a daily run idempotent. |
| [`03-databricks.md`](03-databricks.md) | The Databricks implementation, end to end: bundle, DDL, `MERGE`, the embed task, the Vector Search index, the schedule, sizing, secrets. |
| [`04-openshift.md`](04-openshift.md) | The OpenShift implementation, end to end: `CronJob`, `Secret`, `NetworkPolicy`, RBAC, concurrency, resources — plus the Postgres-target variant for sites with no lakehouse. |
| [`05-operations.md`](05-operations.md) | Bring-up order, the verification probes, every failure mode as it looks from ChemClaw3's side, re-embedding after a model change, cost and scale. |

## Which shape do you need? — decide this before reading further

Two questions, and they are independent.

**1. How big is the corpus?** This picks the search shape, and the binding enforces the choice —
declaring both is refused rather than silently resolved.

| | `vector_column:` — scan | `index:` — Mosaic AI Vector Search |
| --- | --- | --- |
| How it ranks | a similarity function per row, every query | in the index; the relation resolves winning keys to text |
| Right up to | ~10⁶ rows (an ELN) | 10⁷+ (a patent corpus) |
| Query embedded | here, or **in the warehouse** (`embedding: server`) | always here — the binding refuses `server` |
| Feeder writes | a vector column in a table | a table **and** an index over it |
| Worked example | `ingest/sources/eln-databricks/datasource.yaml` | `ingest/sources/pistachio/datasource.yaml` |

**2. Where does the embedding model live?** This is the question that decides where stage 3 runs, and
it is the one most likely to be got wrong — see `01-contract.md` §3.

- If the warehouse owns the model (a Databricks embedding function), use the **scan** shape with
  `embedding: server`. Parity is then true by construction and there is nothing to keep in step.
- If ChemClaw3's internal LLM gateway owns it, the feeder must call **that endpoint, that model** —
  which means the feeder's compute has to reach it. If a Databricks job cannot, run stage 3 as an
  OpenShift `CronJob` beside ChemClaw3 and have it write back (`04-openshift.md` §6).

## The 60-second version

1. Pick the shape above.
2. Build the landing relation to `01-contract.md`.
3. Copy `ingest/sources/pistachio/datasource.yaml` into your own manifest folder, replace every name
   in it with your relation's, mount the folder first on `CHEMCLAW_DATA_SOURCES_DIR`, and add the
   source's name to `CHEMCLAW_DATA_SOURCES`.
4. `make datasource-validate`, then `uv run python -m chemclaw.cli.validate_datasources
   --construct` — both offline, no connection.
5. Backfill once by hand, then let the feeder's schedule and ChemClaw3's `reaction-corpus` Schedule
   (daily) and `reaction-labels` Schedule (hourly) take over.
6. Verify with the probes in `05-operations.md` §2. A corpus that is landing but not draining, and a
   corpus that is draining but never labelled, look identical from a dashboard and are different
   faults.
