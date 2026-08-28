# 01 — The contract: what a feeder must produce

**This file is normative.** Every requirement below cites the module that enforces it, so it can be
checked rather than believed. Where a requirement is *not* enforced anywhere, it says so in those
words — those are the ones that fail silently and they are the reason this file exists.

Read it whichever platform you are on. `03-databricks.md` and `04-openshift.md` are the two ways of
satisfying it; neither adds a requirement of its own.

---

## 0. The shape of the contract

A feeder produces **one relation**, and — only if the corpus is large enough to need index-ranked
search — **one vector index** beside it.

```
  <catalog>.<schema>.v_reaction          the landing relation   (§1, §2)
  <catalog>.<schema>.reaction_index      the vector index       (§4)   [index shape only]
```

ChemClaw3 reaches both through **one** file, the source's `datasource.yaml`, whose `binding:` block
names them. Nothing in `chemclaw.ingest.eln.warehouse` names a table or a column
(`ingest/eln/warehouse/README.md`), so the names above are yours; what is fixed is the *structure*
this file describes.

Put your manifest in your own folder and mount it first on `CHEMCLAW_DATA_SOURCES_DIR` (an
OS-pathsep search path, earlier wins). Do not edit the shipped manifests — you would be editing a
file `tests/test_warehouse_binding.py` exercises against a fixture row.

---

## 1. The landing relation — required columns

Two columns are required. Everything else is optional and its absence is silence, not an error.

| Purpose | Binding key | Requirement |
| --- | --- | --- |
| Stable identifier | `key:` | Non-null, unique across the release, **stable between releases for the same reaction**. This is the value that joins the relation, the index and every downstream row. |
| Reaction SMILES | `smiles:` | Non-null, `reactants>agents>products`. The one required value (`CorpusBinding.smiles`). |
| Citation | `citation:` | Non-null. A patent number, a DOI, a document id — something a chemist can follow back. `CorpusBinding.citation` calls it required "because a precedent a chemist cannot follow back is not a precedent"; a `fallback:` path is allowed. |
| Pagination column | `order_by:` | **Unique and stable across the release.** Defaults to `key:`. |

**A row missing the SMILES or the citation is skipped**, counted in `CorpusReport.skipped`, and
never silent (`ingest/labels/corpus.py`). It is not an error and it does not stop the drain — so a
feeder that lands 40% of its rows with a null citation gets 40% fewer precedents and a healthy-looking
job on both sides. Filter them out upstream, or exclude them with `where:` (§1.4).

### 1.1 The reaction SMILES

The corpus shape hands the reaction over **already assembled** — there is no `related:`,
`components:` or `impurities:` block, because there is nothing to reassemble
(`CorpusBinding`'s docstring). The drain splits the SMILES into species itself and records which
slot each came from:

| Slot in `a.b>c>d.e` | Recorded role |
| --- | --- |
| reactants | `reactant` |
| agents | `reagent` |
| products | `product` |

**The agent slot becomes `reagent`, not `solvent`.** The record form groups solvent, catalyst, ligand
and base into one slot, and guessing which is the labeller's job — writing `solvent` here would put a
wrong answer into the column that records what the *source* claimed
(`ingest/labels/corpus.py::_SLOT_ROLES`).

So: **keep the agents in the SMILES.** A feeder that strips them to produce a "clean"
`reactants>>products` is discarding the only signal the corpus carries about what was added.

Every species is canonicalised through `chemclaw.core.chem.standard_smiles` on the ChemClaw3 side, and
`corpus_molecules` is keyed on the standardised form — so the feeder does **not** need to canonicalise,
and two rows whose SMILES differ only in kekulisation converge without help. What the feeder does need
is for the string to *parse*: an unparseable SMILES is a skipped row.

### 1.2 Optional columns the binding already understands

Each is a `FieldBinding` — `{path: root.<COL>, transform: [...]}` — and each is `None` by default.

| Binding key | What it is | Note |
| --- | --- | --- |
| `published_on` | publication/experiment date | `transform: [{iso_date: {}}]` |
| `temperature_c` | °C | `transform: [{number: {}}]`; use `{scale:}` if your column is K or °F |
| `time_h` | hours | `{scale: {factor: 0.0166666667}}` turns minutes into hours |
| `yield_percent` | % | `[{number: {}}, {clamp: {min: 0, max: 100}}]` |
| `workup_text` | free text | |
| `named_reaction` | e.g. `Suzuki coupling` | see §1.3 |
| `reaction_class` | the vendor's class | see §1.3 |
| `rxno_id` | an RXNO ontology id | see §1.3 |
| `mapped_smiles` | atom-mapped reaction SMILES | see §1.3 |

**The transform vocabulary is closed**: `number`, `scale`, `value_map`, `iso_date`, `iso_datetime`,
`regex`, `strip`, `upper`, `lower`, `default`, `clamp`, and nothing else. An unknown name fails when
the binding loads, not when a row reaches it. A binding is configuration; if a transform could reach
arbitrary code, mounting a manifest directory would mean mounting an execution surface. **So do the
unit conversion the vocabulary cannot express in the feeder, not in the binding.**

### 1.3 Labels the corpus already carries — and the one that is never a skip

If your upstream ships a classification for part of the corpus, land it and declare it:

```yaml
labels:
  provides: [named-reaction, atom-mapping]   # the group names; `species-roles` is the third
  override: []
```

`make datasource-validate` checks `provides:` against the `corpus:` block — a source cannot claim a
group it has no column for.

**`provides` is never a skip.** A corpus that classifies most of its rows and not all of them leaves
the rest to be derived, and that is the ordinary case rather than a defect: the `reaction-labels`
Schedule fills every empty one through `Chemclaw3-mcp:servers/rxnlabel`. The claim is read for the
coverage sentence an answer carries and for the `override` subset check, nothing else
(`pistachio/datasource.yaml`). So a feeder should **not** try to complete the classification itself.

### 1.4 `where:` — the corpus's own notion of a usable row

An extra predicate, ANDed with the keyset filter. **It is inserted literally**, so it is as trusted
as the manifest itself — never build it from anything a user supplies.

```yaml
where: "product_smiles IS NOT NULL"
```

Two things it is good for, and one it is essential for:

- excluding rows the drain would only skip anyway (no citation, no product);
- excluding a staging partition the feeder has not finished writing;
- **narrowing the daily re-walk** — see §5, which is the one every feeder needs.

### 1.5 Column casing is exact, and the warehouse decides it

Every path is an **exact-case** lookup on the returned row. A binding saying `REACTION_ID` against a
column the warehouse returns as `reaction_id` resolves to nothing — and a row that resolves to
nothing is skipped rather than reported (`eln-databricks/datasource.yaml`). Copy the casing from
`DESCRIBE TABLE`. A warehouse that upper-cases unquoted identifiers in its result metadata wants the
binding written in capitals for the same reason.

This is the single most common way a correct-looking binding produces an empty corpus.

### 1.6 `fetch_limit` and page size

`fetch_limit:` is rows per pass, `1 ≤ n ≤ 20000`, default 1000. ChemClaw3 also caps it with
`CHEMCLAW_CORPUS_PAGE_SIZE` (default 1000) and takes the lower of the two, so a site can cap the
load on its warehouse without a redeploy of ChemClaw3. One run drains
`CHEMCLAW_CORPUS_SYNC_MAX_ITERATIONS` pages (default 100) before `continue_as_new`.

What that means for a feeder: **the drain reads your relation in pages of ~1000 rows, ordered by
`order_by`, resuming strictly after the last key it saw.** Make that column indexed and cheap to
range-scan. `OFFSET`-style pagination is deliberately not used — over ten million rows it gets
quadratically slower the further in it gets.

---

## 2. What ChemClaw3 does with the relation, and when

Nothing in this section is the feeder's work. It is here so you can tell a feeder fault from a
ChemClaw3 fault.

| Schedule id | Workflow | Default cadence | What it does |
| --- | --- | --- | --- |
| `reaction-corpus` | `ReactionCorpusWorkflow` | `CHEMCLAW_CORPUS_SYNC_SCHEDULE_MINUTES`, **1440** | keyset-walks every active source with a `corpus:` block; writes the *record* phase into `reaction_labels` + `reaction_species`, and the ECFP4/pattern fingerprints into `corpus_molecules` |
| `reaction-labels` | `ReactionLabelWorkflow` | `CHEMCLAW_LABEL_SYNC_SCHEDULE_MINUTES`, **60** | finds rows whose `labeller_version` differs from the current one and fills the atom map, the named reaction and the refined per-species roles |

Both live on the `background-jobs` queue, are applied by `make schedules-apply`, and run under
`ScheduleOverlapPolicy.SKIP` — a run still going when the next fires means the next is skipped, not
queued.

Daily is deliberate for the corpus drain: "a corpus release changes when a vendor ships one, not
continuously, so an hourly re-walk would read a warehouse to learn nothing"
(`core/config/labels.py`). If your feeder lands data every hour, that comment stops being true for
you — raise the cadence and read §5 first, because the two decisions interact.

---

## 3. Embedding parity — the requirement nothing enforces

**This is the section to get right.** Everything else in this file fails loudly. This one fails as a
number that is meaningless and looks fine.

### 3.1 What ChemClaw3 does at query time

On the scan shape with `embedding: local`, and on the index shape always, ChemClaw3 embeds the
*query* through `chemclaw.core.embeddings.embed_texts` and sends the vector. The model that produces
it is fixed by three settings:

| Setting | Default | What it is |
| --- | --- | --- |
| `CHEMCLAW_EMBEDDING_PROVIDER` | `hash` | `hash` (deterministic, for tests) or `openai_compatible` |
| `CHEMCLAW_EMBEDDING_MODEL` | `""` | the model name sent to the `/embeddings` route |
| `CHEMCLAW_EMBEDDING_DIM` | `1536` | must match the model's output width **and** the stored column |

The base URL, credential and TLS are the LLM provider's (`CHEMCLAW_LLM_BASE_URL`).

**A corpus vector produced by any other model is not comparable to that query vector.** The cosine is
still a number in [0, 1]; it is simply about nothing. Hybrid fusion (`retrieval/hybrid.py`) then
ranks on it beside a lexical leg that *is* meaningful, so the failure presents as "retrieval got a
bit worse", not as an outage.

### 3.2 Why nothing catches it

`document_chunks` and `note_index` each carry an `embedding_key` column — `provider:endpoint:dDIM:model`,
written per row (migrations 038/039) — so a model change makes every stored row compare unequal and
`reembed_stale` / `reindex_notes` rebuild it. That is the self-healing this system has.

**A warehouse corpus has no such column and no such sweep.** ChemClaw3 does not know which model built
your vectors and cannot find out. There is exactly one automatic check anywhere on this path: a
dimension mismatch, which the warehouse or the index will reject as a type error. Same width, wrong
model, is invisible.

### 3.3 The three ways to hold parity, best first

**(a) Let the warehouse own the model — scan shape only.** Set `embedding: server` and name the
function:

```yaml
vector:
  relation: v_reaction_embedding
  key: reaction_id
  vector_column: reaction_vector
  content_columns: [reaction_smiles, title]
  metric: cosine
  embedding: server
  server_embed_function: <the warehouse's embedding function>
  server_embed_model: <its model argument, if it takes one>
```

ChemClaw3 then puts the query through the same function that built the column. Parity is true by
construction: there is no second model and nothing to keep in step. **This is unavailable on the
index shape** — the binding refuses `embedding: server` there, because an index-ranked search sends a
vector and there is no statement for a warehouse function to appear in.

**(b) Have the feeder call ChemClaw3's embedding endpoint.** Same `CHEMCLAW_LLM_BASE_URL`, same
`CHEMCLAW_EMBEDDING_MODEL`, same credential. Correct, and it makes the feeder's compute depend on
reaching that gateway — which for a Databricks job usually means it cannot, and stage 3 moves to an
OpenShift `CronJob` instead (`04-openshift.md` §6).

**(c) Pin the model on both sides and check it.** If the feeder embeds with its own copy of the
model, then:

- land a `embedding_model` and `embedding_version` column beside the vector, written by the feeder;
- record the expected values in the deployment's runbook next to `CHEMCLAW_EMBEDDING_MODEL`;
- run the probe in `05-operations.md` §2.4, which is the only thing that will ever tell you they
  diverged.

Whichever you pick, **write it down in the manifest as a comment**. The manifest is the one file both
sides read.

### 3.4 Re-embedding

Changing the embedding model is a corpus-wide re-embed on the feeder's side and a full re-index on
ChemClaw3's — see `05-operations.md` §4. Settle the model before the first backfill; after one real
deployment it is a bill, not a decision.

---

## 4. The vector surface

### 4.1 Scan shape — a vector column

```yaml
vector:
  relation: v_reaction_embedding      # may be the landing relation itself
  key: reaction_id                    # must hold the same values as the corpus `key:`
  vector_column: reaction_vector
  content_columns: [reaction_smiles, title, patent_number]
  metric: cosine
  embedding: local                    # or `server` — see §3.3(a)
  filter_columns: {tag: project_code, since: created_ts, until: created_ts}
  suppress_ingested: false            # `true` only when this source also has an ingest half
```

Requirements:

- **`metric: cosine`.** It is the only metric this repository's Databricks driver serves —
  `vector_cosine_similarity`, which takes `ARRAY<FLOAT>` (not `ARRAY<DOUBLE>`, and not a `VECTOR`
  type, which Databricks does not have). L2 and inner product are refused here rather than by the
  server, because their function names are not verified in this repository.
- **The vector column's width must equal `CHEMCLAW_EMBEDDING_DIM`.**
- **A driver that offers no `vector_dialect` cannot serve a `vector:` block at all** and says so,
  naming itself, rather than emitting SQL another server will reject
  (`ingest/eln/warehouse/driver.py`).
- `suppress_ingested: true` drops a hit whose reaction already became a note. For a corpus that is
  never ingested, leave it `false`: every check would be a wasted probe, and an id colliding with an
  ELN one would silently hide a legitimate hit.

The scan evaluates a similarity function **per row, on every query**. That is right for an ELN and
wrong for a corpus of millions.

### 4.2 Index shape — Mosaic AI Vector Search

```yaml
vector:
  index: <catalog>.<schema>.reaction_index    # three-level Unity Catalog name
  relation: v_reaction                        # the catalogue: resolves winning keys to text
  key: reaction_id
  content_columns: [reaction_smiles, title, patent_number, publication_year]
  metric: cosine
  embedding: local                            # enforced; `server` is refused with `index:`
  filter_columns: {since: publication_date, until: publication_date}
  suppress_ingested: false
```

Declaring both `index:` and `vector_column:` is refused rather than silently resolved.

**The index must declare exactly these three columns**, and the names are constants in
`retrieval/vectors/databricks.py` rather than settings, because the adapter writes and reads them:

| Column | Must hold |
| --- | --- |
| `id` | the primary key — **the same value as the relation's `key:` column** |
| `embedding` | the vector |
| `group_key` | **also the same value as `key:`** — see below |

**`group_key` is not optional and it is not free.** `DatabricksVectorStore.search` always requests
`columns=[id, group_key]`, so an index without that column fails every search. And a filtered search
sends eligibility as `{group_key: [<key values>]}`: `_eligible_keys` computes the eligible set *in
the warehouse* from `filter_columns` and sends it to the index **before** the top-k, because
filtering after the cut returns nothing when the k nearest all belonged to another year. So a
`group_key` holding anything other than the relation's key silently returns zero hits for every
filtered search and full results for every unfiltered one — which reads as "the filter is very
strict" rather than as a fault.

`id` has the same requirement for a different reason: matches are rejoined to the relation by
`by_key[match.id]`, and a key the relation no longer holds is dropped.

**Vectors must be L2-normalised.** The adapter normalises what it upserts and what it queries with,
then inverts Databricks' `1/(1 + d²)` back to a cosine — `cos = 1.5 − 0.5/score` — which is exact
*only* for unit vectors. A feeder writes into the index directly, bypassing `upsert`, so this one is
yours. An un-normalised corpus vector is not a crash: it is a wrong cosine, fused into the ranking.

**A `score_threshold` of 1/3 is pushed server-side** — the store's own units for a cosine of 0 — so a
hit less similar than orthogonal never costs a slot in the top-k.

### 4.3 Direct Vector Access or Delta Sync?

`retrieval/vectors/README.md` requires a **Direct Vector Access** index, and that requirement is about
the *document and note* corpora, which ChemClaw3 upserts and deletes into — a Delta Sync index computes
its own embeddings from a source table and cannot be written to.

**A feeder-owned corpus index is read-only to ChemClaw3**, so both are viable and they differ:

| | Direct Vector Access | Delta Sync, **self-managed embeddings** |
| --- | --- | --- |
| Who writes vectors | the feeder, by upserting the index | the feeder, by writing the Delta table; the index syncs |
| Query vector accepted | yes | yes — *because* embeddings are self-managed |
| Deletes | explicit | follow the table |
| Best when | you already have an upsert path | the feeder is a table-writing pipeline, which it usually is |

**Delta Sync with *Databricks-managed* embeddings is not viable.** That index computes embeddings
from text with its own model, and ChemClaw3 sends a query *vector* built by
`CHEMCLAW_EMBEDDING_MODEL` — §3 all over again, with the additional problem that the two models are
now chosen by two different teams.

One further constraint: `CHEMCLAW_VECTOR_STORE_PROVIDER` and `CHEMCLAW_VECTOR_STORE_ENDPOINT_NAME`
are deployment-wide, not per-source. If the deployment also keeps its document and note vectors in
Databricks, those indexes must be Direct Access and live on the same endpoint. The corpus index may
still be Delta Sync — the endpoint serves both kinds.

### 4.4 Filters and the scope cap

A filtered search resolves eligibility to a set of keys and sends it to the index. That set is
bounded by `CHEMCLAW_VECTOR_STORE_MAX_SCOPE_KEYS` (default 10 000) and is **refused rather than
truncated** when it overflows — a truncated eligibility set is a wrong answer with no marker.

The consequence for a feeder is a design one: `filter_columns` should map onto columns that *narrow*.
`{since: publication_date}` over a ten-million-row corpus selects far more than 10 000 keys for any
useful window, and the search is refused with a message naming the setting. Options, in order:

1. map `since`/`until` onto a column with real selectivity for the questions being asked;
2. move the broad restriction into the binding's `where:`, which is applied to the resolve query and
   not counted as scope;
3. raise the cap, if the index can take a filter payload that size.

---

## 5. Freshness: the re-walk, and why the feeder controls its cost

**The corpus drain keeps no `sync_cursors` row.** Its cursor is intra-run only, riding the workflow
state (`durable/corpus_sync.py` says so in its own words; `docs/planning/BACKLOG.md` records two ADRs
that claimed otherwise as falsified). Re-draining an unchanged release is a no-op — every write is an
id-keyed upsert — and a *new* release must be walked from the top, which is why there is no watermark.

So a daily `reaction-corpus` Schedule re-reads the whole relation, every day. At ELN scale that is
free. At ten million rows it is a full scan per day for whatever the feeder added.

**The fix is the feeder's, and it is one column.** Land a `load_date` (or a partition, or a monotone
`ingested_at`) that the feeder sets on every row it writes or corrects, and narrow the binding:

```yaml
corpus:
  relation: v_reaction
  key: reaction_id
  order_by: reaction_id
  where: "product_smiles IS NOT NULL AND load_date >= current_date() - INTERVAL 7 DAYS"
```

Three properties this gives you, all of them consequences of the upsert being idempotent:

- **A row that falls out of the window is not lost.** It was already written; nothing removes it.
- **A correction is re-presented by bumping `load_date`.** That is the only mechanism there is — a
  corrected row that keeps its old `load_date` will never be re-read.
- **A backfill is the same query with the window removed.** Widen `where:`, run the workflow by hand,
  put the window back. No code path differs.

Pick the window from the feeder's own failure budget: it must be longer than the longest run of
consecutive failed feeder days you are willing to recover from without an operator. Seven days is a
reasonable default; one day is not, because one missed run then loses data permanently.

The `where:` string is literal, so `current_date() - INTERVAL 7 DAYS` must be valid in *your*
warehouse's SQL. It is on Databricks. If your driver's dialect has no such expression, put the window
in a view and point `relation:` at the view.

---

## 6. Credentials

**Named, never carried.** Any `connection:` key ending `_env` holds the *name* of an environment
variable, read at connect time — so a rotated token is picked up by the next connection and the
manifest is safe to keep in a repository.

```yaml
connection:
  driver: chemclaw.ingest.eln.warehouse.databricks:DatabricksWarehouse
  server_hostname_env: DATABRICKS_HOST
  access_token_env: PISTACHIO_DATABRICKS_TOKEN
  warehouse_id: 0123456789abcdef
  catalog: pistachio
  schema: public
  query_timeout_seconds: 60
```

The names are deliberately **not** `CHEMCLAW_`-prefixed: they are the warehouse client's own
credentials, not settings of this application.

**One credential per corpus.** A licensed corpus has different grants from the ELN beside it, and one
credential per corpus is what makes revoking one of them possible.

**The feeder's credentials are not these.** The feeder needs write grants; ChemClaw3's principal
needs `SELECT` on the relation (and read on the index) and nothing more. Two principals, two secrets,
and the ChemClaw3 one must not be able to write the corpus it reads.

Everything under `connection:` except `driver:` is a keyword argument of the callable `driver:` names
— that database's own vocabulary, checked against the callable's signature offline by
`make datasource-validate`. Attaching a database this repository ships no driver for is one module
exposing a `Warehouse` (three methods, see `driver.py`) plus a manifest naming it, and no edit
anywhere in the package.

---

## 7. The checklist

Offline, on the ChemClaw3 side — no connection, no credentials:

```sh
make datasource-validate              # the manifest resolves; the connection block binds
uv run python -m chemclaw.cli.validate_datasources --construct   # ... and both halves actually build, so the binding is real
```

`--construct` is opt-in and the `make` target does not forward it — `make datasource-validate
--construct` is parsed by `make` as an unknown option, not by the validator. Run the module directly,
as above. It is worth running: binding a `config:` block checks its keyword *names*; only building
the half validates the binding itself.

Against the live warehouse, in order, and each one distinguishes a different fault —
`05-operations.md` §2 has the SQL:

- [ ] the relation returns rows for the binding's `where:` (a wrong `where:` and an empty feed differ)
- [ ] every `key:` is non-null and unique
- [ ] every path in the binding resolves on a sample row — **exact case** (§1.5)
- [ ] the reaction SMILES parses and keeps its agents (§1.1)
- [ ] the citation column is non-null for the rows you expect to be precedents
- [ ] the vector column's width equals `CHEMCLAW_EMBEDDING_DIM` (§3)
- [ ] the model that built the vectors is the one §3.3 says it should be — **nothing checks this**
- [ ] index shape only: `id`, `embedding`, `group_key` all present; `group_key == key` (§4.2)
- [ ] index shape only: vectors are unit length (§4.2)
- [ ] `order_by:` is unique, stable and indexed (§1)
- [ ] the freshness window in `where:` is wider than the feeder's failure budget (§5)

## 8. What the contract deliberately does *not* ask for

Building any of these is building something nothing reads:

- **Molecule fingerprints.** `corpus_molecules` — ECFP4 bits plus the RDKit pattern screen — is
  written by ChemClaw3's drain from the `smiles:` column, deduplicated by standardised structure.
- **Atom mapping, named reactions, refined species roles.** Derived by the `reaction-labels` Schedule
  through `Chemclaw3-mcp:servers/rxnlabel`. Land them if your upstream ships them (§1.3); do not
  compute them.
- **Knowledge-graph notes.** A corpus is *evidence*, not this site's knowledge: a patent reaction did
  not happen in this lab and nobody here can vouch for it
  (`D-2026-08-25-a-corpus-is-evidence-not-an-eln`). A source cannot acquire a write path to the graph
  by declaring one.
- **Anything called from ChemClaw3.** A feeder has no manifest here, no name in any settings list and
  no row in any registry (`D-2026-08-28-a-feeder-writes-a-table-and-nothing-else`).
