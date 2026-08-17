# Verdicts — kg / retrieval / memory (design), reachability lens

In scope: the two findings marked **high**. The three medium/low findings were not examined.

---

## The report path fans out over sources a second time, and its copy has no failure isolation

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

Reproduced the divergence directly (`/tmp/vr/r1.py`, one healthy + one raising retriever through
both paths):

```
ERROR:chemclaw.retrieval.fanout:evidence source 'vector' failed; the sweep continues without it
sweep_sources -> [1, 0]
gather_section RAISED RuntimeError pgvector unreachable
```

Then went after the *trigger list*, because that is where the finding is wrong in both directions.

**One of the three named triggers cannot happen.** `ShareDocumentRetriever.retrieve`
(`src/chemclaw/ingest/documents/retriever.py:150-174`) ends in a blanket
`except Exception: logger.exception(...); return []` — with a comment naming the exact failure it
was added for ("an `openai.APIError` is in neither list above, so a rate-limited embedding endpoint
escaped this leg and failed the whole turn"). The same shape is in
`ingest/eln/warehouse/retriever.py:120`, whose comment cites *`gather` with no `return_exceptions`*
as the reason. So "`ShareDocumentRetriever` on an unreachable share" does not reach `gather_section`
at all, and the codebase's actual convention is per-retriever fail-closed — which the two core
index retrievers and the fingerprint retriever do not follow.

**A trigger the finding did not name is reachable on the *default* deployment.**
`deploy/helm/chemclaw/values.yaml` does not set `CHEMCLAW_DATA_SOURCES`, so the shipped default is
`graph,eln-json` — retrieve sources are `GraphRetriever` (filesystem only) plus the always-appended
`FingerprintReactionRetriever`. `find_similar_reactions` raises `FingerprintError` on a prose query
*before* touching the store (`drfp_bitstring`), so prose sections never hit Postgres. But a section
query that is a reaction SMILES — which `default_retrievers`' own docstring says is a supported case
— does. With Postgres unreachable (`/tmp/vr/r6.py`):

```
query 'what have we tried on the biar' -> 0 chunks
query 'CC(=O)O.CCO>>CC(=O)OCC' -> RAISED ConnectionError: Postgres unreachable at ...
gather_section RAISED ConnectionError Postgres unreachable at ...
```

`ConnectionError` is not in `_BAD_DATA_TYPES`, so it burns all five `activity_max_attempts`, and
`ReportSectionWorkflow` then returns `retrieval_failed=True` — losing the graph leg's hits, which
needed no database at all.

### Why

Mechanism and the headline consequence reproduce exactly as written, and reachability is better
than the finding claimed (default config, not only a hybrid one). What is overstated is the
severity and part of the framing:

1. The failure is **visible and fails closed**: the section renders
   `_Retrieval failed for this section; incomplete — re-run required._`, `retrieval_failed`
   distinguishes it from an empty section, and the chemist is never shown a report that looks
   complete. Nothing wrong is asserted — evidence is lost, not fabricated, and a re-run after the
   outage recovers it. That is an availability regression, not a correctness or safety one.
2. "the durable path gains the per-source counter **and stream event**" is wrong on the second
   half. In a Temporal activity there is no graph runtime, so `stream_writer_or_none()` returns
   `None` and `_report` degrades to `logger.debug` (`core/turn_signals.py:195-197`,
   `fanout.py:123-127`). Only the counter would be gained.
3. The proposed fix trades the defect for a quieter one. Adopting `sweep_sources` means a source
   outage never sets `retrieval_failed` again, so the draft renders as an ordinary supported
   section with a source silently missing — which is the state `SynthesizedSection`'s own
   `retrieval_failed` exists to prevent, and the state the finding quotes `fanout.py` deploring.
   Per-source isolation is right, but it has to come with the section recording *which* sources
   failed; "delete the second fan-out" as written does not.

Medium: real, reachable, worth fixing, but it degrades a durable draft visibly and recoverably.

---

## `GraphRetriever` is the only `SourceRetriever` that returns an unbounded result

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

Built a 2,000-note reaction corpus at `/tmp/vr/corpus` (`/tmp/vr/gen.py`) and ran the real
retriever, the real `gather_section` and the real `report_note` (`/tmp/vr/r2.py`):

```
retrieval_top_k = 8
gather_evidence_max_chunks = 40
cold chunks: 2000 298 ms
warm chunks: 2000 15 ms
chunk JSON bytes: 666890
section evidence: 2000
note body chars: 390950 bullets: 2000
```

Reachability is **worse** than the finding states, because of the widening fallback at
`retrievers.py:176` (`complete or scored`): when no note matches every term, the result is every
note matching *any* term. Ordinary prose section queries therefore return the whole matching corpus
(`/tmp/vr/r3.py`):

```
terms=['reaction'] -> 2000 chunks
terms=['what','have','we','tried','biaryl','step','recently'] -> 2000 chunks
terms=['palladium','catalyst','screening','biaryl','coupling','2026'] -> 2000 chunks
```

Searched for an upstream guard and found none: `Note` has no body-length constraint,
`kg/pr_gate.py` has no size check, and `note_proposals.content` is plain `TEXT`
(`infra/sql/027_note_proposals.sql:29`).

**What the reporter missed, and it makes this worse.** The section evidence crosses the Temporal
wire twice — as `retrieve_section`'s *result*, and then the whole assembled `Report` as
`propose_report`'s *argument* (`report_workflow.py:182`, all sections at once). Against the live
broker on :7233 (`/tmp/vr/r4.py`, `/tmp/vr/r5.py`):

```
500000 bytes -> OK
1500000 bytes -> OK   (WARN TMPRL1103 size=1500034 limit=524288)
2500000 bytes -> WorkflowFailureError
ActivityError caught in workflow: Activity task failed / cause=ServerError: Complete result exceeds size limit.
```

At ~333 bytes/chunk measured, one section passes ~2 MB at roughly 6,000 matching notes, and the
combined `Report` argument passes it much sooner (5 sections × 2,000 notes ≈ 3.3 MB). Past that
point the report path does not merely produce an unreviewable PR — it stops working: sections come
back permanently `retrieval_failed` ("re-run required", which never helps), or `publish_note` raises
`ActivityError` out of `DevelopmentReportWorkflow` and the whole job fails. Already at 2,000 notes
every section trips Temporal's 512 KB warning limit.

### Why

The cited code is exactly as described: `GraphRetriever.retrieve` returns the full sorted list
(`retrievers.py:181-187`) while `VectorRetriever` and `LexicalRetriever` pass
`settings.retrieval_top_k` into the index (`:382`, `:417`), and the graph source is the only
retrieve source enabled by default (`config/sources.py:45`, `data_sources = "graph,eln-json"`,
`eln-json` being ingest-only). The conversational path really is protected by the post-fusion cap
(`gather_evidence_max_chunks = 40`); the durable report path really has no cap anywhere between
`gather_section` and `propose_note`. The trigger is an ordinary prose report section on a corpus of
a size ELN ingest reaches routinely — no private function, no crafted input, no unusual config.
High stands, and the Temporal blob limit converts it from "a 400 KB PR nobody can review" into
"reports stop being producible at all".
