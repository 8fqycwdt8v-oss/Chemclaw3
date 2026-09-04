# 05 — Operating a feeder

Bring-up, the probes, the failure modes, and the two expensive operations. Platform-neutral: where a
step differs, `03-databricks.md` and `04-openshift.md` say how.

---

## 1. Bring-up, in order

Each step is verifiable on its own, and doing them out of order produces a symptom that points at the
wrong step.

**1. Settle the embedding model.** Before anything is embedded. `01-contract.md` §3 — and after the
first real backfill this is a bill rather than a decision.

**2. Build the relation empty, and validate the manifest against it offline.**

```sh
make datasource-validate              # the manifest resolves; the connection block binds
uv run python -m chemclaw.cli.validate_datasources --construct   # ... and both halves build, so the binding itself is checked
```

Both run with no connection and no credentials. A binding that fails here would have failed later as
silence.

**3. Land one page by hand.** A few hundred rows, through the real stages 1–4. Then run §2.1–2.3.
Fixing the binding's column casing now costs a query; fixing it after a ten-million-row backfill
costs the backfill.

**4. Enable the source in ChemClaw3, with the drain window wide open.**

```sh
export CHEMCLAW_DATA_SOURCES_DIR=/etc/chemclaw/sources:/app/src/chemclaw/ingest/sources
export CHEMCLAW_DATA_SOURCES=<source>,<whatever else was enabled>
make schedules-apply
```

**5. Trigger `reaction-corpus` by hand and watch it drain.** Do not wait for the daily fire — you want
to be looking when it runs. §2.3 is what to look at.

**6. Let `reaction-labels` catch up.** Hourly by default; it finds work by asking, so a fresh corpus
produces work through the same `WHERE labeller_version <> current` clause a re-recorded reaction and an
upgraded labeller do. Nobody has to remember anything.

**7. Backfill.** Widen the feeder's window to the whole corpus and let the drain walk it — pages of
~1000 rows, 100 pages per run before `continue_as_new`. Ten million rows is ~10 000 pages: it takes
what it takes, and it is resumable throughout.

**8. Narrow the window and hand over to the schedules.** Put the `where:` window back
(`01-contract.md` §5). This is the step that gets forgotten, and forgetting it means a full-corpus
re-walk every single day.

## 2. The probes

Each answers a different question. Run them in order — the first failure tells you where to stop.

### 2.1 Is the relation what the binding thinks it is?

```sql
-- rows the drain will actually see, under the binding's own where:
SELECT count(*) AS visible FROM v_reaction
WHERE product_smiles IS NOT NULL AND load_date >= current_date() - INTERVAL 7 DAYS;

-- the two required fields (01 §1). Both of these become CorpusReport.skipped.
SELECT count(*) AS no_smiles   FROM v_reaction WHERE reaction_smiles IS NULL;
SELECT count(*) AS no_citation FROM v_reaction WHERE patent_number IS NULL AND document_id IS NULL;

-- the key: non-null, unique, and what order_by paginates on
SELECT count(*) AS rows, count(DISTINCT reaction_id) AS keys FROM v_reaction;
```

`visible` zero with `rows` non-zero is the classic first failure and it is almost always the `where:`
window or the column casing — not the feed.

### 2.2 Is the vector surface right?

```sql
-- width must equal CHEMCLAW_EMBEDDING_DIM
SELECT size(reaction_vector) AS dim, count(*) FROM v_reaction
WHERE reaction_vector IS NOT NULL GROUP BY 1;

-- rows landed but never embedded — the gap between stage 2 and stage 3
SELECT count(*) FROM v_reaction WHERE reaction_vector IS NULL;
```

Index shape, additionally — these three are `01-contract.md` §4.2 and each fails differently:

```sql
-- the index source must expose exactly id, embedding, group_key ...
DESCRIBE <catalog>.<schema>.v_reaction_index_source;

-- ... and group_key must equal the key. Anything else returns zero hits for every FILTERED search
-- and full results for every unfiltered one, which reads as "the filter is strict".
SELECT count(*) AS mismatched FROM v_reaction_index_source WHERE group_key <> id;

-- vectors must be unit length, or the cosine conversion is silently wrong
SELECT count(*) AS not_unit FROM v_reaction
WHERE reaction_vector IS NOT NULL
  AND abs(sqrt(aggregate(reaction_vector, CAST(0 AS DOUBLE), (a, x) -> a + x * x)) - 1.0) > 1e-3;
```

All three should be zero.

### 2.3 Is ChemClaw3 draining it?

```sh
curl -s $CHEMCLAW_URL/schedules | jq '.[] | select(.schedule_id | IN("reaction-corpus","reaction-labels"))'
```

`ScheduleHealth` gives `last_run`, `last_outcome`, `runs_total`, `skipped_overlap`, `running_now`
and `paused`. **`last_outcome` is the first thing to read**: it is Temporal's own status for the
newest run that finished (`COMPLETED`, `FAILED`, `TIMED_OUT`, …), and without it a drain whose every
run is killed by `schedule_run_timeout_seconds` reports exactly the numbers a healthy one does.
**`skipped_overlap` is the number to watch**: it counts fires dropped because the previous run was
still going, and a steadily climbing value is the early warning that the corpus has outgrown its
cadence. That is the signal that says "narrow the window" before anyone notices missing precedents.

Then, in ChemClaw3's own Postgres:

```sql
-- the record phase the corpus drain writes, and the labelling backlog behind it. `source` is the
-- data-source name, so these are scoped to your feeder rather than to every corpus at once.
SELECT count(*) FILTER (WHERE labeller_version IS NOT NULL) AS labelled,
       count(*) FILTER (WHERE labeller_version IS NULL)     AS backlog,
       count(*)                                             AS rows
FROM reaction_labels WHERE source = '<source>';

SELECT count(*) FROM reaction_species  WHERE source = '<source>';  -- species split out of the SMILES
SELECT count(*) FROM corpus_molecules;                             -- ECFP4 + pattern bits, by structure
```

(`corpus_molecules` is keyed on the standardised SMILES and shared across corpora — it is
deliberately not per-source, because two corpora containing the same molecule are one row.)

`reaction_labels` growing while the last column stays high means the corpus is landing and the
*labeller* is the bottleneck — a different fault, in a different pod, from a corpus that is not
landing at all.

**Metrics.** `chemclaw_ingest_records_total{source,outcome}` is emitted by every ingest pass — the
ELN sync, the document sync, the labelling pass (which reports itself as `source="labels"`, its own
stage) and the corpus drain, which books against the **data source** it drained rather than against
the pass, so a deployment reading several corpora gets one series each.

The drain books two outcomes, and the missing third is deliberate: `ingested` for a row that
became a `corpus_reactions` row, `rejected` for one dropped for want of a usable SMILES or a
citation. There is no `skipped` series, because in this system's ingest vocabulary `skipped` means
*deliberately passed over* — unchanged, oversized, an unsupported extension — and the corpus drain
has no such population. A permanently-zero series would assert one exists. So
`ingested + rejected` is the whole of what the pass read, and `rejected` climbing is the number
that says a feeder regressed.

An idle source books **zeros** rather than nothing, so a silent series means the drain did not run
— which is a different fault from a drain that ran and found nothing, and the two are worth telling
apart on a dashboard.

What a record counter still cannot tell you is a feed whose source has *stopped exporting* from one
with nothing new: both produce an empty page and both book `ingested=0, rejected=0`. That
discriminator is the age of `corpus_cursors.updated_at` — a staleness gauge, which does not exist
yet and is its own `BACKLOG.md` row.

### 2.4 Is the embedding the right one? — the probe nothing else does

There is no automatic check (`01-contract.md` §3.2). Two things to run, both cheap:

```sql
-- what actually built the stored vectors, if the feeder records it (01 §3.3(c))
SELECT embedding_model, count(*) FROM v_reaction GROUP BY 1;
```

More than one row here means part of the corpus is not comparable with the rest, and the ranking is
quietly wrong for whichever part is the minority.

Then the end-to-end sanity check, which is the one that catches a model swap the column would not:
**pick a reaction that is in the corpus, ask for it by its own text, and confirm it comes back first.**
A model mismatch does not fail; it returns plausible-looking neighbours that are not the reaction you
asked for. If the top hit for a corpus reaction's own SMILES is not that reaction, stop and check §3.

## 3. Failure modes

Symptom-first, because that is the order you meet them in. The middle column is the one that saves
time: it says which side of the seam to look at.

| Symptom | Side | Cause and fix |
| --- | --- | --- |
| `visible` = 0, `rows` > 0 | ChemClaw3 | the `where:` window, or column casing (`01-contract.md` §1.5). Run §2.1. |
| Every field empty but the SMILES | ChemClaw3 | binding paths resolve to nothing — exact-case lookups. "A path that does not resolve is silence, not an error." |
| `CorpusReport.skipped` high | feeder | rows with no citation or an unparseable SMILES. Quarantine them upstream (`02-acquisition.md` §2) instead of letting the drain count them. |
| `reaction_labels` grows, `labeller_version` stays null | ChemClaw3 | the labelling drain, not the corpus. Check `Chemclaw3-mcp:servers/rxnlabel` reachability and the `reaction-labels` Schedule. |
| `skipped_overlap` climbing on `reaction-corpus` | both | the corpus outgrew its cadence. Narrow the `where:` window first (`01-contract.md` §5); raise the cadence only after. |
| Search returns nothing for any *filtered* query, everything for unfiltered | ChemClaw3 config | index shape, `group_key <> key` (`01-contract.md` §4.2). Run §2.2. |
| Search refused, naming `CHEMCLAW_VECTOR_STORE_MAX_SCOPE_KEYS` | ChemClaw3 config | the filter selects more eligible keys than the payload can carry. It is refused rather than truncated on purpose — a truncated eligibility set is a wrong answer with no marker. `01-contract.md` §4.4. |
| Ranking is subtly bad; nothing errors | **feeder** | the embedding-parity failure. §2.4, then `01-contract.md` §3. |
| Ranking is bad **only on Databricks** | feeder | un-normalised vectors: the cosine conversion assumes unit length on both sides (`01-contract.md` §4.2). |
| Corpus stopped growing; nothing is red | feeder | the CronJob or the Databricks schedule stopped. This produces *nothing* — no failed run, no metric — which is why the alert is on the absence of a success (`04-openshift.md` §8). |
| A field went empty across the whole corpus | feeder | upstream renamed a column. The run keeps succeeding. `02-acquisition.md` §7. |
| Duplicate precedents under two ids | feeder | the identifier recipe changed. `02-acquisition.md` §3 — this one is not repairable by re-running. |
| Drive/query timeouts on the drain | either | the page is too big or the relation is not indexed on `order_by`. Lower `fetch_limit` or `CHEMCLAW_CORPUS_PAGE_SIZE`; index the column. |

## 4. The two expensive operations

### 4.1 Re-embedding after a model change

Changing `CHEMCLAW_EMBEDDING_MODEL` invalidates **every** corpus vector. Nothing detects it
(`01-contract.md` §3.2), so it is entirely a planned operation:

1. Change the model on the ChemClaw3 side and the feeder side **in the same change**, and write the
   new expected value down beside `CHEMCLAW_EMBEDDING_MODEL`.
2. Null the vector column, or write into a new column and swap. A new column is better: the corpus
   stays searchable — with stale-but-consistent vectors — for the length of the re-embed.
3. Re-run stage 3 over the whole corpus. This is the bill.
4. Index shape: resync or rebuild the index.
5. Run §2.4.

ChemClaw3's own corpora self-heal here (`document_chunks` and `note_index` carry `embedding_key`, so
`make reindex` rebuilds what went stale). A warehouse corpus does not — that asymmetry is the whole
of why this is a written procedure rather than a command.

### 4.2 Re-walking the whole corpus

Needed after a binding change (a field added, a mapping corrected), because the drain only re-reads
what the `where:` window admits:

1. Widen the window to the whole relation.
2. Trigger `reaction-corpus` by hand and let it drain. Every write is an id-keyed upsert, so this is
   safe at any time and resumable throughout.
3. Narrow the window back — the step that gets forgotten.

The alternative, bumping `load_date` on every row, achieves the same thing from the feeder's side and
costs a full rewrite of the table. Prefer the window.

## 5. Cost and scale

| | ~10⁵ reactions | ~10⁷ reactions |
| --- | --- | --- |
| Search shape | `vector_column:` scan | `index:` (a per-row similarity is a full scan per question) |
| Query embedding | `embedding: server` if the warehouse serves the model — parity for free | always local; parity is the feeder's problem |
| Daily drain | the whole relation is fine | the `load_date` window is not optional |
| Backfill | one run | ~10 000 pages, resumable, and the reason `continue_as_new` exists |
| Molecule fingerprints | ChemClaw3's, from the SMILES — not the feeder's | same |

The knobs, and what each one actually bounds:

| Setting | Default | Bounds |
| --- | --- | --- |
| `CHEMCLAW_CORPUS_PAGE_SIZE` | 1000 | rows per warehouse query (the binding's `fetch_limit` caps it lower) |
| `CHEMCLAW_CORPUS_SYNC_MAX_ITERATIONS` | 100 | pages per workflow run before `continue_as_new` |
| `CHEMCLAW_CORPUS_SYNC_TIMEOUT_SECONDS` | 900 | one page: a query plus a fingerprint per distinct structure |
| `CHEMCLAW_CORPUS_SYNC_SCHEDULE_MINUTES` | 1440 | how often the whole walk starts again |
| `CHEMCLAW_LABEL_SYNC_SCHEDULE_MINUTES` | 60 | how often the labelling backlog is drained |
| `CHEMCLAW_VECTOR_STORE_MAX_SCOPE_KEYS` | 10000 | eligible keys a filtered index search may send |

## 6. What to write down where

Three facts outlive whoever set them up, and each has one right home:

- **The embedding model, on both sides** — in the manifest as a comment, because the manifest is the
  one file both sides read (`01-contract.md` §3.3).
- **The identifier recipe** — in the feeder's repository, next to the code that computes it. Changing
  it is not a migration; it is a new corpus (`02-acquisition.md` §3).
- **The licence and provenance of each release** — in the `releases` table, not in a README
  (`02-acquisition.md` §6). It is the answer to the question that gets asked a year later, by someone
  who will not have this document.
