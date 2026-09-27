# D-2026-09-14-a-hit-is-not-a-citation — the record's precedent is offered at draft time and never written into the design's evidence

## Status

Accepted.

## Context

`D-2026-09-14`'s failure-memory check closed the gap where a design could repeat a documented
failure sitting in the same graph. The symmetric gap is the positive one: the record holds runs
*like* this design, and nothing surfaced them at the moment a protocol was being drafted.

`draft_experiment_protocol`'s docstring already tells the model to search — "Do the work before you
call this: a design citing no precedent and no tool is refused" — and `evidence_present` refuses a
draft that cites nothing at all. Neither asks the question the record can answer on its own: *is
there something here you did not cite?*

## Decision

`precedent_consulted`, a `note`-severity check fed by `agent.protocol_design_tools._uncited_precedent`.

**It offers, and it never cites.** This is the whole design constraint and the reason the check is
advisory rather than a filler. A citation is a claim the chemist makes about what a decision rests
on; a search hit is a thing that exists. Auto-filling `design.evidence` from hits would forge the
first out of the second — and `evidence_present` would then pass on a design nobody had actually
grounded, which is a check satisfying itself and worse than the gap it closes. A test asserts the
design object is unmutated, because "advisory" is a property of what is *not* written and no
assertion about the returned check could see it.

**A `note`, for `no_documented_failure`'s reasons.** A Tanimoto neighbour can share a scaffold and
nothing else, the chemist may have read it and judged it inapplicable, and a deliberate re-run under
changed conditions is ordinary work. Blocking would teach people to cite noise.

**The empty case is silence, not a claimed negative.** An empty list means one of three things —
nobody looked, the index is empty or mid-rebuild, or the record genuinely holds nothing like this —
and keeping those apart is the entire reason `FingerprintSearch` is not a bare list. A check cannot
re-derive the distinction, so the passing text says nothing was *offered* rather than that nothing
exists. That is `ScreenResult.verdict`'s lesson applied one subsystem over.

**Already-cited hits are dropped, and the comparison is the citation's spelling.** `EvidenceRef.ref`
carries `reaction-<source>.<id>` or the bare `reaction-<id>` that `note_id_for_reaction` mints,
while a `Match.id` is the record id alone. Compared raw, every citation a design *does* carry comes
back as uncited — so a chemist who did the reading would be told off for exactly the work they did,
which is the noisiest possible way to be wrong. `external_record_ref` is the inverse that already
exists; driven, dropping either half of that reports the cited hit.

**It runs at the request stage too.** `ExperimentRequest.reaction_smiles` is part of the ask, so what
the record holds like it is knowable before there is a procedure — the same argument that already
put `no_documented_failure` in `_REQUEST_STAGE`, and the moment it is cheapest to read.

**`run_checks` dispatches through a mapping now.** With one corpus-fed check it was
`if check is no_documented_failure`; with two that becomes a chain of identity tests, and there is a
*class* of checks the caller feeds from a corpus rather than one function's arrangement. The third
should not need this function edited in two places.

## Consequences

- One more Postgres round trip per draft and per revision, on the same best-effort terms as the
  failure lookup: it never raises, and a failure is counted on
  `chemclaw_degraded_total{subsystem="precedent_lookup"}` rather than swallowed — a lookup that has
  quietly stopped working otherwise returns every draft clean forever.
- `POST /protocols/{id}/revisions` still calls `run_checks` with neither corpus input, so a human
  edit publishes the same clean bill it already did. That is the pre-existing honesty gap
  `run_checks`' docstring states, unchanged rather than widened.
- A design with no `reaction_smiles` does not search at all; there is nothing to be similar to.

## What keeps it true

- `tests/test_protocol_checks.py::test_precedent_consulted_is_silent_when_nothing_is_offered`,
  `::test_precedent_consulted_names_the_runs_the_design_did_not_cite`,
  `::test_uncited_precedent_is_a_note_rather_than_a_blocker`,
  `::test_many_precedents_are_summarised_rather_than_listed_entire`,
  `::test_the_precedent_check_never_writes_into_evidence`.
- `tests/test_protocol_design_tools.py::test_a_precedent_the_design_already_cites_is_not_reported_as_uncited`,
  `::test_a_design_with_no_reaction_smiles_does_not_search`,
  `::test_an_unreachable_index_is_counted_rather_than_failing_the_draft`.
