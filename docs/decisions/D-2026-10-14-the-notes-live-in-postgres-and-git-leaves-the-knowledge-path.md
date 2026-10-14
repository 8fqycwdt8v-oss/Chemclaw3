# D-2026-10-14-the-notes-live-in-postgres-and-git-leaves-the-knowledge-path — Postgres is the only store for notes; the Markdown format survives as body plus JSONB frontmatter, and the readers of the corpus go through `NoteStore`

**Status:** accepted · **Date:** 2026-10-14

Takes the storage choice `D-2026-10-07-the-architecture-programme` made for the knowledge graph and
fixes how it lands: the schema, the read interface, and the order. Nothing reads from Postgres yet;
`kg_store=git` is the only value and behaves exactly as before.

## Context

Notes are Markdown files with a YAML header in a git repository. Every pod keeps a clone and a
NetworkX copy of it, and one advisory lock serialises every write cluster-wide. What that costs,
measured:

| Cost | Number | Source |
| --- | --- | --- |
| One note per commit, 10,000-note corpus | 327.3 ms per note, so about 3 notes/s for the whole fleet | `D-2026-09-13-the-lock-is-not-the-bound-the-commit-is`, batching table |
| Of which the advisory lock | about 14 ms: 14.4 ms of a 298.8 ms fetch, commit and push at 10,000 notes (4.8%), taken from the ADR's separate lock table, so the share of the 327.3 ms is about 4.4% | same, lock table |
| The same write, batched 10 or 50 notes to a commit | 31.6 ms or 8.5 ms per note, ten- to 38-fold cheaper, with a note unreadable until its batch flushes | same, batching table |
| The parsed graph in every pod | about 6 kB per note: 5.98 at 1,000 notes and 5.96 at 5,000 (synthetic 1.1 kB bodies, two links each), so about 120 MB at 20,000 notes in each replica | `make architecture-baseline`, key `kg_graph_kb_per_note` |
| A second copy for retrieval | `note_index` holds an embedding, a `tsvector`, a content fingerprint and the checkout's commit count (`012`, `035`, `039`, `099`), rebuilt by `make reindex` from every pod's checkout | `infra/sql/README.md` |
| A clone per pod, kept current by `fetch` and `reset --hard` | `deploy/knowledge-sync.sh` on a 300 s cadence, plus `graph_cache_ttl_seconds` (60 s) before a pod looks; a note merged on one pod reaches another after the sync interval and the cache window | `deploy/helm/chemclaw/values.yaml`, `.env.example` |

The baseline script for the programme (W0.1) did not record the knowledge-store figures in
`docs/planning/architecture-baseline-2026-10-07.json`; the memory figure is now part of it, and the
table above is where the rest are kept. The two lock figures come from two runs of that ADR's
harness and are not one measurement.

## Options

1. **Keep git and batch the writes.** `BatchingNoteWriter` exists and cuts the cost per note ten- to
   38-fold. It leaves the clone per pod, the sync sidecar, the staleness window and the second copy
   in `note_index`, and a batch is a queue: a note a chemist just learned cannot be read.
   `D-2026-09-13` deferred it for the conversational path with a rate trigger, and it would still
   fix one of five costs.
2. **Postgres with git as an export mirror.** Removes the per-pod reads and the lock, but keeps a
   writer that pushes, credentials for it, and a second source of truth that can diverge. Every
   failure of the mirror is then a decision about which store is right.
3. **Postgres only.** One store that is already required, pooled, backed up and, in W7, tenant
   isolated. History is a table, a human edit is an API call, and Markdown is something the system
   can export, not something it keeps.
4. **A graph database.** Declined: one more store to back up, secure and isolate for traversals a
   recursive CTE over an indexed edge table answers at this corpus size. The programme ADR holds
   its `Revisit when:`.

## Decision

**Postgres only (option 3).** There is no mirror; once the reads flip, git is not written.

**The format stays; the container changes.** A note is still a header and a body. The header is
JSONB, the body is text, and `kg/rows.py` is the one mapping between `Note` and the row:
`id`, `type`, `created_by`, the validity window and `body` are columns, and every other field of
`Note` (`compound_smiles`, `tags`, `actor`, `source`, `confidence`, `conditions`, `calc_refs`,
`artifact_refs`, `relations`) is `frontmatter`. A field the model grows needs no migration.

**Three tables, no more** (`infra/sql/128_kg_notes.sql`):

- `kg_notes`: the current revision of each note. `validity` is the window as a `daterange`, which is
  `TemporalWindow.is_current` exactly (inclusive at both ends, open where a bound is absent).
  `updated_at` moves with each revision so a cache can key on the count and the newest value.
- `kg_note_revisions`: every revision, written once and never changed. It replaces the repository
  log as the audit trail and adds what the log never had: the person on whose behalf a revision was
  written (`recorded_by`, the turn's or job's actor), where git's committer was always the service.
- `kg_edges`: the typed edges a note asserts, from its body links and its `relations:` together
  (`Note.outgoing_relations`), derived when the note is written. `dst` has no foreign key: a link to
  an id nothing defines, or to a `reaction-<id>` record, is a bare node in the graph today and stays
  one. Edge windows and `confidence` are the edge's own.

**`content_sha` is the digest of the canonical, redacted render, and it is not the file
fingerprint.** It is `sha256` of `render_note` with this process's credentials redacted
(`kg/rows.py`), the text `kg.record` writes. `note_index.fingerprint` is the digest of a file's
bytes, which also depends on how a person laid the file out; the two agree for 29 of the 41 shipped
notes and differ for the hand-formatted rest. Options were a second `file_sha` column, or one
digest and a one-time re-embed. The second is taken: a database store has no files, so the only
fingerprint it can have is of the note, `fingerprints()` answers with `content_sha`, the flip
re-embeds each note once, and `kg-validate --compare` compares notes and edges, never fingerprints.
A digest of the unredacted model would describe a different text from the one stored, so redaction
is part of the definition and the Postgres write stores the redacted note.

**Tenancy is in the keys now.** Every table carries `tenant_id TEXT NOT NULL DEFAULT 'default'` and
leads its primary key, foreign keys and indexes with it. A key on `id` alone could only be widened
by dropping it, which a forward-only schema refuses, and a table is empty only once. Deployments are
one tenant, so nothing writes the column and the Python side does not name it; the row-level
security policies come with the tenancy wave (W7.5), consistent with `tenant_id` plus RLS. `note_index`
has no tenant of its own yet, so its key names the note's tenant beside its id.

**`note_index` follows by key, in three releases.** This one adds a nullable `kg_tenant_id` and
`kg_note_id` with an indexed `MATCH FULL` foreign key and writes nothing to them (expand). The next
fills them from the imported corpus and reads them in place of the fingerprint and commit-count
columns (migrate). The one after stops writing those (contract). Nothing is dropped.

**A read interface in front of both stores.** `kg/store.py` defines `NoteStore` (`get`,
`get_many`, `notes`, `related`, `neighborhood`, `edges`, `current_successor`, `search`, `graph`) and
`CorpusState` (`snapshot`, `signature`, `arrivals`, `fingerprints`, `unparseable`, `revision`,
`refresh`), and `GitNoteStore` answers by delegating to `kg/graph.py`. `note_store()` selects by
`kg_store` and has an exhaustive `match`. The second group exists because caches and the reindex
ask whether the corpus moved without reading a note; a Postgres store answers each from a column:
`fingerprints` from `content_sha`, `arrivals` from the first revision's `created_at`, `revision`
with `None` (one database has no pod that is behind), `signature` and `snapshot` from the count and
`updated_at`. `get_many` is one consistent read for the readers that look up a set of ids.
`graph()` is for a whole-graph analytic and nothing else (`find_knowledge_gaps`, `kg/analytics`);
a request reads through the other methods.

**There is no write on the interface.** `kg.record.record_note` is the one write path and takes a
`NoteWriter`, whose `write` is shaped like a git file. Stage 2 designs the Note-level write that runs
in one transaction and puts `put` on the interface then, with the real implementation behind it,
rather than a second seam now beside the first.

**The readers that do not go through `note_store()`, truthfully:** the writers (about eleven call
sites pass `default_writer()`; stage 2), `kg/validate.py` and `cli/validate_kg` (by design: they
audit raw files and report paths), `cli/verifier_margin.py` (reads the tree directly),
`api/app.py`'s `startup_inventory` (counts `*.md` files, configuration only) and the
`knowledge_sync_age_seconds` gauge (deleted with the sidecar in W4.9). Several reader helpers still
take a `directory` (`conflict_index`, the retrievers' `notes_dir`, the `exists()` guards) because
the git store is a directory; they go at the flip.

**Order of the programme** (`tasks/todo.md`, W4): this record covers W4.1 to W4.3. Then the writer
becomes one transaction in the order dependencies, subject, retirements; the importer and dual-write
follow; reads flip only after `kg-validate --compare` has been clean for seven days; and the git
writer, the per-pod clone, the sync sidecar and its alert are deleted only after the restore drill
of W7.7 has run, because after that the rollback is a Postgres restore.

## Decided for stage 2

- **The read interface becomes async before the Postgres store lands.** Reads today are
  synchronous under `asyncio.to_thread`; a database store has two ways to be read that way and both
  lose. A sync psycopg pool is a second pool per process: the fleet's connection budget is counted
  from the list of pool-minting call sites (`tests/test_fleet_pools.py`,
  `D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint`: one pool per role), and
  a pooler in transaction mode serves only the stateless connections. Bridging into the loop's pool
  from the worker thread works only where a loop exists, and the CLIs and several tests have none.
  So `NoteStore` and `CorpusState` reads become `async def` over `core.db.connection`, the git store
  wraps its calls in `to_thread`, and callers `await` them. This is a mechanical change with no
  behaviour in it and ships as its own pull request first; the tests that call the synchronous
  helpers (`_match_corpus`, `_eligible_sync`, `_rank_by_terms`) move with it.
- **Search needs a stored haystack.** `find_notes` and the retrievers match substrings over
  `kg.search.search_text(note)`, which a SQL `ILIKE` over `body` and `frontmatter` does not equal.
  The Postgres `search` stores that text (a generated or maintained `search_text` column with a
  trigram index) in the change that writes `search`, not before: a column nothing reads is dead.
- **`put` and the transaction.** `put(notes)` takes `Note` values, redacts them, writes
  dependencies, subject and retirements in one transaction, appends `kg_note_revisions`, re-derives
  `kg_edges`, and reports how many notes changed; the conformance suite then builds its corpora
  through it. It refuses to overwrite a person's note with an agent's, as the git writer does.

## Consequences

- Behaviour is unchanged with `kg_store=git`. The tests that pin the readers pass without edits
  except the two that patched a reader's module-level `load_notes`, which now patch the store.
- One refusal is new: a `Note` containing U+0000 is rejected, because Postgres refuses it in `text`
  and `jsonb` and a note git accepts would fail after the flip. A note carrying one cannot have
  been recorded usefully (a NUL in a Markdown file is a corrupt file), and none is shipped.
- The runtime role gets no grant on the new tables in this change. The privilege matrix is checked
  against the SQL `src/` runs, so the grants (INSERT only on `kg_note_revisions`) arrive with the
  first statement that writes.
- `kg-validate` stays a file-level audit of the repository until it learns the store: it reports
  paths, filenames and misfiled types, which a store has no notion of.

Revisit when: a traversal the store must answer exceeds what a recursive CTE serves within the
turn's latency budget, measured on `kg_edges` at the corpus size of the day; or the clean-comparison
window before the flip cannot be reached because the two stores keep diverging for a reason the
importer cannot explain.

## What keeps it true

- `tests/test_kg_schema.py` migrates the schema and round-trips every note of the shipped
  `knowledge/` corpus, and a note carrying every field of `Note`, through the columns and back. It
  compares each edge's window and confidence with the graph's and selects by `validity @>` as
  `related(as_of=)` does (a mutation of `edge_columns` fails it). It checks the slug, type,
  `created_by`, revision and confidence constraints against the model, that keys are per tenant,
  that `note_index` takes a matching key or none, that a NUL is refused by the model and the
  database, that `content_sha` is a property of the note (stable under the canonical file, equal to
  the writer's own file for a note with a credential in it, and equal to the shipped file's
  fingerprint exactly when the file is the canonical render), and that an erasure counts the notes
  and revisions written for a person and keeps them (`agent/leaver.py`).
- `tests/test_note_store.py` is a characterisation of the git adapter that is parametrised over a
  registry, `_STORES`, with one entry today. The Postgres store joins by adding a harness that
  builds its corpus through its own write, and is then held to the same assertions. The same file
  counts reads of the corpus: one per batch for `get_many`, a premise check, a protocol comparison
  and a job grounding, including the empty batch.
- `tests/test_layering.py`: `kg` imports nothing above it, and `tests/test_database_privileges.py`
  and `tests/test_retention.py` hold the grant and disposal rules for the three tables.
