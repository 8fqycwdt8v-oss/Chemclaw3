# D-2026-09-06-the-one-agent-that-exists-is-named-in-the-trail — `audit_events.agent` gets a producer, and the absence test that demanded one becomes the presence test it asked for

**Status:** accepted · **Date:** 2026-09-06 · **Supersedes** the "no producer" half of
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`; its deletion of the
contextvar plumbing stands and is not undone · **Carries out**
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` invariant 3 · **Follows from**
`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`.

## Context

`D-2026-08-26` found `audit_events.agent` empty on every row the trail had ever written, deleted the
contextvar trio that claimed to fill it, kept the column, and pinned an **absence**:
`test_nothing_in_the_tree_writes_the_agent_column`, so that *"whoever re-adds subagents fails this
test … the producer and the claim have to arrive together."* Its reasoning was explicit: there were
no subagents, so nothing could honestly write it.

That reasoning expired three days later.
`D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller` established what this repository's
own CLAUDE.md now opens with: there is **exactly one subagent, on every turn**. Driven through a
real `task` spawn against the real `PostgresAuditSink`, a helper's four tool calls came back:

```
tool=record_knowledge_note   outcome=refused  actor='oid-caller' agent=''
tool=ask_clarifying_question outcome=error    actor='oid-caller' agent=''
tool=write_file              outcome=error    actor='oid-caller' agent=''
tool=read_file               outcome=error    actor='oid-caller' agent=''
tool=task                    outcome=ok       actor='oid-caller' agent=''
```

The first four were made by a graph running on a **model-authored brief the chemist never saw**, and
nothing in the trail told them apart from the chemist's own acts. A reviewer walking
`chemclaw explain` saw five calls by one person in one turn. So the column that was correctly empty
when nothing could fill it was now empty while something could — which is the same defect the
earlier ADR named, one turn of the wheel later.

This is a **reviewability** gap and not an authorization hole: every gate ran on those four calls,
the helper is an attenuation that holds, and the refusal is in the record. The trail simply could
not say who.

## Decision

**Give the column a producer, and make it a build-time argument rather than an ambient read.**

`make_audit_middleware(..., agent="")` carries the value into `_recording`, which stamps it on the
`AuditEvent`. `build_langgraph_agent` passes `prof.name if helper else ""` — taken from the profile
*after* `helper_profile`'s narrowing, so it is the derived `<caller>-helper` name and a caller that
resolved `property-lookup` produces `property-lookup-helper`. Nothing else changes: `audit_store`
already wrote the column, and `AuditEvent`'s shape is untouched.

**A build-time argument, deliberately.** A helper is a separately compiled graph with its own
middleware chain, so the thing that builds the chain is the only thing that needs to know which
graph it is — and it cannot forget in the way a contextvar setter demonstrably can. The failure
`D-2026-08-26` deleted was precisely a setter nobody called; re-introducing an ambient carrier here
would rebuild the shape rather than the control.

**Beside `actor`, never instead of it.** The helper's rows still name the chemist. Overloading
`actor` is the D-040 failure — an agent's self-authorization recorded under a person's identity,
worse than an unrecorded act because it *looks* attributable. `purpose` stays empty; it answers a
different and still unanswerable question.

## Why the absence test has to go, and what replaces it

An absence test is the right assertion for a claim nothing can write. Once something writes it,
keeping the absence would forbid exactly the fix the absence was written to demand — it would have
to be defeated rather than satisfied, which is the worst of both.

So it is replaced by the stronger form of the same demand:
`test_the_trail_names_the_helper_that_made_a_call_and_leaves_the_caller_unnamed` drives a **real
`task` spawn through the compiled graph** and reads the rows off the sink — the helper's `ls` row
carries `default-helper`, the caller's `task` row carries `""`, and every row carries the chemist.
A source scan could only ever see that a keyword was written somewhere; this sees the value reach a
row, and it fails if the argument is dropped anywhere between `build_langgraph_agent` and the sink.
That is what "the producer and the claim arrive together" asks for, satisfied rather than sidestepped.

`test_the_audit_row_leaves_agent_empty_for_the_agent_the_chemist_talks_to` (renamed from
`…records_an_empty_agent…`) keeps the other side: a chain built with no `agent=` records none, which
is what every non-helper caller relies on — `agent/tool_invocation.py`'s template step included.

## Consequences

The trail can now distinguish a helper's act from the chemist's agent's act, which is what makes the
one-subagent design reviewable rather than merely bounded. Rows already in the database are
unaffected: empty stays the caller's value, so `''` continues to read as "the agent the chemist is
talking to", and the only new value in the column is a name that says which graph it came from.

The cost is one argument threaded through one factory. `docs/decisions/` now records the rule in two
places rather than one, which is correct: the invariant is D-2026-08-10's, and this is the commit in
which it acquired a producer.

## What was measured rather than assumed

- The five-row trail above, off a real `task` spawn against the real Postgres sink.
- The new test watched to fail against the unfixed source: `{'ls': '', 'task': ''}` — the finding
  itself — and to pass after the one-line producer, with the caller's row still `''`.
