# Concept: attaching a warehouse ELN whose schema nobody knows yet

**Status:** implemented offline; the live connection waits on a tenant. The decision is
`docs/decisions/D-2026-08-04-the-schema-is-a-file.md`.
**Scope:** how ChemClaw connects to a corporate ELN held in a SQL warehouse — reaction records,
their child tables, and the per-reaction embedding beside them — when the tables and columns are not
knowable in advance.
**Non-scope:** the live tenant, the driver dependency, and user-scoped reads via on-behalf-of; all
three need infrastructure this environment does not have (`docs/planning/DEFERRED.md`).

---

## 0. The problem, and the answer in one line

The final ELN integration will be a Snowflake database. It will carry reaction SMILES, protocol
text, project ids, several child tables of charged amounts for starting materials, solvents and
reagents, yield, purity, more tables than anyone has enumerated, and a vector representation of each
reaction in a warehouse vector table.

**Nobody knows today which of those are tables, which are columns, or what any of them are called.**

So the schema is not written in Python. It is written in the source's manifest, as a *binding*, and
a generic engine executes it. Attaching the real warehouse is writing that file. Adding the table
the site invents next quarter is adding a block to it.

## 1. What was already free, and what was not

D-120 made *attaching* a source free: a folder holding a `datasource.yaml`, plus the folder's name
in `CHEMCLAW_DATA_SOURCES`, and no core Python changes. `docs/planning/DEFERRED.md` recorded the
Snowflake connector as blocked on "the tenant, not the seam", and for attachment that was true.

It was not true for *mapping*. Both existing adapters —
`src/chemclaw/ingest/eln/json_adapter.py` and `src/chemclaw/ingest/eln/ord_adapter.py` — name their
source's fields in code, which is correct for them: both formats were fixed before the adapter was
written. Under that pattern, a Snowflake ELN means writing an adapter on the day access arrives, and
editing it whenever a column lands. The seam was ready; the mapping was still a code change waiting
to happen.

## 2. The binding

One document, inside the manifest's `config:` block, in five parts.

- **`connection:`** — where the warehouse is, and the *names* of the environment variables holding
  its credentials. Also the driver, as a `module:callable`.
- **`entry:`** — the root query. Which relation holds reactions, which column is the id, and which
  columns the sync cursor reads.
- **`related:`** — one block per child table. This is the answer to "and many more data tables": the
  charge sheet, the analytics, the workup log and whatever comes next are each a block, and none of
  them costs a line of Python.
- **`reaction:` / `components:` / `impurities:` / `attributes:`** — which column becomes which field
  of `OrdReaction`, which child rows become charged species and impurity peaks, and what happens to
  everything else.
- **`vector:`** — the embedding column, and how a similarity search over it is run and filtered.

Two grammars, both deliberately small. A **path** names a value (`root.YIELD_PCT`,
`analytics[0].PURITY_PCT`, or a bare column inside a child-row block). A **transform chain** reshapes
it — minutes to hours, grams to milligrams, the site's `SM` to this schema's `reactant`.

`src/chemclaw/ingest/sources/eln-snowflake/datasource.yaml` is the worked example, and
`tests/test_warehouse_binding.py` resolves every path in it against a fixture row, so it cannot decay
into a file that only looks correct.

## 3. Four things the binding language deliberately does not do

**It cannot run code.** Transforms are looked up in one table of pure functions in
`src/chemclaw/ingest/eln/warehouse/expr.py`. There is no `eval`, no import from a transform name, and
an unknown name fails when the binding loads. A binding is a configuration file, and a deployment
mounts a directory of them; if a name in one could reach arbitrary code, mounting would mean mounting
an execution surface.

**It is not a query language.** JSONPath and a small expression language were the obvious reaches,
and both buy generality this problem does not have: a binding maps columns onto a fixed schema, so
every expression it needs is "one value, optionally reshaped". `src/chemclaw/templates/resolve.py`
made the same call for the same reason.

**It cannot build a value from more than one place.** The fields that are *rows* rather than values —
components, impurities, provenance, the attribute bag — each come from their own section. A binding
that could also map them field-by-field would be two answers to one question.

**It does not segment prose.** A warehouse records a protocol as text, which lands in
`procedure_text` verbatim. Turning prose into ordered steps is what the free-text adapter already
does; a second, YAML-driven segmenter would be that logic twice.

## 4. The column nobody has a field for

`OrdReaction` will never have a field for every column a corporate ELN carries — a lot number, an
equivalents figure, an assay, a vessel id, whichever tables the site keeps. Under the obvious design
each newly-interesting column costs an edit to the model, to the note renderer, and to their tests.

So the canonical record gained one bounded field: `attributes`, a bag of strings, on the reaction and
on each component. `attributes:` in the binding decides what lands there — named columns, or
everything the row held that no field already took. It is rendered at the end of the note body, and
it is capped, because a wide view would otherwise put a hundred unmodelled lines into every note and
push the actual chemistry out of the retrieval excerpt.

Three properties make it safe rather than a second untyped schema:

- **Strings, not values.** These are unmodelled by definition, so there is no type to validate and no
  unit to normalise to. Stringifying also keeps the note body deterministic, which the sync's
  amendment detection depends on — it compares merged note bodies byte-for-byte.
- **Never chemistry.** `reaction_smiles`, `transformation_smiles` and both fingerprint paths ignore
  it entirely. A structure reaching the corpus through an unvalidated bag of strings is exactly the
  failure the typed fields exist to prevent, and a test pins it.
- **A datum that earns a real question earns a real field**, in its own change. This is where a
  column waits until someone asks something of it — not a place to leave it forever.

## 5. Searching the vector table where it lives

The warehouse holds an embedding per reaction, over a corpus much larger than the part worth
ingesting. Copying those vectors into this system's own index would mean re-embedding the larger
corpus and keeping the copy fresh forever, so the retrieve half pushes the search into the warehouse:
the similarity, the ordering and the limit are all in the statement.

Two consequences worth stating plainly.

**It is a `SourceRetriever`, not a `NoteIndex`.** The note index was the obvious seam and is the
wrong altitude: `src/chemclaw/retrieval/retrievers.py` drops any hit whose note is not on local disk,
which is every warehouse row. `src/chemclaw/retrieval/evidence.py` says the same thing from the other
side — a new source is a new retriever behind that interface, never a change to core.

**It cites the row, not a note.** There is no note id for a reaction that was never proposed as one,
and inventing one would be a citation that resolves to nothing. `src/chemclaw/ingest/sources/vendored_dataset.py`
made the same call for the same reason: a citation has to resolve to something a reader can check.

### Why this ELN carries both halves when the others carry one

`src/chemclaw/ingest/sources/README.md` states the rule: an ELN whose records become notes does not
also carry a retriever, or every ingested reaction is surfaced twice. That rule is about
double-counting, not about ELNs — and a file-drop ELN ingests everything it sees, so for it the two
are the same thing.

A warehouse ELN ingests a curated slice of something much larger, and the rest has no other way in.
`suppress_ingested` keeps the rule intact by dropping exactly the hits that did become notes. What
reaches the agent is reviewed knowledge for the curated part, raw rows for the rest, and never both
for one reaction — which matters because a duplicate would read as two sources agreeing.

## 6. Why this is provable now, with no tenant

`src/chemclaw/ingest/eln/warehouse/driver.py` is Protocols and nothing else — no vendor import, no
`chemclaw.core.db`. That is what lets `tests/warehouse_fake.py` exist: a warehouse that serves canned
rows and records the exact statement it was sent.

So the parts that would otherwise be untestable until access arrives are asserted today: that the
cursor predicate reads the later of created and modified (an amended run counts as new, and getting
this wrong is silent — no exception, no rejected row, the correction simply never arrives); that
child tables are fetched once per batch rather than once per reaction; that the site's vocabulary and
units map; that unmapped columns survive and are bounded; that a bad row is rejected and the batch
continues; and that the similarity search is ranked and truncated by the warehouse.

`snowflake.py` imports its client inside a function rather than at module scope — the one departure
from the seam's "import whatever you need at the top" corollary. That corollary is about which
*process* pays for an import; this is about a package that is not installed in any of them, because
the client is not a dependency of this repository and should not become one before a tenant exists.

## 7. Attaching the real one

1. Copy `src/chemclaw/ingest/sources/eln-snowflake/datasource.yaml` into a directory the deployment
   mounts, and replace every name in it with the site's own.
2. Put that directory first in `CHEMCLAW_DATA_SOURCES_DIR` — it is an OS-pathsep search path where
   the earlier entry wins, so the site's schema is never a change to this repository.
3. Set the environment variables the `connection:` block names.
4. `make datasource-validate`, then the same command with `--construct`, which builds the halves and
   so checks the binding itself rather than just the keyword carrying it. Neither needs a network.
5. Add the source's name to `CHEMCLAW_DATA_SOURCES`. Discovery is not enablement (D-018): the repo
   ships the source disabled, and a deployment runs the subset it has validated.

Then install the warehouse client in the image that runs the sync worker. That, a reachable tenant,
and the site's real binding are the whole remaining list.

## 8. What is still open

**Per-user reads.** Everything here connects as a service identity, so warehouse-side row access
control sees one principal. `src/chemclaw/agent/identity/obo.py` exists for exactly this and is
dormant; wiring it needs a real tenant on both sides. Until then, the deployment's answer to "who may
see which reactions" is the view named in the binding.

**Child tables cost a query each.** One `IN (...)` per block per chunk is the mitigation, and it is
the right shape, but a site with a dozen child tables still issues a dozen queries per batch. Worth
measuring against a real warehouse before optimising against an imagined one.

**`Component` has no home for equivalents, concentration or assay.** They land in `attributes` as
strings today, which is honest and not queryable. Promoting any of them is a later decision, driven
by a real question rather than by the fact that the column exists.
