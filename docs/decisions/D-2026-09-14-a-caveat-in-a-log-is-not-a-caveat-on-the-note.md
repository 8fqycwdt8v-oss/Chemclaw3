# D-2026-09-14-a-caveat-in-a-log-is-not-a-caveat-on-the-note — where "partial" has to be written

**Status**: accepted. Completes
`D-2026-09-14-the-memory-corpus-is-a-memory-bound-not-a-time-bound`'s stated behaviour.

## Context

`memory_corpus_max_reactions` bounds what one corpus read holds, and hitting it marks the read
*incomplete* rather than raising. The whole justification for that choice, in the ADR and in the
setting's own comment, is: **"a deployment over the bound gets partial knowledge that says it is
partial, instead of a worker that dies with no note at all."**

## The finding

The knowledge did not say it was partial. `corpus_complete=False` reached `memory/jobs.py::_units`
and did exactly two things: it skipped the retirement pass, and it logged a WARNING. The notes were
built unchanged and written to `knowledge/` **byte-identical** to notes distilled from the whole
record.

So the caveat existed for whoever was reading a background worker's log, and a chemist opening the
note — the reader the sentence is about — had nothing. The retirement skip is a real control and it
is a control about what is *removed*; it says nothing about what is written.

**The second order is worse and is what makes the mark load-bearing.** `stable_id` anchors a
cluster's id on its **smallest** member id, so a truncated read that drops that member mints a
different id for the same cluster. Measured: `["r-001","r-002","r-003"]` →
`playbook-22484f4007b4`, the same cluster minus `r-001` → `playbook-3a61d1964e6d`. That case is
normally resolved by the retirement pass —
`tests/test_memory.py::test_shrunk_cluster_retires_the_pre_shrink_note` is exactly it — and an
incomplete read is **precisely the run whose retirement pass is skipped**. The two notes coexist
with nothing linking them, and until now nothing on either said why.

## Decision

`memory.jobs.PARTIAL_READ_CAVEAT` is appended to the body of every note a partial run builds. It
names the two consequences separately, because they are different risks: the evidence cited may be
a subset, and the note's id may differ from the one the same cluster mints when read whole. It also
states that nothing was retired, so a reader is not left to infer the skip.

**Stamped in `_units`**, the one function both publish paths go through — the same argument that
put the retirement pairing there, and the same failure if it were duplicated into the three
builders: a fourth builder would inherit the caveat rather than having to remember it.

A later run over a complete corpus rewrites the note **without** the line. That is the note being
corrected in place, which is the model `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`
already establishes for agent-written knowledge.

## What is not taken

The id instability itself is not fixed, and that is deliberate. `min(member_ids)` is what keeps a
growing cluster's id stable through routine ELN sync — the case it was designed for — and no anchor
over the members *present* can be stable against a read that did not see them all. The honest
remedy is the streaming miner `BACKLOG.md` already carries with its own trigger; until then the
mark is what a reader gets, and it now exists.

## What keeps it true

- `tests/test_memory.py::test_a_note_from_an_incomplete_read_says_so_in_the_body_a_chemist_reads` —
  both halves in one assertion, because marking without skipping and skipping without marking each
  look like the fix and are half of it; plus the complete-read direction, without which the mark
  could be unconditional and say nothing.
- `::test_the_caveat_names_the_id_risk_the_skipped_retirement_pass_leaves_open` — asserted on the
  id derivation as well as on the words, so the sentence stops being worth having the day the
  mechanism behind it stops being real.
- `::test_shrunk_cluster_retires_the_pre_shrink_note` — the pre-existing test that establishes what
  the retirement pass does about a changed anchor, and therefore what its absence costs.
