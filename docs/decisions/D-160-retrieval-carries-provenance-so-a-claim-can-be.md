# D-160 — Retrieval carries provenance, so a claim can be qualified by who authored its evidence

## Status

Accepted. Implements W3.1 of the dataflow review's plan. A hard prerequisite for W3.3 (D-161): the
observations tier cannot ship before this without introducing the bug described below.

## Context

`Note` has carried `created_by`, `source` and `confidence` since the schema was written, and
`NoteRef` has surfaced all three to `find_notes` and `expand_note` since KM-6 — precisely so the
agent can weigh a source without a second lookup.

`EvidenceChunk` carried none of them. It held `content`, `source_note_id`, `retriever`, `score` and
`conflicts_with`, and `gather_evidence` is the sweep that gathers most of the evidence an answer is
actually built on. So the one path the agent is instructed to reach for first was the one path that
stripped authorship.

`confidence` did reach the chunk, as `score` — a truncation-order signal. That is not the same
thing. Being ranked lower means a note survives a cut less often; it does not tell the model the
note's own author said they were unsure. A 0.2-confidence playbook and a measured reaction record
arrived as two sentences of equal standing, and the answer built on them read the same either way.

Today that is harmless: everything readable was human-merged through the PR-gate, so every chunk
had the same provenance and losing it lost nothing. **It becomes a correctness bug the moment a
second tier exists** — which is exactly what W3.3 proposes. Hence: first, and on its own.

## Decision

**`EvidenceChunk` gains `created_by`, `source` and `confidence`, populated from the note by every
note-backed retriever, and the answer contract is taught to qualify a claim by them.**

### `created_by` defaults to `""`, not `"human"`

A structural hit's content is a Tanimoto score the retriever composed, not a sentence anyone wrote,
so there is no author to report. Defaulting to `"human"` would put an unchecked claim into the one
field whose entire purpose is to be trusted — the failure this ADR exists to prevent, introduced by
its own default. Empty means "the retriever could not establish this", and the instructions say so
explicitly, because a model shown `created_by: ""` will otherwise fill the gap with the common case.

### One builder, so the two paths cannot drift

`_chunk_for(note, retriever, score, conflicts)` is now the only place a note becomes a chunk; the
graph retriever and `_chunks_from_hits` (dense + lexical) both go through it. A sweep fuses those
retrievers into one list, so a partially-provenanced list is the worst of the three possible
states: an agent-authored note would then be qualified or not depending on which retriever happened
to surface it, and from the model's side that is indistinguishable from a note that has no author.

### Provenance is a qualifier, never a filter

Retrieval does not drop a low-confidence or agent-authored note. It was merged — a human signed it
off — and retrieval has no basis for overruling that. This is the rule `conflicts_with` already
established: flag, do not silently decide for the reader. The instructions say "never suppress —
qualify", because the model's natural move on being told a source is weak is to omit it, which
converts "we have a weak indication" into "we have nothing".

### Framing stays at the call site

`tests/test_layering.py` forbids `chemclaw.retrieval` from importing `chemclaw.agent`, including
`agent.framing`. Only the *data* moves here; the `<retrieved-note>` envelope stays where
`gather_evidence` applies it. That constraint is right on its own terms — a retriever should not
know what a prompt looks like — and it is why this change is three fields and a builder rather than
a rendering change.

## Consequences

- The evidence sweep now says who wrote each chunk, so "a distilled playbook note suggests…" and
  "the ELN records…" become different sentences. They were the same sentence before.
- The instruction added to `_INSTRUCTIONS` is the load-bearing half. The fields alone change
  nothing: a model that is not told what `created_by: agent` means will treat it as metadata and
  ignore it.
- W3.3 can now put an ungated tier behind a retriever without that tier's claims becoming
  indistinguishable from merged knowledge. It still gets its own labelled bucket (that is a
  separate rule, for a separate reason), but the per-chunk provenance is what makes a mistake
  there *recoverable* rather than silent.
- `score` keeps its meaning — ranking within a sweep — and `confidence` is now reported
  independently. They coincide for graph hits and diverge for structural ones, which is correct
  and was previously unrepresentable.
- Every existing chunk-construction site outside the retrievers (tests, the report harness's own
  fixtures) still constructs valid chunks: all three fields default.
