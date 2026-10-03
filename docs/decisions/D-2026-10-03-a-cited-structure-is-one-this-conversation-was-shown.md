# D-2026-10-03-a-cited-structure-is-one-this-conversation-was-shown — a geometry's `structure_id` is session-scoped on write

**Status:** accepted · **Date:** 2026-10-03. Narrows the `structure_id` citation the hardening
contract added (item 1); `D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy`
stands for `source`.

## Context

A geometry artefact may cite a stored structure by its `structure_id`, resolved to XYZ from the
structure store when the artefact is read. The structure store is content-addressed and shared, as
the calculation cache is: one optimised geometry serves every session that computes it, and the calc
tools accept any `structure_id` a caller names. So, as first written, an artefact could cite any
structure the deployment ever stored — including one only another conversation computed — if its
writer knew or guessed the id. The id is sixteen hex digits of a hash, so guessing is not the
realistic path; an id copied from another conversation, by a chemist or by a model steered by text
that carried one, is.

## Decision

On a **write**, a `structure_id` must appear in one of this session's stored tool results that is
evidence (`exhibits.evidence`): a calculation or job status here reported it. A mention in the
agent's own text handed back — a helper's report, an artefact readout — does not count. A revision
that carries the same id its parent already cited is accepted without asking again, as a carried
binding is, so retention sweeping the reporting result does not block later edits. A **read** still
resolves globally: the citation was checked when it was written, and an artefact must stay readable.

Where the deployment keeps no session's tool results (`core.result_handle.handles_resolve` false —
the in-memory session store, or the result store off) there is nothing to check against, and the id
resolves globally as the calc tools resolve one.

**It is cheap.** One substring query over the session's evidence blobs, measured on Postgres 16 with
the id absent (every blob read): 4.5 ms for 50 results of 20 kB, 13 ms for 200 of 50 kB, 46 ms for
500 of 100 kB — once per geometry write. **It does not break the use it exists for**: every calc
tool and job status that hands the agent a geometry hands it the `structure_id` in its result text
(`science/calc/geometry.py` strips coordinates and keeps the address), and that text is what the
session stores.

## Options considered

- **Keep it global**, because structures are shared like the calc cache and the calc tools already
  accept any id. Declined: a calc tool *computes from* a structure, while an artefact *shows* one to
  the chemist as this conversation's result, which is a claim about provenance the global form
  cannot back.
  Revisit when: a legitimate flow cites a structure the session was never shown — a chemist pasting
  an id from another conversation, or a job whose result is over the result store's cap and so not
  stored — and is refused for it; `tests/test_exhibit_geometry.py` is where that case would be added.
- **Record the ids a session was shown in a table of their own.** Declined: the session's stored
  results already are that record, with its retention and erasure; a second one would need both.

## What keeps it true

- `tests/test_exhibit_geometry.py::test_a_structure_cited_must_be_one_this_conversation_was_shown` —
  refused before a report, refused for a helper's mention and another session's report, accepted
  after this session's, carried on revise.
