# D-2026-09-15-an-answer-days-later-is-answered-against-a-corpus-that-moved — a durable wait carries what it rests on, and an answer is refused once that has gone

**Status:** accepted · **Date:** 2026-09-15 · Extends `durable/awaiting.py` and the answer route
without changing either's contract. Does not supersede
`D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again`; it adds a refusal *before*
the signal, where that ADR changed what happens to a previous cycle's answer.

## Context

From the Paperclip evaluation (`tasks/paperclip-ideas-2026-09-15.md`): a decision record there
"tracks whether affected issues have changed since the proposal" before applying an answer. This
tree had no equivalent, and the window is not small — `awaiting_max_days` is 90, and the BO case in
`connectors/bo/workflows.py` deliberately waits a week for plates.

So "approve running these eight conditions" could be answered on Friday against a recommendation
that `memory/supersede.retire_note` closed on Tuesday, and `AwaitAnswerWorkflow` would release
exactly as though nothing had changed. `Answer.payload` is opaque to the workflow by design, and
the front door validated only *who* may answer, never *whether the question still stood*.

## What the corpus can and cannot answer

The obvious design — fingerprint the premise at ask time, recompute at answer time, compare — was
built on paper and abandoned for two measured reasons.

**1. There is no arrival signal for a note anywhere in this repository.** `valid_from`/`valid_to`
are *valid* time at day granularity; `is_current(as_of)` is the only currency test; `conflict_index`
answers "does this disagree with something now". Nothing takes a `since`. A note retired before the
question was asked reads identically to one retired since.

**2. A fingerprint would compare two readings taken on two pods.** The knowledge corpus is a git
checkout refreshed by a sidecar (`deploy/knowledge-sync.sh`), so two API replicas are routinely
minutes apart — `kg/graph.corpus_revision` exists precisely because that is the only fact two pods
holding differently-aged clones share. A sync landing between the ask and the answer would read as
a change that never happened, and the refusal would be a false one.

## Decision

**Ask the narrower question — "does the premise still hold?" — at both ends.**

- `AwaitRequest.premise_note_ids` carries the notes the question rests on, stored on the row
  (`infra/sql/101_pending_request_premise.sql`, `TEXT[]`).
- `agent/pending_tools.request_external_input` **refuses to open** a wait whose premise is already
  broken, before the broker is touched.
- `api/routes/pending.answer_pending` **refuses the answer** with 409 once any premise note has
  been superseded, refuted or removed.

**The ask-time refusal is what makes the answer-time check mean "since".** Because every wait that
exists began with a whole premise, a break found at answer time is a change since the ask —
established by construction rather than by a comparison the corpus cannot support. That is the
whole argument, and it is why `test_a_question_on_retired_knowledge_is_refused_at_the_ask` is
load-bearing for the *other* test's claim rather than a separate nicety.

### The premise is derived, never an argument

`cited_ids(subject + rationale)` — the `[[wikilinks]]` the question already writes. A
`premise_note_ids` parameter would be a control whose producer is a model remembering to populate
it, which is the `map_to_hpc_identity` shape this repository has now deleted twice: a claim that a
check exists. `test_the_premise_is_derived_from_what_the_question_cites` asserts the parameter's
*absence* from the tool signature, so re-adding one fails.

**A question citing nothing carries an empty premise and the check is a no-op.** Stated rather than
hidden: this control covers exactly the questions that say what they rest on. The empty case also
returns without touching the corpus at all, which is what keeps it free rather than merely cheap —
`build_graph` is 1,223.8 ms inline on a 2,000-note corpus, and most questions cite nothing.

### Only retirement and absence, not suspected conflict

`kg/conflicts.py` mixes *declared* disagreement with `_suspected` heuristics. A heuristic that
refuses a chemist's approval has authority it has not earned. A refutation its reporter judged
decisive already closes the note it refutes (`close_refuted_note` sets `valid_to`), so the case that
matters arrives here as an ordinary retirement.

`note_in`, not `note_id in graph`: `_assemble_graph` mints a bare node for every cited-but-undefined
id, so membership is `True` for exactly the ids that resolve to nothing — the failure mode inverted.
`test_an_id_that_resolves_to_nothing_breaks_the_premise` is that distinction.

### The check is in the route, and that placement is the whole reason it was cheap

A workflow cannot read the corpus — it is deterministic and replayed — so checking there would mean
a new activity, which changes the command sequence and needs a `workflow.patched` guard
(`D-2026-09-14-the-seam-shipped-a-replay-break-and-the-adr-said-nothing-changes`), *and* a new
outcome state, which `pending_requests_state_known` would have to be widened to admit. The route
already has the authenticated caller, the stored row, permission to do I/O, and a reply channel to
refuse on.

**Nothing here is a replay break.** `AwaitRequest` gains a field with a default, so a payload in an
existing history deserializes unchanged; no activity is added, removed or reordered; and
`open_pending_request_activity`'s own argument model is untouched. The new parameter is on
`pending_store.open_request`, which is inside an activity rather than a workflow command.

### A moved premise is not an ending

The request is left `waiting`. Somebody who re-reads the current evidence can still answer it, and
the deadline still expires on its own. Settling it would destroy a question nobody decided.

The re-ask path refreshes the premise (`premise_note_ids = EXCLUDED.premise_note_ids`), for the same
reason `requested_by` and `session_id` are refreshed and with a sharper edge: keeping the previous
cycle's premise would check an answer against notes this question never rested on, and where the old
cycle cited a since-retired note, would refuse every answer to a question whose own premise is whole.

## What this does not do

- **It does not detect a note whose *content* changed** without a retirement. A note edited in place
  still reads as current. `note_file_fingerprints` could see that, and is the thing whose cross-pod
  comparison problem is described above.
- **It does not cover a question that cites nothing.** See above.
- **A replica whose checkout is behind can miss a retirement** — a false negative, inherent to an
  eventually-consistent corpus. The failure direction is the safer one: a stale replica admits an
  answer it would later refuse, rather than refusing one it should admit.
