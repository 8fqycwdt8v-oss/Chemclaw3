# `chemclaw.ingest` — getting external records in

**Responsibility:** the one seam through which outside data enters the system.

- **`sources/`** is the seam itself (D-120). A data source has an ingest half, a retrieve half, or
  both; attaching one is a `sources/<name>/datasource.yaml` folder plus its name in
  `CHEMCLAW_DATA_SOURCES`, and **zero** core edits. `ingest/sources/README.md` is the how-to.
- **`eln/`** is the first concrete family of adapters — free-text JSON exports and ORD — re-hosted
  behind the seam unchanged when F7 introduced it.
- **`documents/`** is a mounted SMB/CIFS file share read as cited evidence (D-2026-08-06). It is
  also this system's one home for *reading a document* — the PDF/DOCX/XLSX/PPTX parsers live in
  `ingest/documents/parse.py` and `agent/attachments.py` imports them, because reading a PDF is an ingest
  concern that an upload happens to use, and `chemclaw.ingest` may not import `chemclaw.agent`.

A share is a **retrieve-only** source: the ingest half of this seam is reaction-shaped
(`ElnAdapter.map_to_ord`) and a PowerPoint is not a reaction, so its index is filled by a background
job — the shape `vector` and `lexical` already had, and the reason the "universal ingest
abstraction" in `DEFERRED.md` still has no second caller.

The acceptance test for the seam is in `tests/test_datasource_seam.py`: it attaches a source the way
an operator would, by writing a manifest into a directory, and touches no core Python at all. Before
D-120 that test had to `monkeypatch.setitem` a dict inside the registry — a test that must reach
into core to add a source is evidence the seam does not work.

## A missing source fails loudly

The failure this layer is most exposed to is silence: a retrieval returning nothing looks exactly
like a corpus with no matches. So a name in `CHEMCLAW_DATA_SOURCES` that no manifest declares is a
startup error, not a corpus that quietly stops being read.

## Reaching outward, and the one thing that is not reaching outward

A **mounted** share is not an external data source in D-089's sense and needs no exception to it:
the code takes a POSIX path, so there is no client, no credential and no network peer — the mount is
the platform's job. That is why `tests/test_no_egress.py` needed no amendment when the share landed.

## No external data sources

`ingest` reaching outward is bounded by D-089: this system takes no third-party data source. The
addresses in the codebase are infrastructure the operator runs (the LLM endpoint, Temporal,
Postgres, Tower), not somebody else's corpus. `tests/test_no_egress.py` enforces it against the
shipped registry rather than against prose — because prose is what failed the first time.
