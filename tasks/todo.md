# ELN records become queryable data, not knowledge-graph notes (D-2026-08-25)

## Task
Remove the PR-gate from ELN ingestion. Keep the ELN queryable ("similar reaction", "same
product") with full content. Nothing extracts knowledge automatically without a user asking.

## Done
- [x] Measure the gate before changing it (202 ms/note serialized git; 425 µs/note corpus scan;
      zero LLM calls in ingest; refs *not* the bottleneck — disconfirmed)
- [x] Migration `050_reaction_records.sql` + grants + `infra/sql/README.md` inventory row
- [x] `ingest/eln/records.py` — Protocol + InMemory + Postgres, shaped like `fingerprints/store.py`
- [x] `ingest/eln/note.py` → `record.py`; `note_from_ord_reaction` → `record_from_ord_reaction`
- [x] `ingest.py` drops `propose_note`; fingerprint indexing untouched
- [x] `sync.py` drops `_merged_note_bodies` (the O(corpus) scan) and `awaiting_merge`
- [x] `dangling_links` external-id namespace + `cli/validate_kg.py` citation-existence check
- [x] `expand_note` falls back to the store (graph still wins — `reaction-` is a prefix, not a
      reservation); D-018's dangling-citation failure class removed
- [x] Retriever filter resolves against the store
- [x] No Schedule opens a PR: 3 memory schedules removed, observation promotion split out
- [x] Layering: removed both new edges by injecting one-method Protocols, not by declaring them
- [x] Slug validation kept (`require_note_slug` extracted, not copied) — caught by a test, not review
- [x] `tests/test_reaction_records.py` — the 4 claims the change would be wrong without
- [x] ADR + ledger + CLAUDE.md + ARCHITECTURE.md + DEFERRED row

## Review
The elegant version was not the first one. Two things forced it:

1. **The layering test.** Putting the store in `ingest` inverted `ingest → kg` and
   `ingest → retrieval`. The file's own rule ("move the code rather than excuse the edge") gave
   the answer: each consumer declares a one-method Protocol, the caller injects the store, and the
   edge disappears instead of being allowlisted. `FingerprintReactionRetriever`'s `records` is
   required rather than defaulted for the same reason.
2. **A dropped guard.** `ReactionRecord` initially had no slug validation, because a Postgres PK
   does not need one. An existing test failed and was right: the id still becomes the
   `reaction-<id>` citation a campaign note carries into git.

One hazard I introduced and then removed: routing on the `reaction-` prefix *before* the graph made
any human-authored note under that name silently unreachable. Graph first, store second.

## Merge with main (main moved mid-flight)
- [x] Base was `bed7d69`, whose own CI run **failed**; `50cb06f` on main fixed the two mypy errors
      that had kept main red since 2026-08-22. Merged main in rather than waiting.
- [x] Main added `ProcessConditions` frontmatter, read by `condense.py` and `protocol_tools.py`.
      Both sides changed the same mapping, so it is carried rather than picked: the record gains a
      `conditions` JSONB column, and `condense_protocols` gains a record fallback — without it
      every reaction reference would read as `missing`, silently breaking a feature main had just
      shipped.
- [x] Verified the exact CI command: `mypy src examples tests` → clean.

## Not done, deliberately
The chemist's actual insight is still not captured — see the `DEFERRED.md` row. This change makes
the ELN queryable; it does not make it teach.
