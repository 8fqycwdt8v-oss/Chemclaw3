# Fifty expert questions, asked live (2026-07-28)

**Method.** A catalogue of 50 questions a process/analytical development scientist and the project
manager for the same programme would actually ask, each carrying a *direction* — the shape of an
answer that would satisfy the asker — rather than a key. Real users do not know the answer; they
know what a useful answer looks like, so grading against a direction is a judgement, which is the
point. Every question was asked over the real HTTP/SSE front door by its own signed identity
(`entra_required=true`, so budgets, RBAC and the per-user caps were genuinely in the path), against
live Anthropic traffic, Postgres sessions and audit, a Temporal dev server, and all six connector
bundles. Four questions ran as ordered threads in one session, because "which jobs did you start
earlier?" and "apply that preference from now on" cannot be graded any other way.

**Coverage was deliberately mixed.** 28 questions the tool surface should answer, 12 it can answer
partly, 10 it cannot answer at all. Tailoring every question to the tool list would measure the tool
list, not the system, and would hide the behaviour that matters most: what happens at the edge.
Analytical development is thin on purpose — there is no chromatography, no spectrum prediction and
no calorimetry anywhere in the surface, and a chemist asking for an HPLC method deserves to find
that out honestly.

Full decision record: `docs/decisions/` D-138.

## What the run found

Five defects. Four were invisible to a suite of 1450 passing tests.

| # | Defect | Evidence |
|---|---|---|
| 1 | **Every declared connector job was broken.** `'dict' object has no attribute 'model_dump'` on all seven — the entire durable-compute surface unreachable from a conversation. | Q11, Q13, Q07, Q10, Q12 |
| 2 | **`ToolCallEvent.arguments` was empty on every call ever emitted**, and could not have been anything else. | 112 tool calls, 0 with arguments |
| 3 | **A failing tool was invisible to the asker.** Q11's turn ended mid-sentence with no answer and no error. | Q11 |
| 4 | **The graph retriever matched the query verbatim**, so `the biaryl` found nothing where `biaryl` found three notes. | Q26, Q43, Q46 |
| 5 | **Two instruction gaps**: asking before searching, and not knowing its own audit story. | 16 zero-tool answers; Q46 |

## Measured, before and after

Same 50 questions, same corpus, same model.

| | before | after |
|---|---|---|
| durable jobs started, across all 50 questions | **0** | launches, then the workflow's own domain check runs |
| tool-call events carrying their arguments | **0 / 112** | **139 / 147** |
| tool failures visible to the asker | **0** | every one, as `tool_failed` |
| answers using no tool at all | **16 / 50** | **10 / 50** |
| …of those, questions the surface *covers* | **9** | **4** |
| median turn | 8.4 s | 9.2 s |

Eight questions that had handed the chemist a form to fill in now answer from the record: Q12
(conformers), Q13 (solvent screen), Q16 (hydrogen hazard), Q23 (green metrics), Q31 (campaign), Q39
(report), Q41 (risk register), Q43 (cost).

Q46 is the clearest single change. Asked what to show an auditor who wants proof a computed number
was not edited, the agent had described job ids and re-polling `get_durable_job_status` — which is
reproducibility, a different claim — and never mentioned the hash chain. It now describes what the
trail records, that each entry hashes the previous one, and `make audit-verify`.

## What is still open, and why

Ten answers still use no tool. Six of them are right to: three are the gap questions (Q20 spectra,
Q22 forced degradation, Q32 statistics) where the honest answer is expert prose with its limits
stated, and Q46/Q50 are questions about the system itself. Four remain genuine misses — Q26, Q29,
Q30 and Q49 — where the agent asks for input it could have derived. They are behavioural, not
structural, and the next lever is the eval set rather than more instruction text.

Two defects are recorded rather than fixed, with the reasoning in `docs/planning/BACKLOG.md`: **VIBE-1**, a
durable job's domain error not reaching the model (the actionable "reaction is not atom-balanced:
C +2, H +4, O +2" stays in the worker log), and **VIBE-2**, `resolve_compound` knowing solvents and
bases but none of the substrates the corpus is about. Both touch a seam boundary and want deciding
rather than patching.

## The part worth keeping

Four of the five defects were invisible to a thorough suite, and the pattern is identical each time:
**the test supplied the thing the system was supposed to supply.** The connector-job tests built the
pydantic model that MAF was meant to build — twenty-one tests, and a helper whose docstring claimed
that was "what MAF does". The tool-call test asserted the event's type and never its contents. The
retriever tests queried with the exact word the fixture contained. None of that is sloppiness; each
is the natural way to write the test. But none of it can find a defect in the seam between a
component and its real caller.

Defect 2 makes the same point twice. The reassembly fix was written, unit-tested against a synthetic
stream, deployed — and the live run came back 0/147 again, because the provider opens the argument
stream with an empty fragment that the synthetic capture had omitted and the new code read as "end
of call". A test written from a partial observation reproduces the partial observation. The
regression test now models the empty opening fragment explicitly, and the verification that settled
it was feeding a real agent stream through the accumulator.
