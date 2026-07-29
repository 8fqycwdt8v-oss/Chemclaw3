# D-138 — Fifty questions, asked live: the job surface was dead, the trace was blind, and a failed tool was silent

**Status:** accepted · **Context:** a catalogue of 50 questions from a process/analytical development
scientist and their project manager, asked against the running stack (real Anthropic traffic, real
per-user Entra identity, Postgres sessions and audit, Temporal, all six connector bundles) rather
than against the test suite.

Five defects, four of them invisible to a suite that is otherwise thorough. Each is recorded with
the question that exposed it, because the questions are the reason they were found at all.

### 1. Every declared connector job was broken, in production, from the day the seam landed

Q11 asked for the reaction energy of an acetylation. `compute_reaction_energy` failed three times
with `'dict' object has no attribute 'model_dump'`, MAF stopped the tool loop after three
consecutive errors, and the turn ended mid-sentence. The same failure hit `compare_solvents`
(Q13, Q07), `scan_coordinate` (Q12) and `compute_thermochemistry` (Q10). It applies to every job
the manifests declare — `compute_reaction_energy`, `compare_solvents`, `scan_coordinate`,
`sample_conformers`, `compute_interaction_energy`, `start_optimization_campaign`,
`compute_dft_energy` — so the entire durable-compute half of the system was unreachable from a
conversation. Across 50 questions, before the fix: **zero jobs started, ever**.

`build_job_tool` declared its parameter as a generated pydantic model and then called
`model_dump()` on it, under the comment *"Validation has already happened — MAF constructs the
model from the tool call's arguments before the body runs."* It does not. MAF publishes the
model's JSON schema and hands the body the decoded JSON *object*. The tool now validates what it
is given (`params_model.model_validate`), which also repairs the `precondition` hook — it had been
receiving a dict whose attributes it could not read.

**Why the tests could not see it.** `tests/test_connector_jobs.py` has twenty-one tests over this
factory, and its helper builds the model and passes the instance — with a docstring claiming that
is "what MAF does". A test that constructs the argument itself cannot discover that nothing else
does. The three tests added here go through the framework's own dispatcher instead, and the middle
one pins the property that would otherwise be repaired the lazy way: accepting a dict must not mean
forwarding *any* dict, or the declared type would be advertised to the model and enforced nowhere.

### 2. `ToolCallEvent.arguments` was empty on every call ever emitted

The field is documented as "a short argument preview" and is rendered by the UI trace. Across the
first run: 112 tool calls, **0 with arguments**. Not a bug in the sense of a wrong value — the
field could not have carried anything else.

A streamed call does not arrive as one object. The name comes first, on a content whose `arguments`
is still empty; the argument JSON then streams as fragments on contents carrying only the
`call_id`. The extractor read name-and-arguments off a single content, so it matched exactly the
one content that never has arguments and skipped every fragment for want of a name. `_ToolCallTrace`
reassembles the call and emits it once complete — which is also the more truthful moment, since a
tool cannot run before its arguments are.

**This defect survived its own first fix**, which is the part worth remembering. The reassembly was
written, unit-tested against a synthetic stream, deployed — and the live run came back 0/147 again.
The provider opens the argument stream with an *empty* fragment, and the first version read that as
"nothing more is coming" and closed the call immediately. The synthetic stream had been written from
a capture that omitted it. After: **139 of 147** calls carry their arguments; the remainder are
calls that genuinely had none.

### 3. A failing tool was invisible to the person who asked

Q11's turn ended on the model's last words before its final failure — *"Let me try the carboxylic
acid acetylation:"* — with no answer and no error. The failure was in the log, in the audit trail,
and in the model's context. It was in none of the places the chemist can see. `ErrorEvent` was right
to stay silent (the turn had not failed); what was missing was the trace being honest about a step
that did not work.

`ToolFailureSignal` joins the existing turn-signal union and surfaces as a `tool_failed` event.
`announce_tool_failures` is attached innermost, closest to the tool body, so it sees the raw
exception from *every* failure including the two that `surface_authorization_denials` and
`surface_domain_errors` convert into results: what the model is told and what the transcript shows
are separate questions. It observes and re-raises, so audit and both converters behave exactly as
before.

### 4. The graph retriever matched the query verbatim, so ordinary phrasing found nothing

Q26 ("have we run anything like this biaryl coupling before?"), Q43 and Q46 were all answered with
some form of "I need you to tell me which one" — against a corpus whose largest cluster is a Suzuki
biaryl campaign. `gather_evidence("biaryl")` returns the campaign, the compound and the playbook.
`gather_evidence("the biaryl")` returned nothing at all.

`GraphRetriever` tested `query.lower() in note_text(note).lower()` — the whole query as one
substring — so a note had to literally contain the sentence a chemist typed. The docstring warned
about the opposite risk (`ester` matching `polyester`) and never about this one, and with the graph
retriever the only source enabled by default, an empty result is the agent's whole view of the
record. Matching is now per term, all-terms-must-match so precision is unchanged for any query that
already worked, widening to any-term with coverage ranking rather than answering "nothing known".
The stopword list is deliberately fourteen words: it exists to stop `the` from erasing a hit, not to
do linguistics.

### 5. Two instruction gaps the tools could not fix

*Ask-before-search.* Sixteen of fifty answers used **no tool at all**, most of them handing the
chemist a form to fill in for data the system holds or can compute. With defect 4 fixed and an
explicit "look before you ask" rule — search first, resolve names, ask only when the search came
back empty, and answer partially rather than withholding everything — that fell to **ten**, and six
questions that had asked for input now answer from the record.

*The system did not know its own compliance story.* Asked (Q46) what to show an auditor who wants
proof a computed number was not edited, the agent described job ids and re-polling
`get_durable_job_status` — reproducibility, which is a different claim — and never mentioned the
tamper-evident hash chain, the fields it records, or `make audit-verify`. It is the one question in
the catalogue where being confidently wrong has a regulatory cost, so the trail is now described in
the instructions.

### What this says about the test suite

Four of these five were invisible to 1450 passing tests, and the pattern is the same each time: the
test supplied the thing the system was supposed to supply. The job tests built the model MAF was
meant to build. The tool-call test asserted the event type, never its contents. The retriever tests
queried with the exact word the fixture contained. None of that is sloppiness — each is the natural
way to write the test — and none of it can find a defect in the seam between the component and its
real caller. That is what a live catalogue is for, and it is why the three new job tests drive MAF's
dispatcher and the retriever tests query the way a person would rather than the way the fixture was
written.

### Left open, deliberately

*A durable job's domain error does not reach the model.* With the launcher fixed, Q11 launched and
`CalcJobWorkflow` correctly rejected the model's unbalanced equation — the model had written
salicylic acid + Ac2O → aspirin with no acetic acid by-product. The message the chemist needed
("reaction is not atom-balanced (reactants minus products): C +2, H +4, O +2") stayed in the worker
log; the tool raised `WorkflowFailureError: Workflow execution failed`. The check also retried five
times, though no retry can change the outcome. Recorded as VIBE-1 rather than fixed here: relaying
a workflow's failure text to the model is a policy decision about what is safe to surface (the same
question `surface_domain_errors` answers by naming known-safe types), and it wants deciding rather
than patching.

*`resolve_compound` knows solvents and bases, not substrates.* Its table is 87 spellings, almost all
reagents; every substrate in the corpus — 4-bromoanisole, phenylboronic acid, salicylic acid — misses
and the model falls back to its own memory of the structure. It happened to be right each time
observed, which is the problem: a wrong structure propagates silently into every downstream
calculation. Recorded as VIBE-2; the fix touches the connector seam (the graph holds these
structures and the bundle must not import the graph) and is a design question, not an oversight.
