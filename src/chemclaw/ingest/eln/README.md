# `ingest/eln` — turning lab-notebook entries into records this system can query

The integration layer between somebody else's ELN and a canonical, structure-addressable record.
Every ELN-specific quirk is confined to an adapter behind the `ElnAdapter` contract
(`adapter.py`), so nothing above it knows any ELN's shape.

| file | what it is |
|---|---|
| `ord.py` | the ELN-agnostic target schema, borrowed from the Open Reaction Database |
| `adapter.py` | the contract every source implements, and the `limit` it may honour or ignore |
| `json_adapter.py`, `ord_adapter.py` | the file-drop adapters, reached through `ingest/sources/*` |
| `warehouse/` | the generic warehouse engine whose schema is a binding in a manifest, not Python |
| `record.py`, `records.py` | the transcription tier — Postgres rows rather than notes (D-2026-08-25) |
| `sync.py`, `cursor.py`, `ingest.py` | the incremental drain and the datetime cursor it resumes from |
| `validate.py`, `compound.py` | structure handling and the checks a transcription has to survive |

**A transcription is data, not a claim.** `record_from_ord_reaction` infers nothing, so it hands a
reviewer nothing to decide, and an entry is readable the moment it is ingested.

**A record has a tier, and a structure is never invented to avoid the lower one.** A species the
source names without a structure — a paper's shorthand the reagent table cannot resolve — is carried
verbatim as an `UnstructuredComponent`, and the record is *citation-only*: stored and citable for
what it states, and written to no fingerprint or molecule index, so no structure or similarity
search can return it
(`D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable`). It has a label row of the
record phase alone, which the facet tools keyed on a drawn species count in a tier of its own and
mark, and which the labeller never reads
(`D-2026-10-23-a-facet-on-a-drawn-species-counts-the-citation-tier`).
