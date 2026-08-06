# D-2026-08-06-a-flag-is-a-signal-not-an-inventory — A flag is a signal, not an inventory

**Status:** accepted · **Date:** 2026-08-06

## Context

KM-8 exists because retrieval used to hand back two contradictory notes with no marker, and two
notes saying different things read as corroboration. Its stated target in the original gap audit was
"a recency/authority/agreement **signal** on conflicting notes".

What shipped was a scan. `conflicts._suspected` pairs every note against every other note sharing a
`(type, compound_smiles)` whose confidences differ by more than `conflict_confidence_gap` and whose
validity windows overlap. On a 2,000-note corpus spread over 7 substrates — the shape a real
programme has, because an optimization campaign is many runs on one substrate — that measured
**141,156** conflicts, 637 ms of pure pair enumeration, and a `conflicts_with` list of ~141 ids on
every evidence chunk reaching the model.

`D-2026-08-05-one-rule-in-three-places-is-three-rules` cached the computation, which bounds how
*often* that is paid and nothing else. The backlog row that survived it framed the remainder as a
product decision: a per-note cap, a widest-gap rule, or a narrower pairing key, "each changes what
KM-8 shows a chemist".

## Decision

**A note's conflict flag carries its widest disagreements, worst first, capped at
`conflict_max_per_note` (3) — and the number it is not naming.**

The cost and the noise turned out to be the same fact, so one change fixes both. Sorting a group by
confidence puts a note's strongest partners at the two ends, so the walk takes the wider end first
and can **stop** the moment that end falls inside the threshold: nothing further in can beat it.

Re-measured on the same corpus: **141,156 → 5,937 pairs, 637 → 44 ms, 3 ids per chunk instead of
~141.**

## Two things the narrowing must not do

- **Evict what an author stated.** `Conflict.kind` was already documented as load-bearing — a
  `declared` conflict is a fact recorded by a human, a `suspected` one is a heuristic's suggestion.
  `Conflict.severity` pins declared conflicts above every suspected pair regardless of confidence
  gap, so three hedging notes cannot push a stated contradiction off a note's flag. KM-8's
  declared-conflict promise is byte-identical.
- **Truncate silently.** A reader shown three ids and no count concludes there were three, which is
  a *stronger and wronger* claim than the exhaustive list ever made. `NoteConflicts` carries the
  full total beside the capped list, `EvidenceChunk.conflicts_total` carries it to the surfaces, and
  the report renders "(the 3 strongest of 141)". This is the same rule the tool-result number cap
  already follows.

## Why this was smaller than the row implied

The row treated the narrowing as a product decision because it "changes what KM-8 shows a chemist".
Two things in the code made it smaller than that:

- `kind` already split author-stated from heuristic, so the cap has a natural boundary and applies
  to `suspected` alone.
- The gap magnitude was **already computed**, at the line that decides whether to report a pair at
  all. "Widest gap first" needed no new signal — only `max` instead of `append`.

And an unranked exhaustive list was arguably further from KM-8's own stated target than a ranked few
is. A scan that flags a note against every other note on its substrate is a fact about the corpus,
not a signal about the note.

## Consequences

- `conflict_index` returns `dict[str, NoteConflicts]` rather than `dict[str, list[str]]`. Four call
  sites in `retrieval/retrievers.py` and the report renderer changed; nothing else read it.
- `conflict_max_per_note` is the knob, beside the two conflict settings that already existed. `1`
  gives the widest disagreement only; raising it costs context and buys nothing a reader acts on.
- The early stop is invisible in the output — bounding what each note *emits* already bounds the
  flags, so a `break` replaced by a `continue` changes no assertion about conflicts. It is pinned by
  a test that counts *reads* rather than results: rejecting a 500-note group that agrees must cost a
  handful of comparisons, not 500.
