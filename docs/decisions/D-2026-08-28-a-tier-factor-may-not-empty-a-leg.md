# D-2026-08-28-a-tier-factor-may-not-empty-a-leg — the fusion weight is banded, and the kept counter stops crediting list order

**Status:** accepted

Supersedes nothing. It corrects two claims made *about*
`D-2026-08-01-a-cap-that-starves-a-source` by work that came after it: the rank-space weight said
it could not starve a leg, and the counter built to alert on starvation said it could see one.
Both were checked by running them.

## Context

An adversarial pass over `retrieval/`, `kg/` and `memory/` measured what each evidence leg actually
contributes, on the shipped 38-note corpus, with `graph,vector,lexical` enabled against a real
Postgres note index and the offline hash embedder. Ten research questions, both merge modes.
`truncated_by` was `None` on all twenty sweeps — **every hit every leg handed over reached the
answer**, so any number below 100% is an artefact rather than a loss.

### A positive weight can still empty a leg, and the docstring said it could not

`reciprocal_rank_fusion` divides the rank by the weight, so a source at weight `w` fuses its rank-1
hit at *effective* rank `1/w`. `retrieval/hybrid.py` asserted that "no weight can push a source's
rank-1 hit below another source's *tail*, because every source's best hit still scores within one
rank position of every other's", and the config refused only non-positive weights.

Measured over six sources of eight hits each, every other weight at the default 1.0, against the
shipped 40-chunk cap:

| lexical weight | its best hit's fused index (0-based, of 48) | lexical chunks kept |
|---|---|---|
| 1.0   |  1 | 7 |
| 0.5   |  6 | 4 |
| 0.2   | 21 | 1 |
| 0.125 | 36 | 1 |
| 0.1   | 40 | **0** |

At `0.1` the leg's own best hit sits behind all five other sources' complete eight-hit tails and the
cap ends before it, and at `0.1` its best hit fuses at effective rank 10 — nine positions from rank
1, not one. The other direction fails too: at weight `8.0` a source's whole eight-hit list precedes
every other source's rank-1 hit, which is precisely "below another source's tail". So the sentence
condemning the *multiplier* design — "one leg contributing nothing at all, which is the defect
`D-2026-08-01-a-cap-that-starves-a-source` names as this merge design's reason to exist,
reintroduced by the knob meant to tune it" — described the replacement as well, at a value the
config accepted.

### The starved-source counter credits list order, not contribution

`chemclaw_evidence_source_kept_total` counted chunks in the answer by their `retriever` label. Both
merge modes collapse a note two sources found — `_interleave_dedup` keeps the first
`(note, content)` it meets, RRF keeps "the first one encountered across the lists (stable input
order)" — so the survivor carries the *earlier* source's label. On the sweep above, where nothing
was cut:

| mode | handed | kept, as labelled | queries reading exactly 0 |
|---|---|---|---|
| `graph`  | graph 38, vector 73, lexical 73 | 25 / 38 / 25 | 0 of 10 |
| `hybrid` | graph 38, vector 73, lexical 73 | 38 / 42 / **8** | **4 of 10** |

The metric's own registry entry says a leg "contributing 30 and surviving 0 ... is exactly the state
D-2026-08-01 was written about". Here that is also the state of a healthy leg that agrees with an
earlier one, and the ratio the dashboard plots read 0.11 for lexical with nothing discarded.

### The report path booked the denominator and not the numerator

`gather_section` shares `sweep_sources`, so every section increments the pre-merge counter for each
source it asks. Nothing on that path ever called `record_kept_chunks`, and `gather_section` applies
no cap — every hit it is handed reaches the draft. So report traffic pushed `kept / chunks` down for
every source it swept, on the one ratio that is supposed to mean starvation.

## Decision

**`retrieval_source_weights` is banded to `[0.5, 2.0]`, and the band is derived rather than
chosen.** With every weight in `[1/W, W]`, a source's own best hit fuses at effective rank at most
`W` and another source's rank `r` at effective rank at least `r / W`, so another source's chunk can
precede it only while `r <= W²` — at most four chunks per other source at `W = 2`, whatever the
ranks. A sweep of `S` sources therefore keeps every leg's best hit inside any cap above `4 (S - 1)`:
eleven sources at `gather_evidence_max_chunks`'s 40, against the four a deployment runs today. The
ENV comment's own worked example (`{"graph": 1.5, "vector": 0.8}`) is inside the band, and the
default is still the empty map. The refusal lives in the config because the fusion cannot see the
cap, and the non-positive case it replaces fails it as the extreme it always was.

**The kept counter counts a source's own hits whose note reached the answer**, not the chunks
labelled with its name. It is handed the per-source hit-lists and the *unframed* survivors —
`framed` is a positional copy of the merged ranking and `_within_budget` keeps a prefix, so
`ranked[: len(kept)]` is the same evidence with the ids the sources actually returned; comparing a
framed id against a source's own would reintroduce the silent zero for any id `defang` touches. The
ratio stays in `[0, 1]` per source and the sum across sources may now exceed the chunks returned,
deliberately: corroboration is not waste.

**`gather_section` books it too.** One metric, both producers, and the report path keeps everything
it retrieves, so it books kept equal to handed.

## Consequences

- A deployment running `CHEMCLAW_RETRIEVAL_SOURCE_WEIGHTS` outside `[0.5, 2.0]` now fails at
  startup with the arithmetic in the message. No shipped configuration is outside it.
- On the corpus above the kept counter reads 38/38, 73/73 and 73/73 in both modes — which is what a
  sweep that truncated nothing should read, and what it did not read before.
- The dashboard panel (`Evidence: contributed against kept, by source`) is unchanged; what changed
  is that its two series are now the same question asked twice.
- Not addressed, and filed as measured rather than fixed: the PR-gate's per-note cost is not O(1) in
  the branch set earlier proposals left behind — 312 / 316 / 337 / 402 ms by quartile over 200
  consecutive notes against a local bare remote, +29%. The `BACKLOG.md` row carries the numbers.
