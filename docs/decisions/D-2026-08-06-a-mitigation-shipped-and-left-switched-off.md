# D-2026-08-06-a-mitigation-shipped-and-left-switched-off — A mitigation shipped, and left switched off

**Status:** accepted · **Date:** 2026-08-06

## Context

Four open rows from the security sweep's framing lane: `frame_untrusted` is applied at five call
sites, and four sources reach the model unframed — `find_past_jobs`'s cross-user job reason, every
connector/MCP tool result, `recall_observations`'s corpus-mined statement, and the `source` that
travels beside a `gather_evidence` chunk whose `content` *is* framed.

The lane deferred them deliberately, recording the reason: the envelope wraps a prose *string* while
each of these returns a structured model, "so covering them is a decision about which fields to wrap
without corrupting the shape the model reads — a design question, not a mechanical fix."

Reading the five existing call sites first changed what that question is, and looking for the
mechanism they depend on turned up a larger defect than any of the four rows.

## Decision

### The design question was smaller than it looked, because the pattern already existed

Every one of the five call sites frames **a named free-text field of a structured result**:
`chunk.content` inside a `model_copy(update=...)`, `note.body` inside a note envelope,
`attachment.text`, a job summary. Three of the four open rows are that same pattern applied to one
more field each. There was no shape to decide — only fields to name.

### Two treatments, chosen by the value's shape rather than by its source

An **envelope** is right for a *sentence* that has to reach the model intact and be read as data: a
job's `rationale` (another chemist's words, from another turn), an `Observation.statement` (mined by
a durable job and reviewed by nobody — D-161's ungated tier), an artifact's text.

It is the wrong tool for `EvidenceChunk.source`. That is a provenance label, and on an ELN note its
value is `eln-json:<entry id>:<operator>` — both segments straight from the export, so chosen by
whoever wrote the entry. Wrapping a label in a two-line envelope triples its cost and still leaves it
a string the model could read as prose. An identifier only has to be recognisable, so the stronger
move is available: reduce it to a charset an instruction cannot survive. `frame_untrusted` has always
applied exactly that to the envelope's own `id` attribute, against the same threat; `safe_identifier`
makes it reusable.

`JobRecordSummary.summary` is deliberately left bare, and that is pinned too: it is written by the
bundle's own code from a typed result. A marker applied to our own output dilutes what the envelope
tells the model it means.

### The connector row is one tool, not a surface — and framing it exposed the real defect

"No connector/MCP tool result is ever framed" reads as the widest of the four. Checking which
connector results actually carry text nobody here authored: `fetch_artifact` returns a file a
pipeline wrote on a cluster. Everything else is a number, a key or a name the bundle computed —
molfp/rxnfp hits, bo candidates, `find_calculations` metadata, safety explanations quoting our own
cited `rules.yaml`. ELN text already arrives framed, because it reaches the model as a *note*.

So it is framed in the connector that produces it, since core receives a connector result through a
generic MCP boundary and cannot know which field of an arbitrary payload is untrusted. That is only
correct if the envelope tag is the same in both processes — and it was not.

### The larger finding: the tag was per-process in every shipped deployment

`D-2026-08-06-an-envelope-that-only-survives-its-own-process` made the nonce deployment-stable
because a durable session outlives the process that framed it. `framing_envelope_secret` is that
knob. **The chart never set it.** Measured across two real interpreters:

```
process A tag: retrieved-note-166f67d33a3c3370      # without the secret
process B tag: retrieved-note-b4991f2cca4f7ac1
process A tag: retrieved-note-4f8e682d7b5a39db      # with it
process B tag: retrieved-note-4f8e682d7b5a39db
```

The shipped chart runs `session_store: postgres` behind up to six front-door replicas. Content framed
by one pod was therefore replayed by another as ordinary text — and the agent instructions say
*only* an envelope with exactly the current tag marks retrieved data, so those spans are not merely
unrecognised, the model is told to read them as content. The fix shipped and stayed switched off.

`framing.py`'s docstring compounded it by asserting a guard that did not exist: *"`Settings` warns
when durable sessions are configured without it, because that is the exact combination where
envelopes orphan."* Nothing warned. That sentence is now true.

## Consequences

- A ninth plain secret, `framingEnvelopeSecret`. It is a secret for the reason a nonce is: the
  envelope holds only while the author of a note body or an ELN field cannot *guess* the tag.
  Absent degrades rather than breaks, which is why `Settings` warns instead of refusing.
- `fetch_artifact` returns framed text. A caller quoting an artifact verbatim now quotes it out of
  an envelope, which is the same shape `gather_evidence` and `expand_note` already hand the model.
- Every one of the four sites is **mutation-proven through its own tool**, and that mattered: the
  first version of two of these tests called `frame_untrusted` directly and passed against the
  unframed tool. That is this repository's most-recorded defect — a test supplying the thing the
  system was supposed to supply — caught here by running the mutation rather than by reading.

## Alternatives rejected

- **A blanket framing of every connector result.** Would wrap the numbers, keys and enumerations the
  model must read as data, and teach it that the envelope marks "output" rather than "text nobody
  here authored". One tool earns it today.
- **A manifest declaration naming each tool's untrusted fields** (the shape
  `endpoint.privileged` took for the write gate). Right if there were a second caller; with one it
  is an abstraction ahead of its trigger. The trigger is recorded: a second connector tool returning
  externally-authored text.
- **Framing connector results in core instead.** Core cannot know which field of an arbitrary
  payload is untrusted without exactly that declaration, so this is the same alternative one step
  removed.
- **Wrapping `EvidenceChunk.source` in an envelope.** Costs more and buys less than reducing it: the
  reduction removes the capability rather than marking it, and a provenance label loses nothing by
  being restricted to an identifier's charset.
