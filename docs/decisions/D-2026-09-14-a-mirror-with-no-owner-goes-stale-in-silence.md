# D-2026-09-14-a-mirror-with-no-owner-goes-stale-in-silence — a vendored corpus says whether it is a copy, and a copy names who re-takes it

## Status

Accepted.

## Context

`Chemclaw3-mcp`'s `MODULES.md` carries this as an open question, and states it better than a
restatement would:

> **Snapshot refresh is an operational commitment.** Every mirrored corpus needs a named owner and
> a cadence, recorded in that server's README. A stale patent index that nobody knows is stale is
> worse than no patent index.

W29.9 asked this repository to assign an owner or record the posture. **Measured first: this
repository mirrors nothing.** Its one vendored corpus, `data/vendored/common-reagents`, is
first-party and hand-authored in this tree, and its `retrieved_from` says so. There is no upstream
here to go stale against, so there is no owner to assign — and the READMEs `MODULES.md` is asking
for belong to servers in another repository, which this session may not write to.

Recording that as a posture in prose would be the weakest possible answer, and it would be false
within one commit: the vendored seam exists precisely so a third-party corpus *can* be added, and
the day one is, nothing would ask the question. The mechanism's own `README.md` lists six required
manifest fields and argues each one — "a corpus with no recorded licence is a legal question nobody
can answer later" — while the question of whether a corpus is even a *copy* was never asked at all.

## Decision

**A vendored dataset manifest must answer `mirrored`, and a mirrored one must name
`refresh_owner` and `refresh_cadence`.** Enforced by `DatasetManifest`, which every path into a
vendored corpus goes through, so the refusal is at load rather than in a review checklist.

- `mirrored` is **required, not defaulted**. A default answers the question on the author's behalf,
  which is the one thing a provenance model must never do — the same argument the six existing
  required fields already make.
- The refusal runs **both ways**. First-party content naming a refresh owner is a claim about an
  upstream that does not exist, and the next reader spends an afternoon looking for a feed. That is
  a smaller harm than a stale corpus, and it is a harm, so it is refused too.
- `data/vendored/dataset.json` carries `mirrored: false` and names neither, which is the posture
  W29.9 asked for — recorded as a field a test can read rather than as a sentence.

**Not done here:** the fleet's own READMEs. Those corpora are real mirrors (SureChEMBL, MassBank,
nmrshiftdb2) and their owners are that repository's to assign; the rule above is stated in a form
the fleet can adopt, and `MODULES.md`'s open question stays open until it does.

## Consequences

The first third-party corpus vendored into this repository cannot load until somebody has written
down who re-takes it and how often. That is the intended cost, and it is paid once per corpus.

## What keeps it true

- `tests/test_vendored_source.py::test_a_mirrored_corpus_must_name_who_refreshes_it_and_how_often`
  — driven through `_read_manifest`, the function every load path uses, not through the model.
  Mutation: disabling the `mirrored` arm of the validator fails it.
- `tests/test_vendored_source.py::test_first_party_content_may_not_claim_an_upstream_it_does_not_have`
  — the other direction, and it asserts the shipped corpus is `mirrored: false`. Mutation:
  disabling that arm fails it.
