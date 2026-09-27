# D-2026-09-14-a-label-only-a-human-can-read-is-not-a-label — the 46 (query, note) pairs become data

**Status**: accepted

## Context

`data/evals/probes/knowledge.yaml` has carried a labelled gold set against the **product** corpus
since it was written: probes whose `direction:` prose names the `knowledge/` notes a correct answer's
retrieval should have reached. Nothing could read them. `Probe` is `extra="forbid"`, so there was no
field to put them in, and a pair written into prose is a pair only a human grader can score.

The consequence was recorded in the wrong place and in the wrong words. `DEFERRED.md`'s
live-retriever-drift row ended "the shipped graph has none", was corrected on 2026-09-05 to
"unreadable as data because `Probe` is `extra='forbid'`", and the correction was still a statement
about a gap nobody had closed.

Measured while closing it: **46 pairs across 20 probes**, not the 44 across 19 the earlier
measurement recorded. The two extra are real — `kn-05` names both `playbook-degassing` and the
retired `playbook-degassing-old`, because the probe is about telling them apart.

## Decision

`Probe.expects_notes: list[str]`, transcribed from the prose that already named them, scored in
`evals/live.py` beside `expects_tools` off the `returned_ids` the runner already accumulates, and
reported by `make live-probes` as mean recall with the probes that missed something named.

**All-of, where `expects_tools` is any-of, and the difference is the question each asks.** Several
tools can legitimately serve one question, so demanding a specific one grades the model's routing
taste. A note is not interchangeable with another note: `kn-05` expects the current playbook *and*
the retired one, and an any-of satisfied by either would score that probe's failure as a pass.

**Scored off `returned_ids`, not off the answer's citations.** Whether *retrieval* reached the note
is what a gold set grades; whether the answer then cited what it was handed is `uncited_note_ids`'
question. One number covering both would report a retrieval failure and a citation failure as the
same thing.

**Scored live, not offline, and that was already measured.** Running `GraphRetriever` on each
probe's raw question offline gives mean recall 0.636 with two probes at 0.00 — below any sane floor
on day one, because a probe question is conversational chemist prose while the live agent
reformulates before calling `gather_evidence`. What CI gets instead is the half it can run: every
`expects_notes` id resolves to a note the corpus holds.

## What was measured

Driven against the live lane with the shipped corpus bound
(`CHEMCLAW_NOTE_REPO_DIR` at the checkout, 40 notes indexed):

| arm | `kn-01` recall |
| --- | --- |
| shipped corpus | **1.0**, nothing missing |
| `knowledge/reaction/rxn-suzuki-biaryl.md` removed | **0.667**, `['rxn-suzuki-biaryl']` named |
| lane's own empty note repo (0 notes) | **0.0**, all three named |

So the number moves with what retrieval actually returned, which is the property the pairs never
had while they were prose.

## Consequences

- A **third** thing this found, unasked: `make live-up` on its own leaves
  `CHEMCLAW_NOTE_REPO_DIR` pointing at a directory `infra/live/bootstrap.sh` creates, so a lane
  brought up without `make live-infra` indexes **zero** notes and every retrieval probe in it scores
  0 for a reason that is not the system. Visible now because a gold set has a number; invisible
  before, because "the model did not cite a note" reads like a model.
- The pairs can slide back into prose, so `tests/test_probe_coverage.py` asserts the direction and
  the field agree.
- `DEFERRED.md`'s row keeps its first clause and loses its parenthetical: a gold set over this
  repository's fixture corpus is not a deployment-local one, and it is scored on demand rather than
  on the drift cadence.

## What keeps it true

- `tests/test_probe_coverage.py::test_no_probe_expects_a_note_that_does_not_exist` — the offline
  half. Driven: pointing one label at `opt-suzuki-conditions-gone` reddens it.
- `tests/test_probe_coverage.py::test_every_note_a_direction_names_is_declared_as_data` — the
  anti-regression half, in both directions. Driven: dropping `playbook-degassing-old` from `kn-05`'s
  field while leaving it in the prose reddens it.
