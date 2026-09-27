# D-2026-09-13-a-citation-names-the-source-it-was-found-in — the index has been keyed by source since 063, and the citation was not

`D-2026-08-27-a-fingerprint-is-keyed-by-its-source` keyed `reaction_fingerprints` on `(source, id)`,
which is what stops one site's chemistry being overwritten by another's, and deliberately left the
read side alone. `D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry` then **deleted** an
optional `source` argument on `note_id_for_reaction` — correctly, because nothing passed it and
every reader still spelled and stripped the bare form, so the qualified id it built resolved to
nothing anywhere. A spelling no reader accepts is a claim that two sites can be told apart, not a
way of telling them apart.

This closes it from the readers, which is the order both of those ADRs said it had to be done in.

## What was measured

With one entry id ingested from two sources, against a real database:

- the retriever and the `similar_reactions` tool returned **two hits carrying one citation**, and
  `ingest.eln.records._one_of` raised `AmbiguousReactionRecord` the moment either was expanded.
  Loud rather than wrong, and still not an answer: the chemist cannot open the run the search found.
- `reaction_labels.citation` — the field whose own description says "a precedent the chemist cannot
  follow back is not a precedent" — was written bare from a row that already carried its source.
- `retracted()`, shipped hours earlier in
  `D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports`, was keyed on the bare id, so one site's
  withdrawal dropped the *other* site's run out of the evidence sweep. Driven: alpha withdrew
  `EXP-9002`, beta did not, and both hits left the sweep.

## The decision

**A citation names the source it was found in, and the bare form keeps resolving.**

- `note_id_for_reaction(record_id, source)` spells `reaction-<source>.<id>`; the separator is `.`
  because `_SLUG` already admits it, so a qualified id is a legal note slug, git ref component and
  filename with no widening. It splits on the **first** occurrence, so an entry id containing dots
  survives, and a *source* containing one is refused where it would be spelled.
- `external_record_ref` is the inverse, returning the pair rather than the id, because a resolver
  handed only the id back would ask the store the same ambiguous question the qualification answers.
- Four readers pass the source they already have: `retrieval.retrievers`, `connectors.rxnfp`,
  `ingest.labels.record.record_phase`, and — resolving — `agent.graph_tools` and
  `agent.protocol_tools`.
- `records.read(reaction_id, source)` answers from the primary key when the citation is qualified,
  as a *different statement* rather than the ambiguous one filtered in Python. Unqualified, it is
  unchanged: `_one_of` still refuses, because a bare citation genuinely does not name one run.
- `retracted()` takes `(source, reaction_id)` refs, empty source meaning "any", for the same reason.

**The bare form is not deprecated.** Every citation already committed to `knowledge/` and every
`reaction_labels.citation` row written before this spells it, and they must keep resolving. That is
also why this is not a migration: nothing rewrites stored ids.

## What this does not do

`kg.validate.unresolved_citations` still checks existence by **id**, so a qualified citation whose
source does not hold the id passes the validator when another source does — and is then refused at
read time, loudly, naming the id. The cut is drawn rather than closed because `records.known`
answers a page of ids with one indexed lookup, and the validator's message says what it asked.
`external_record_id`'s docstring states this instead of implying that `kg.validate` argues it, which
is what it claimed before and that argument was nowhere.

## What keeps it true

- `tests/test_eln.py::test_two_sources_behind_one_entry_id_are_two_citations_that_each_resolve` —
  the acceptance test over real Postgres: two hits, two citations, each expanding to its own row,
  and the bare form still refusing.
- `tests/test_eln.py::test_one_sites_withdrawal_does_not_retract_the_other_sites_run` — both
  directions, so "nothing was dropped" cannot be satisfied by a filter that never runs.
- `tests/test_rxnfp_server.py::test_two_sites_behind_one_entry_id_are_cited_apart` — the tool a
  chemist asks directly.
- `tests/test_reaction_records.py::test_a_structural_hit_still_expands_into_its_recipe` — the round
  trip, which is what makes a citation a citation rather than a string.
- `tests/test_eln.py::test_ingesting_a_reaction_writes_the_label_index_record_phase` — the precedent
  citation.

Each of those asserts the expected id as a **literal**. Deriving it from `note_id_for_reaction`
moves both sides of the assertion together, and a reader that stopped naming its source would still
pass.
