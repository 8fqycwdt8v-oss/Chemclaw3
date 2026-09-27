# D-2026-09-14-an-artefact-three-files-name-and-none-produces — three documents named the run sheet's CSV export and nothing wrote one, so the design layer's only output that leaves the system had no file form

## Status

Accepted.

## Context

`protocols/README.md` lists "the CSV export" among the five things written once because there is one
design shape. `protocols/models.py` repeats it in the same breath. `ProtocolArm.arm_id`'s own
comment calls itself "the CSV row key", as a statement about a file format that exists.

Measured before this change: `grep -rn "csv" src/chemclaw/protocols/` returned exactly those three
prose hits and no executable line, and the only `import csv` anywhere in `src/` was in ingest
readers. Three documents named an artefact, one of them counting it among five things as an argument
for the single design shape, and nothing produced it.

That is the same shape `D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` names one
seam over — a declared artefact with no producer — and it is worth stating that this repository has
now found it twice in one day, in two subsystems, by the same method: grep for the producer of the
thing the prose claims, rather than for the noun.

It matters more here than a missing format usually would, because a run sheet is the one thing in
this subsystem that leaves the system. A protocol is read in a browser; a **plate** is carried to a
bench, pasted into instrument software, or imported into a LIMS. The design layer's whole output
had no file form.

## Decision

`src/chemclaw/protocols/export.py` produces it, and owns two spellings beside it.

**CSV rather than a spreadsheet, as a decision and not a first step.** Every consumer named above
reads CSV. `openpyxl` is in this tree, but transitively through `drfp`, with no version pin and no
declaration in `[project.dependencies]` — and four modules plus `tests/test_datasource_isolation.py`
exist to keep it out of the chat process's import graph. A deliverable format is not a reason to
undo that.

**Written through `csv.writer`, not by joining commas.** The property worth having is not that a
CSV comes out — joining commas satisfies that — it is that a factor level, a solvent name or a note
containing a comma, a quote or a newline does not shift every column after it. A run sheet that
silently mis-columns after a reagent called "toluene, anhydrous" is worse than no run sheet, because
somebody weighs out what the row says. `QUOTE_MINIMAL` with CRLF is what a spreadsheet and an
instrument both expect. Driven: replacing the writer with `",".join(...)` turns the comma test and
the quote/newline test red, and leaves the other five green — which is what makes those two the
tests and the rest the scaffolding.

**The row order is `render.run_sheet_rows`'s**, randomised against session drift when there is a
layout and arm order otherwise. A CSV that re-sorted would be a second opinion about the thing the
layout exists to fix.

**One address, and this module holds it.** `GET /protocols/{design_id}/run-sheet.csv` serves it and
`read_experiment_protocol` hands the model the path so a chemist gets a link instead of a retyped
table — two readers of one URL. Neither may own the spelling: two copies drift, and the one a model
quotes is the one nobody tests. `api -> protocols` is an allowed import direction and `agent -> api`
is not, so `export.run_sheet_path` is the only place both can read, with the route table itself as
the test's second basis rather than a third literal.

**The `Content-Disposition` names the revision, and is sanitised.** A sheet is printed and carried
to a bench where the design has already moved on, so two plates under one filename on one laptop is
the ordinary case rather than the edge one. And the `design_id` in that header is a *path
parameter*: `design_id_for` mints `design-<12 hex>`, but nothing between the URL and the header
enforces that, and `POST /protocols/{id}/revisions` files a revision under whatever the path said.
"The store 404s an unknown id" bounds which ids resolve, not which characters a resolving one holds.
Driven through the real app with a stored id carrying `"\r\n`: percent-encoded it travels (`httpx`
refuses a raw CR; Starlette unquotes before binding), the handler sees the CRLF, and without the
sanitiser it reaches the header value verbatim.

The `x-injected` half of that test was deleted rather than kept, and the reason is the rule this
repository keeps re-learning: ASGI carries headers as a list of pairs, so the CRLF never splits
in-process and an assertion about the split *outcome* passes whether or not the sanitiser is there.
What a real server splits on is the character, so the character is what is asserted.

## Consequences

- One new module, one new route, one new field. `ProtocolReadout.run_sheet` is a **path**, not the
  CSV: a read otherwise carries the plate twice, once as prose and once as a table, and the second
  copy is the one a model is most likely to retype with a digit changed.
- The field is declared in `render.py` and filled by the caller, because `export` imports `render`
  for the run order and cannot be imported back.
- Three prose claims become true. The README row for `export.py` says so explicitly rather than
  quietly — the sentence that listed it among five was wrong for as long as it stood, and a reader
  who believed it is the person this row is for.

## What keeps it true

- `tests/test_protocol_export.py` — the comma, the quote-and-newline, the absent number, the
  exponent form, the factor columns, the empty design, and the two spellings against the app's own
  route table.
- `tests/test_protocol_routes.py::test_the_run_sheet_comes_back_as_a_downloadable_csv`,
  `::test_an_older_revision_sheets_that_revision_and_says_so_in_the_filename`,
  `::test_a_design_id_cannot_write_its_own_response_headers`,
  `::test_a_run_sheet_of_a_design_that_does_not_exist_is_a_404`.
- `tests/test_protocol_routes.py::test_every_route_here_is_inside_the_apps_authentication_sweep` —
  the new route is in the set that sweep keys on.
