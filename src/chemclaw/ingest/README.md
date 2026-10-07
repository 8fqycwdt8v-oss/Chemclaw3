# `chemclaw.ingest` — getting external records in

**Responsibility:** the one seam through which outside data enters the system.

- **`sources/`** is the seam itself (D-120). A data source has an ingest half, a retrieve half, or
  both; attaching one is a `sources/<name>/datasource.yaml` folder plus its name in
  `CHEMCLAW_DATA_SOURCES`, and **zero** core edits. `ingest/sources/README.md` is the how-to.
- **`eln/`** is the ELN family of adapters — free-text JSON exports, ORD, and a warehouse ELN read
  through `eln/warehouse/` — behind the seam.
- **`documents/`** is a mounted SMB/CIFS file share read as cited evidence (D-2026-08-06). It is
  also this system's one home for *reading a document* — the PDF/DOCX/XLSX/PPTX parsers live in
  `ingest/documents/parse.py` and `agent/attachments.py` imports them, because reading a PDF is an ingest
  concern that an upload happens to use, and `chemclaw.ingest` may not import `chemclaw.agent`.
- **`commitments/`** mirrors what a programme has committed to in from the system that owns it.
- **`labels/`** is the I/O half of the reaction-label index (`science/labels` is the pure half): the
  record-phase builder, the MCP client for the labelling server, and the drains.
- **`rejections.py`** is the ingest rejection ledger — what was refused, why, and since when.

A share is a **retrieve-only** source: the ingest half of this seam is reaction-shaped
(`ElnAdapter.map_to_ord`) and a PowerPoint is not a reaction, so its index is filled by a background
job — the shape `vector` and `lexical` already had, and the reason the "universal ingest
abstraction" in `BACKLOG.md` still has no second caller.

The acceptance test for the seam is in `tests/test_datasource_seam.py`: it attaches a source the way
an operator would, by writing a manifest into a directory, and touches no core Python at all.

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
addresses in the codebase are infrastructure the operator runs (the LLM gateway, Temporal,
Postgres, the organisation's own ELN warehouse), not somebody else's corpus. `tests/test_no_egress.py` enforces it against the
shipped registry rather than against prose — because prose is what failed the first time.
