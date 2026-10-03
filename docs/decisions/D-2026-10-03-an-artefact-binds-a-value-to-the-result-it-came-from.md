# D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from — every tool result carries a handle, and an artefact may bind to it

**Status:** accepted · **Date:** 2026-10-03 · **Owner decision** (2026-10-03). Wave 3 of
`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`, against the frozen wire contract
the frontend builds to. Reverses that ADR's decision 7, which deferred bindings; that ADR is merged
and stands as the record of why they were deferred then.

## Context

`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect` deferred bindings on a measurement:
tables are 2.7% of answer tokens, so the output-token argument was weak, and what was left —
provenance per cell — cost "a handle stamped into *every* tool result's shape". Its trigger named
two conditions, neither of which has been measured as having fired. The owner chose on 2026-10-03
to build them anyway, on the provenance argument alone: a table a chemist will export is worth more
when each cell can say which tool result it was copied from, and a transcription that cannot be
mistyped is worth more than one that is checked afterwards and flagged "unchecked". This record
states what that costs and how it is bounded, since the deferral's whole case was the cost.

## Decision

1. **Every stored tool result ends with one line, `⟨r:<12 hex>⟩`** — the first twelve hex digits of
   the `tool_result_blobs` content hash of the result's *full* text. `bound_tool_results` stores
   every successful result through the turn's sink before the message leaves it (a cut keeps the
   write it already made, `D-2026-09-27-a-cut-result-is-kept-for-the-chemist-not-the-model`) and
   stamps the ref on `response_metadata` (`RESULT_REF_KEY`); `stamp_result_handles`, the outermost
   pass that rewrites a result, appends the line. It lies **outside** the envelope and the defang:
   it is this system's line. Its position alone does not stop a tool writing a handle-shaped line of
   its own — one naming another result of the same conversation — so every `⟨r:` run in a result's
   text is escaped before the stamp is appended, and the stamp is the only bracketed handle the
   model reads. A bare `r:<hex>` a tool writes is not escaped; it binds only within the session, as
   any handle does.
   A failure, an empty result, a result over `stream_max_result_bytes`, and every turn on a driver
   with no sink (the CLI, a template step) carry **no** handle, because a handle that names bytes
   nobody kept is an address a binding would be refused on.
2. **The write moves; it is not added.** The stream used to store the model's copy of each result
   after the node completed. `ToolCallTrace.returned` now reuses the stamped ref, so a result is
   written once, and `ToolResultEvent.result_ref`, the transcript's pairing and the handle name the
   same bytes. For a connector result those bytes are now the tool's own text rather than the framed
   copy, which is what a surface rendering the result wanted anyway.
3. **A binding is `{"$bind": {"result": "r:<hex>", "pointer": "<RFC 6901>"}}`**, accepted where a
   literal cell, property, SMILES or series is, plus a table-wide `rows_from`. It resolves by prefix
   **within the session's `tool_result_links`** — the join that authorizes the fetch route, so bytes
   another conversation produced resolve to nothing — and an ambiguous prefix is refused by name. A
   write resolves every binding or is refused naming the failures; the stored spec keeps the
   binding with the full 64-hex ref, so a later result sharing the prefix cannot re-point it.
4. **A read serves both forms.** `ExhibitView.spec` is resolved, so every renderer, export and cap
   sees values; `raw_spec` is as stored; `bindings[]` lists each with `ok`. A result retention has
   swept reads `null` with `ok: false` and a reason (a `rows_from` reads as no rows), and the rest of
   the artefact still opens. A diff compares stored specs, so a re-resolution is never a revision.
5. **A bound value is grounded by construction** and never an `unverified_figures` entry; the scan
   reads the stored spec and skips bindings.
6. **A person may keep, detach or re-point a binding to any result of the same session**, and may
   not name one outside it — the same resolution, the same refusal.

## What it costs, measured

- **The thread, per result.** The line is 17 characters: **+4 tokens** per result on the
  approximate counter the budgets use (`count_tokens_approximately`, 7 → 11 on a probe result) and
  12 on `cl100k`. Nothing re-bounds after it, so a maximal batch is
  `agent_max_tool_result_chars` plus one line per call: 13,000 → **13,034** estimated tokens at the
  shipped 8 parallel calls. `tests/test_compaction.py::_unreclaimable_batch_tokens` now charges it,
  and the budget's warm arm passes over it (14,181 on this tree against the 13,034 floor).
- **The prefix.** Teaching the form costs `create_exhibit` +75 and `read_exhibit` +20 tokens:
  the default profile 73,027 → **73,122**, under the 73,450 ceiling, so no ceiling, budget or
  `PREFIX_BOUND` moved. The rules for using it live in the `exhibits` skill body, which is loaded on
  demand and is not prefix.
- **A read.** A spec with no binding reads nothing more. One with bindings costs one query for the
  session's links and one for the blobs it names, bounded by `exhibit_max_bound_results` (20)
  distinct results per spec; the JSON parse runs off the event loop above
  `exhibit_binding_offload_bytes`.

## Options considered

- **Keep bindings deferred.** The record's own trigger has not fired. The owner chose to build.
- **Stamp the handle only on JSON results**, or only when artefacts are on. Declined: a model that
  sees a handle on some results and not others has to be taught which, and the line is 4 tokens.
  Revisit when: `tests/test_compaction.py`'s warm arm reads under 13,100 (the margin this record
  spends from), or a measurement over a recorded live lane shows handles above 1% of thread tokens.
- **A handle inside the envelope**, so connector framing carries it. Declined: the defang rewrites
  the inside, and a payload could then spell a handle of its own beside the real one.
- **Resolve on write and store values** (a binding as a provenance note beside a literal). Declined:
  it keeps the transcription a binding exists to remove, and a later detach would have nothing to
  show changed.
- **A JSON Pointer library.** Declined: RFC 6901 is two escapes and an index; one more dependency
  in every image buys nothing one short function and its tests do not.
- **A model-facing tool to fetch a result back by handle.** Declined, for the reason the cut gives:
  it would re-inflate exactly the context the cut and compaction reclaim.
  Revisit when: a recorded lane shows the model re-calling a tool only to recover a value it could
  have bound (an identical call within one turn whose result it then binds).

## What keeps it true

- `tests/test_result_handles.py` — the handle after the closing delimiter, one per block list, none
  on a failure, an empty result or a sinkless driver, the stream reusing the ref with no second
  write, the transcript pairing by the stamp, no grounding reader taking an all-digit handle for a
  figure or an id, and the front door storing once and stamping the thread.
- `tests/test_exhibit_bindings.py` — RFC 6901, the positions a binding may take, literal `null`
  refused, grounding and diff over the stored spec, resolution against real stored results
  (handle → full ref, `rows_from`, series, SMILES), every refusal named, another session's ref,
  an ambiguous prefix, the caps, the off-loop parse, a swept result reading `null`, and the
  agent's tools.
- `tests/test_exhibit_routes.py` — a person keeping, detaching and failing to invent a binding,
  and the diff of each.
- `tests/test_compaction.py::test_the_shipped_budget_leaves_the_thread_what_its_derivation_claims`
  and `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling`.
