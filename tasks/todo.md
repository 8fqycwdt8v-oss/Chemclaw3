# Task: the Snowflake ELN — a schema nobody knows yet

Planned 2026-08-04. Branch: `claude/snowflake-eln-generic-concept-6aikvz`.

**Question asked:** the final ELN integration will be Snowflake, carrying reaction SMILES, protocol
text, project ids, child tables of charged amounts, yield, purity, "many more data tables" and a
per-reaction vector in a warehouse vector table — and nobody knows today which tables or fields
exist. Come up with a generic concept that makes connecting easy later.

**Answer shipped:** the schema is a file, not an adapter. `chemclaw.ingest.eln.warehouse` is a
generic engine naming no table and no column; the site's schema is a *binding* in the source's
manifest. Attaching the real warehouse is writing YAML. Decision:
`docs/decisions/D-2026-08-04-the-schema-is-a-file.md`; concept:
`docs/guides/warehouse-eln-concept.md`.

## Work packages

- [x] **W1** The binding engine — `binding.py` (the document's schema, `extra="forbid"`),
      `expr.py` (paths + a closed transform vocabulary), `sql.py` (checked identifiers written,
      every value bound), `driver.py` (Protocols only, no vendor import), `connect.py` (late-bound
      driver, credentials read from named env vars).
- [x] **W2** The unmapped-column problem — `attributes: dict[str, str]` on `OrdReaction` and
      `Component`, rendered last in the note body by `ingest/eln/note.py`. Bounded; never reaches
      `transformation_smiles` or either fingerprint.
- [x] **W3** Both halves — `WarehouseElnAdapter` (`COALESCE` watermark so amendments count as new;
      one child query per table per batch) and `WarehouseVectorRetriever` (ANN inside the warehouse,
      cites `<source>:<row>`, `suppress_ingested` keeps the no-double-surfacing rule). Plus
      `snowflake.py`, the only module that knows a vendor exists.
- [x] **W4** The worked manifest `sources/eln-snowflake/datasource.yaml` (discovered, not enabled)
      and the offline suite: `tests/warehouse_fake.py` + adapter/retriever/binding tests.
- [x] **W5** `--construct` on `make datasource-validate`, so an operator can check a mounted binding
      offline — binding the kwargs alone cannot see inside a binding document.
- [x] **W6** Docs and registers: ADR + ledger row, concept guide, package README, seam README,
      `DEFERRED.md` rewritten, `BACKLOG.md`, `CLAUDE.md`, capability-map row.

## Verification

`make lint type test` green: **2913 passed, 127 sandbox-skipped**. `make ci` green through every
validator (`datasource-validate` also passes with `--construct`); `helm-validate` fails only because
`helm` is not installed in this sandbox, unrelated to this change and the same as the previous task.

What the fake-driver suite actually proves, with no tenant and no client installed:

- the cursor filters and orders on `COALESCE(modified, created)`, asserted on the emitted SQL —
  the failure this prevents is silent (an amended run simply never re-arrives, no error, no reject);
- child tables are fetched once per batch, not once per reaction;
- a new child table reaches the payload with **no Python change** — the claim, as a test;
- the site's `SM`/`SOLV`/`PROD` vocabulary maps to `Role`; grams → mg; minutes → h;
- unmapped columns survive into `attributes`, bounded, without repeating consumed fields, and
  **without changing `transformation_smiles()` or `reaction_smiles()`**;
- an unmapped vocabulary value rejects its row rather than dropping a field silently;
- the retriever ranks and truncates server-side, cites the row, and suppresses a reaction already
  merged as a note;
- an unreachable *or misconfigured* warehouse costs that leg of the fan-out and nothing else.

## Review

**Three things the work found that the plan had not.**

*The canonical record has no place for the impurity profile through a scalar field binding.*
`purity_percent` is one column, but the impurity table behind it is rows. Added `impurities:` as a
block mirroring `components:`, reusing the same reader — otherwise the first real binding could not
carry a profile that `OrdReaction` already models, which would have been a hole in "connect without
code" on a field the question named explicitly.

*A tree-walking test imports every first-party module.* `tests/test_publish.py` enumerates the error
hierarchy that way, so a module-scope `import snowflake.connector` would have made this repository's
own suite depend on a client only a real deployment has. The driver's client import moved inside a
function — the one departure from the seam's "import at module scope" corollary, and it departs for
a reason that corollary does not cover: that rule is about which *process* pays for an import, this
is about a package installed in none of them.

*`gather_evidence` fans out with a plain `asyncio.gather`, no `return_exceptions`.* So a raising
retriever does not degrade a question, it loses it. The first version caught transient failures and
would have let a `BindingError` — a driver package the image lacks — escape and break every question
in the process. Now caught, and logged at ERROR rather than WARNING because it recurs until someone
changes the deployment.

**One thing stated plainly rather than glossed.** This is *not* zero core edits. `attributes` on
`OrdReaction` and `Component`, the note renderer, four names in the non-retryable list, one runtime
hook in the log-redaction inventory, and the pinned source set in `tests/test_no_egress.py` all
changed. Each is small and each is argued in the ADR — but the seam did not hold perfectly, and the
honest framing is that one typed field was the price of "a new column is a line of YAML".

**Deliberately not built.** A schema-introspection CLI drafting a binding from `INFORMATION_SCHEMA`.
It can only be written against a real warehouse's metadata; written against an imagined one it would
draft bindings nobody can use.
