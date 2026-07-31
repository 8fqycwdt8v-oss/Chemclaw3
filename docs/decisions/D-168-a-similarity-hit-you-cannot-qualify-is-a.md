# D-168 — A similarity hit you cannot qualify is a similarity hit you cannot use

## Status

Accepted. Implements W2.5 of the dataflow review's plan, the last item of W2.

## Context

`gather_evidence` runs several retrievers over one query and fuses their results. Every
graph-backed one — graph, dense, lexical — passes its `filters` through `_eligible_notes`, the
single gate that applies `type`/`tag`/`since`/`until` and the currency check.

`FingerprintReactionRetriever` took the same `filters` argument and ignored it completely. So a
sweep narrowed to one campaign's notes still got the ten nearest structural neighbours, whatever
they were: the one retriever whose hits are *precedents* — the ones a chemist most wants scoped to
a project, a step, or a fortnight — was the one that could not be scoped at all. "Similar
reactions, but only on this step" had no answer, and nothing said so; the unnarrowed hits arrived
beside narrowed ones and looked the same.

## Decision

**The retriever applies the note filters, by searching deeper than the page it returns and
narrowing the neighbours.**

### The filter cannot go into the index, so it goes after it — but before truncation

`reaction_fingerprints` holds an id, a label and a bit vector. It has no note type, no tags, no
dates, and giving it any would duplicate the corpus into a derived index that then has to be kept
in step with it. So the filter necessarily runs in the retriever, on what came back.

Which makes *when* it runs the whole decision. Applying it to the returned page — ask for ten,
drop the ones that do not match — means every unwanted neighbour costs a wanted one, and produces
a result that gets **worse as the index gets better**: the closer the top neighbours cluster, the
more of the page one campaign's near-duplicates occupy, and the fewer filtered hits survive. So
the search asks for `fingerprint_top_k × retrieval_filter_overfetch` neighbours, narrows those, and
truncates to the page.

The over-fetch is bounded by `fingerprint_max_top_k`, the same ceiling clamped onto every other
caller of the index. Searching deeper may not become a way around the one cap on how much of the
index a single query pulls into memory.

### Only a filtered call changes behaviour

With no filter the retriever does exactly what it did: same `top_k`, no corpus read, and the
pending-note citation intact. That last one is load-bearing and easy to break. The fingerprint
index is written at ingestion while the note is merged separately (D-018), so a hit whose note is
still in review yields a reference `kg-validate` flags on the report PR — the PR-gate working, on
purpose. Dropping every hit whose note is not on disk would have deleted that behaviour as a side
effect of adding a filter nobody had asked to apply.

### A filtered call *does* drop a hit whose note is missing

This is the one place the pending-note rule does not hold, and deliberately. A filter says "only
notes that are X"; a note nobody can read cannot be *shown* to be X, so serving it would answer a
narrowed question with an unnarrowed hit. Same rule `_in_window` already applies to an undated
note, for the same reason.

### The exhausted-search case is logged, not swallowed

If the deeper search is itself exhausted and still does not fill a page, matching reactions may
exist further down the ranking that were never looked at. That is logged with the knob to raise —
the repo's "no silent caps" rule. A short list that reads as "this is all there is" is the failure
this whole item exists to remove.

## Consequences

- The plan said to "resolve through `note_index`". That is not possible: `NoteIndex` stores a note
  id, its text and an embedding — no type, no tags, no dates. The eligibility gate that parses the
  corpus is the only thing that can answer the question, and it is what the other retrievers use,
  so this shares it rather than adding a second definition of "eligible".
- **The MCP `similar_reactions` tool does not get this**, and cannot. It lives in the `rxnfp`
  connector bundle, and a connector must not import the knowledge graph (D-115) — reading note
  metadata there is exactly the coupling that rule exists to prevent. The filter belongs on the
  retriever because the retriever is in core, where the graph is legitimately readable. An agent
  that needs qualified precedents reaches them through `gather_evidence`.
- A filtered structural search now parses the corpus, where before it touched only Postgres. It
  reads through the same cache the graph retriever's every call already uses, so on a warm query it
  is a stat scan — and an unfiltered search still does not touch it at all.
- `FingerprintReactionRetriever` takes an optional `notes_dir`, matching `GraphRetriever`. Both
  production construction sites (`agent/research_tools.py`, `durable/report_workflow.py`) keep the
  configured default.
- The retriever still does not filter on *yield* or *reagent*, which the review's example asked
  for. Those live inside a reaction note's body, not in its frontmatter, so nothing can filter on
  them without parsing prose. Tagging at ingestion is the real answer and belongs to the ELN
  adapter, not here.
