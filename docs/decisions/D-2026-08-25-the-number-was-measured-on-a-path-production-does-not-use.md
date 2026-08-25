# D-2026-08-25-the-number-was-measured-on-a-path-production-does-not-use — the condenser's saving, corrected

**Status:** accepted · **Date:** 2026-08-25 · Corrects the **Measured** section of
[`D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool`](D-2026-08-25-a-summarizer-in-the-thread-and-a-condenser-behind-a-tool.md).
That ADR's decision stands unchanged; its numbers do not.

## Context

The condenser was justified by a measured saving against `expand_note` once per protocol, and the
figure was taken with `model_dump_json()` after `Condensation.rows` was given `Field(exclude=True)`.

Neither describes what a model receives. `langchain_core.tools.base._stringify` prefers
`json.dumps(content)` and falls back to `str(content)` when that raises — as it does for every
pydantic `BaseModel`. So a structured tool return reaches the model as pydantic's **repr**, which
ignores `exclude`.

Measured off a compiled graph, the `ToolMessage` content was:

```
table='' rows=[] complete=True oversized=[] degraded=[]
```

`rows` present, and with real content every `ProtocolDigest` field spelled out beside the table that
already renders it. The commit *"Do not send the comparison twice"* claimed a fix that never took
effect.

## The corrected figures

On a 2.8 kB protocol fixture, against `expand_note` once per protocol, in approximate tokens:

| N | `expand_note` × N | as claimed (`model_dump_json`) | as sent (repr) | now (rendered) |
|---|---|---|---|---|
| 5 | 2,490 | 472 · 5.3× | 987 · **2.5×** | 506 · **4.9×** |
| 20 | 9,970 | 1,648 · 6.0× | 3,712 · **2.7×** | 1,679 · **5.9×** |
| 80 | 39,890 | 6,352 · 6.3× | 14,611 · **2.7×** | 6,368 · **6.3×** |

The middle column is what shipped. The claimed size was right; what was wrong is that it was not
what got sent.

The superseded ADR's second table — protocols fitting the 100,000-token budget, 137 → 1,455 and
49 → 1,455 — was computed the same way and is wrong by the same factor for the same reason. The
property it was drawn to show survives and is the one worth keeping: **the condensation's marginal
cost per protocol is independent of protocol size**, because a digest row is bounded. That is what
makes the budget bound how many protocols a turn can hold rather than how long they are.

## Decision

**The tool renders a string; it does not hand over a model.** Not because a string reads better, but
because the payload then stops being chosen by a fallback path in somebody else's library — the
thing measured and the thing sent become one object. `Condensation` remains `agent/condense.py`'s
return type, so tests and any programmatic caller keep the structure; `render()` is the boundary,
and `agent/protocol_tools.condense_protocols` returns it.

Verified on the production path: a `str` return arrives with real newlines rather than
JSON-escaped, so `_format_output` short-circuits strings before `_stringify` can quote them.

**`exclude=True` is removed rather than left in place**, with a comment where someone would
otherwise add it back. A field annotation that documents a behaviour it does not cause is the shape
`D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has` is about.

**The assumption is pinned upstream.** `tests/test_upstream_surface.py` now asserts that a pydantic
tool return still stringifies as repr, in the absence form that file uses — so if `_stringify` gains
pydantic support, every tool's payload changing shape turns a test red instead of happening
silently. That this repository already made one design decision against the wrong belief about this
is the argument for the assertion.

## Consequences

- The saving is **6.3×** at 80 protocols and the number is now taken from a `ToolMessage` the
  compiled graph built. Three tests read the payload that way rather than choosing a serializer,
  which is the mistake they exist for.
- The honesty fields are rendered as prose. `complete`'s meaning cannot be recovered from a bare
  `True`: it says every reference *you passed* was read, never that you have seen every protocol on
  file. The oversize refusal names the document and says to open it with `expand_note` — the one
  place this artifact sends a chemist elsewhere, so it cannot live on an attribute the model never
  sees.
- **Every other structured tool in this tree still reaches the model as repr.** That is
  pre-existing, unaffected by this change, and a `BACKLOG.md` row rather than a repo-wide payload
  change made in passing.

## Why this is a new ADR rather than an edit

`CLAUDE.md`: never edit a merged ADR. The usual case is a decision that changed; this is a
measurement that was wrong, which is if anything the stronger reason to leave the original standing
— the earlier ADR is the record of what was believed when the decision was taken, and a quietly
corrected number would erase exactly the thing worth learning from.
