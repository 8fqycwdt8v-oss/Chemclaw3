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
reviewer nothing to decide, and an entry is readable the moment it is ingested — the gate that used
to stand here cost 202 ms of serialized git per entry and was deleted with its whole mechanism.
