# D-2026-08-30-a-review-of-the-review — four findings against the change that replaced the mechanism

## Status

Accepted · 2026-08-30

## Supersedes

Nothing. It **corrects two claims** in
`D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure`, which is merged and therefore
not edited (CLAUDE.md), and fixes one defect that ADR introduced and one coverage gap it created.
The mechanism itself stands.

## Context

Three fresh-context reviewers were run over the merged change, each told to run the code rather
than read the prose, each on a separate dimension: the mechanism, the tests (by mutation), and
every factual claim. They found four things. All four were then reproduced independently before
being accepted — the point of the exercise being that a reviewer's report is also prose.

That is the fifth consecutive review round on this subject to find something, and the shape of what
it found has changed: the first three found *design* defects, this one found **two false claims and
two untested paths**. That is the expected residue of a design that is now right, and it is also
the class of defect this repository is worst at catching, because nothing goes red.

## 1. `_bounded_reason` lost the bound entirely below a three-character budget

`text[-0:]` is the whole string in Python, not the empty one. The binary search added to stop the
tail slice cutting a `repr` escape in half leaves `lo` at `0` when no suffix fits the budget, and
`"…" + repr(text[-0:])` then reprs the **entire** document.

Measured against a 100 kB parse error:

| `agent_audit_max_arg_chars` | returned |
| --- | ---: |
| 0, 1, 2 | **100,024 characters** |
| 3 and above | bounded correctly |

Reachable rather than theoretical: the setting is `Field(default=200, ge=0)` with no floor
anywhere, and `repr` of one character is already three characters wide. The shipped default is 200,
which is why no test and no deployment saw it — and the failure is a *total* loss of the bound at
exactly the tightening an operator would make to be safer, reinstating the 100 kB WARNING line and
the log-forging surface the function exists to prevent.

Fixed by answering the no-room case explicitly rather than leaving it to the slice; the ellipsis
alone is the honest answer, because the budget says there is no room and something was still cut.

**The rule:** a fix for an off-by-one at one end of a range is not evidence about the other end. The
escape-cutting bug lived at large inputs; the fix for it created a worse bug at small budgets, and
the test written with it exercised only the budget the defect had been found at.

## 2. The promotion's synchronous hook was never driven

`PromoteInvalidToolCalls` declares `wrap_model_call` and `awrap_model_call`, deliberately —
`create_agent` puts a middleware declaring either into **both** chains, so an async-only promotion
is green under `ainvoke` and silently promotes nothing under `invoke`.

Gutting the sync method to `return handler(request)` left **all 137 tests across the seven files
this mechanism touches green**. Every graph-driven test goes through `astream`; every hook-level
test calls `awrap_model_call` directly.

This is a coverage *regression* rather than an inherited gap: the design being replaced had exactly
this test (`test_the_sync_path_announces_what_the_async_path_announces`), and it was deleted with
the mechanism instead of re-pointed at its replacement. Sibling middlewares in this tree
(`RecordContextCompaction`, `MeasureRequestPrefix`) do have theirs.

The new test asserts the sync result *equals* the async one rather than asserting a property of its
own, so the two cannot drift apart.

**The rule:** when a mechanism is replaced, its predecessor's tests are a checklist of properties
somebody already thought were worth holding. Deleting one is a decision, and it needs the same
argument as deleting the code.

## 3. "Innermost of the governance chain" was false, and the truth is worth stating

The claim appeared in four places (the ADR, two docstrings in `model_calls.py`, the comment on the
chain entry itself, and a test module docstring). `tool_governance_middleware` appends
`enforce_plan_approval` and `stamp_plan_link` **below** `refuse_unparsed_arguments` whenever a
profile enables the harness. Measured on a `plan_only` profile:

```
… refuse_repeated_calls, refuse_unparsed_arguments, enforce_plan_approval, stamp_plan_link
```

The comment two lines further down — "Innermost, and deliberately after the gate", on
`stamp_plan_link` — contradicted the claim directly, in the same list.

It survived review because both tests that pin this chain (`tests/test_middleware_order.py`,
`tests/test_profiles.py`) build it under profiles that attach *neither* of those two entries, so
the one configuration that falsifies "innermost" was the one neither could see.

**The consequence is real and is now deliberate rather than accidental:** `refuse_unparsed_arguments`
raises before calling its handler, so **the plan gate never sees a promoted call**. That is the
right outcome — arguments that did not parse are not a well-formed request for a gate to decide
about, and the turn correctly reports a fault (`reason=None`) rather than a refusal nothing made —
but it was an emergent property of a list nobody had read under the profile that produces it. It is
now asserted, so a future change that wants the plan gate to refuse malformed calls has to say so.

The replacement wording is a *relation*, not an index: every gate that decides sits outside the
guard. That survives a deliberate reordering, which "innermost" did not.

## 4. "~20 lines replace ~180" was never counted

Measured with docstrings, comments and blank lines stripped (AST-based, over the nine superseded
functions against the four that replace them): **113 old, 42 new — 2.7x.** Including docstrings:
252 against 125, 2.0x. Raw diff hunks: 268 removed, 132 added.

No counting method approaches the 9x the figure claims. The "180" is loosely near the
docstring-inclusive old count and the "20" is near nothing; they were paired from memory.

This is the third round in a row in which this author published a number as a property without
measuring it, and the second in which the correction is recorded rather than quietly applied. The
previous ADR corrected an unreproducible `841 kB`; `tasks/lessons.md` already carries the rule.
The figure is corrected in the module docstring, the lessons log and the ledger row; the merged ADR
keeps its text and this supersedes that sentence in it.

## What the reviewers confirmed sound

Recorded because a review that only lists faults is not evidence about the rest: `_promote` is
idempotent and survives a missing `id`, a missing `name` and a `None` `tool_calls`; in-place
mutation sticks on both `AIMessage` and `AIMessageChunk`; the sentinel guard does **not** misfire on
a nested dict that happens to contain the key; `refuse_unparsed_arguments` runs before LangGraph's
own schema validation, so the sentinel is load-bearing; `parse_partial_json` behaves exactly as the
ADR describes on all four documents it names; the "11 of 54" count is exact; all ten deleted symbols
are gone; and **19 of 20 mutations** across `model_calls.py`, `evals/live.py`, `runner.py`,
`graph_stream.py` and the chain ordering were caught by the tests that claim to cover them.

## The rule to carry

**A review round that finds only false claims and untested paths is the signal that a design has
settled.** The first three rounds on this subject found defects in the mechanism; this one found
nothing wrong with what the code *does* and four things wrong with what it *says about itself* or
never exercised. Those are the cheapest defects to introduce and the most expensive to find later,
because nothing goes red — which is the argument for keeping the fresh-context review going one
round past the point where it feels finished.
