# D-2026-08-27-a-retirement-rides-its-replacement — a supersede is one reviewable decision

## Status

Accepted (2026-08-27).

## Context

The 2026-08-27 knowledge-system review
(`docs/archive/REVIEW-2026-08-27-knowledge-system-analysis.md` §4) traced the memory-synthesis
loop and found the retire/replace pair structurally separable at three points:

1. A retirement and its replacement were independent notes in one flat list, and the per-run
   cap's rotating window could put them in different days' runs — a reviewer could merge "retire
   `campaign-aaa`" while its replacement had not been proposed yet, and the retired note's
   successor line named a note that did not exist. Each half was also its own PR, so even inside
   one run a reviewer could merge one and reject the other.
2. The successor line was prose, not an edge: `superseded-by` could not be a `Relation` because
   the target did not exist on the retirement's own branch, so the graph never carried the one
   pointer a reader following a retired note actually wants.
3. The miners read the corpus through a fetch that could truncate, and a partial read looked
   identical to a corpus in which a merged campaign's reactions had vanished — so a transient
   ELN failure could retire a valid note.

Observation promotion had the same shape plus two of its own: the promotion dedup lived in a
process-local set (reset per pod, so a restart re-proposed everything), and the promotion PR
described its evidence as "merged notes" when D-2026-08-25 made the cited runs unreviewed
transcriptions.

## Decision

**A synthesis run produces `SynthesisUnit`s, not notes.** A unit pairs the new note with the
retired copies of the merged notes it replaces, and the pair travels the fan-out whole: the cap
slices units, the publish activity takes a unit, and the retirements ride the replacement's
submission as `propose_note`'s `superseded` — one branch, one PR, one merge, so neither half can
land without the other.

**Because the pair rides one submission, the retirement points forward with a real edge.**
`retire_note` writes `Relation(rel="superseded-by", to=<successor>)`; it resolves because the
successor is on the same branch.

**A partial corpus read retires nothing.** The builders take `corpus_complete`; when the read
was truncated they still propose what the corpus supports but skip every retirement, logging
why — absence of evidence in a partial read is not evidence of absence.

**Promotion dedup is the store's, and the PR body tells the truth.** The already-promoted set
seeds from `promoted_observations()` rather than process memory, and the promotion summary calls
its evidence "cited runs (unreviewed ELN transcriptions and merged interaction notes)".

**An operator can start a mine directly.** `make synthesize KIND=<kind> [FRESH=1]`
(`cli/synthesize.py`) starts the same workflow the agent tool starts, through the same PR-gate
and the same daily-dedup id; `--fresh` is the one way past the dedup when today's run predates
the corpus change that matters.

## Consequences

- A reviewer sees replacement and retirement as one diff and decides once; the corpus can no
  longer hold a retirement whose successor was never proposed.
- Connector jobs publish through the same activity with an empty retirement list — a job result
  replaces nothing, and the shared path keeps one gate rather than two.
- The flat-list builder API is gone; callers that fanned notes out individually now fan out
  units. `_slice_for_this_run` sorts by the unit's note id, so the cap's rotation is unchanged.
