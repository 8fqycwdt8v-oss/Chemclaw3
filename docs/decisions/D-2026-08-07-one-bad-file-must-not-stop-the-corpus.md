# D-2026-08-07-one-bad-file-must-not-stop-the-corpus — the guard belongs at the boundary, not on the constructor

**Status:** accepted

## Context

The same review that produced `D-2026-08-07-the-mark-means-observed-not-processed` found a second
family in the mounted-share corpus: not data loss, but **availability**. One malformed file, or one
transient database blip, stopped far more than itself.

**1. Each parser guarded its constructor, which is the call these libraries do the least work in.**
`_parse_xlsx` wrapped `load_workbook` — but `read_only=True` parses the sheet inside `iter_rows`.
`_parse_csv` wrapped `csv.Sniffer().sniff` — but the reader raises from `csv.reader`. Measured:

```
truncated sheet1.xml  -> xml.etree.ElementTree.ParseError   (a SyntaxError)
unbalanced quote, 200kB field -> csv.Error: field larger than field limit (131072)
issubclass(ET.ParseError, (DocumentParseError, OSError)) -> False
issubclass(csv.Error,     (DocumentParseError, OSError)) -> False
```

Neither is caught by `_parse_changed`'s reject-and-continue net, so both escaped `sync_share` and
failed the activity. The crawl keeps **no cross-run cursor** — by design, since the sweep needs a
complete pass — so every later scheduled run restarted from the top and hit the same file. One
half-written `.xlsx` from an interrupted network copy, a routine artefact on a decade-old share,
stopped the entire corpus from indexing past that path, permanently and with no report saying so.

**2. `retrieve()` promises "never raises" and did not deliver it for the deployed backend.**
It caught `(ConnectionError, OSError, RuntimeError)`. `psycopg.Error` descends from `Exception`;
`db.connection` converts only *connect-time* failures. So a statement timeout on a large share —
`pg_statement_timeout_seconds` is set on every connection this index opens — propagated out through
`gather_evidence`'s bare `asyncio.gather` (no `return_exceptions`) and failed the whole turn,
taking the knowledge graph's answer with it. Exactly the outcome the except-clause was written to
prevent.

**3. The re-embed drain is deterministic and runs ahead of the crawl.** `stale_chunks` is
`ORDER BY doc_id, ordinal LIMIT k`, so a chunk the provider refuses produced the identical failing
batch on every retry, exhausted `activity_max_attempts`, and killed the workflow **before it reached
the crawl loop** — blocking indexing for every share, not just the affected one.

**4. `max_file_bytes` bounds compressed bytes, and OOXML is a zip.** Measured on a hand-built
workbook: 110,579 bytes on disk, 31,200,133 bytes of sheet XML — **282×**. At the shipped 50 MB
limit that is ~14 GB of XML. The worker is OOM-killed with no counter and no log line, and the same
`parse_document` serves chat uploads.

## Decision

### One net, at the boundary, around the whole parse

`parse_document` wraps the parser call: `DocumentParseError` (and its `ScannedDocumentError`
subclass) passes through untouched, anything else becomes `DocumentParseError(f"could not read
{name}: …")`. The four per-parser constructor guards are **deleted** — they were four partial nets
each covering only the first line of its function, and the boundary net subsumes all of them.

Deliberately broad, and only here: `raw` is untrusted bytes from a share or an upload, and every
library below is a third-party parser over them. *"This file could not be read"* is the honest
statement about any failure in that region, and it is the one callers already handle. The message
now names the file, which the old per-parser messages did not — an operator reading
`skipped_unreadable: 1` against 500k files needs to know which one.

Two of the four suspected formats turned out to be **already covered**: `python-docx` and
`python-pptx` parse eagerly enough that a truncated part fails in the constructor. Recorded because
the reviewer's "structurally identical" reasoning was right about the shape and wrong about two of
the four instances; the parametrised test keeps all four as regression cover.

### The index gets a wrapper type — on the *outage* hierarchy, not the bad-data one

`DocumentIndexError`, raised by `PostgresDocumentIndex._run` around `psycopg.Error`. This is the
`WarehouseQueryError` shape that `WarehouseVectorRetriever` has; the pattern was copied into this
package without the piece that made it work.

**It subclasses `SubsystemUnavailableError`, and the first attempt got that wrong.** I initially
made it a `DocumentShareError`, reasoning that one except clause should catch "anything wrong with
this share". `tests/test_publish.py` rejected it, and it was right to: `ChemclawError` is this
repository's **non-retryable bad-data** contract — every subclass name is registered in
`_BAD_DATA_TYPES` precisely so an activity fails fast instead of retrying invalid input. A statement
timeout says nothing about the query, and the identical call succeeds once the database is back.
Registering it as bad data would make a workflow give up on a blip it would otherwise ride out,
which is the whole argument `SubsystemUnavailableError` exists to make (and why that hierarchy's
*absence* from the list has an asserting test of its own).

The convenience of one except clause was not worth putting a retryable failure in the non-retryable
hierarchy. The retriever names both, which costs one tuple entry.

Only the *read* path is wrapped. A backend failure during a sync **should** fail the activity, which
is what Temporal's retry is for.

### A failed re-embed batch is retried per chunk, and cannot spin

On a batch failure `reembed_stale` falls back to one chunk at a time, so the rest of the batch is
still refreshed and only what genuinely cannot be embedded is left behind. `ReembedReport.failed`
carries the count, logged at ERROR — a superseded vector that could not be fixed is exactly the
silent wrongness this mechanism exists to prevent, so it must not become silent again at the
recovery step.

`has_more` becomes `len(stale) == limit and bool(refreshed)`. Without the second clause a batch
where every chunk failed would return the identical batch forever — the same wedge, one layer up.

### Zip containers are checked for expansion before a parser sees them

`document_max_expanded_bytes` (512 MB), read from the zip's central directory so it costs no
decompression. **The residual is stated rather than hidden:** a hand-crafted archive can understate
`file_size` and this check believes it. It bounds the realistic case — a real generator writes true
sizes — and is not a defence against a crafted one, which needs a streaming limit at every read.

## Consequences

- Seven new tests. Three of them fail against the unfixed parser, which is what makes them
  discriminating rather than merely green.
- One message change: `could not read the PDF` became `could not read broken.pdf`. The existing
  assertion was updated rather than the message reverted — naming the file is the improvement.
- A file that expands past the ceiling is refused for chat uploads too. That is intended: the chat
  pod has less memory headroom than the worker, not more.

## Alternatives rejected

**Wrap each extraction loop in its own `try`.** The obvious fix, and it leaves eight places where
the next parser added forgets one. The failure was not that a particular loop was unguarded; it was
that the guard was on the wrong side of the call.

**Catch `psycopg.Error` in the retriever.** Fewer lines, and it puts the database's vocabulary in
the retrieve half, which then has to re-learn it for every future backend. The wrapper type is the
established pattern in this repository and it already had a precedent to copy.

**Make `DocumentIndexError` a `DocumentShareError`.** Tried, and wrong — see above. It buys one
except clause and pays by classifying a retryable outage as non-retryable bad data.

**Mark a poison chunk in the schema so it is never retried.** Correct, and a migration plus a state
column for a case that should be rare. Per-chunk retry costs one failed call per run and needs no
schema; if that ever becomes material, the ERROR line names it.
