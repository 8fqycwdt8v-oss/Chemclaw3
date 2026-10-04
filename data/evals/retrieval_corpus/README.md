# Retrieval gold corpus (fixture — not live knowledge)

A fixed set of knowledge-graph notes the KM-13 retrieval metrics score against
(`src/chemclaw/evals/retrieval.py`). It is deliberately **not** under `knowledge_dir`: keeping it here makes the
retrieval recall/precision numbers reproducible and independent of whatever is in the live graph,
and keeps the default `kg-validate` scan (which reads `knowledge_dir`) from treating these fixtures
as real notes.

Each file is a valid `kg.note.Note`. The paired gold queries and their expected source ids live in
`data/evals/cases/retrieval-*.md`. Edit the two together: a change here that moves what a query surfaces
must be reflected in the expected-source lists (the tests pin the resulting recall/precision).

## Why it is this big, and why most of it is distractors

A corpus no larger than `retrieval_top_k` could never engage the per-leg cut — every matching note
would be returned for every query, recall would sit at 1.00 by construction, and a ranking
regression could not show. So the corpus is large enough that the cut engages on the broad queries
(dozens of notes contain the literal "coupling" against a `top_k` of 8), and most of it is
**distractors that genuinely match**. A distractor that matches nothing teaches the gate nothing:
recall only becomes a statement about the *ranking* when the retriever has to choose between a note
the query is about and a note that merely says the word. Every distractor here is a note a process
chemist would really write about the same terms — a reagent that names its role in a coupling, a
playbook arguing that reflux is not a setpoint, a work-up failure that only happens off reflux.

The content is one coherent programme (a biaryl intermediate made by Pd-catalysed cross-coupling,
with an organozinc/Negishi arm scouted beside it), so the distractors are plausible rather than
generated. Note ids are type-prefixed, so `reaction-*` notes sort last — deliberately: several gold
notes sort late, and `reaction-zinc-negishi-biaryl` sorts **last in the whole corpus**, so a ranker
that falls through to note id drops it first. `retrieval-zinc-negishi-last-id` is the case that
watches for exactly that, and it is why growing the corpus and adding that case are one change.

## Layout

`<type>/<id>.md`, the same layout `kg/record.py` writes and `kg.validate` requires, so `python -m
chemclaw.cli.validate_kg data/evals/retrieval_corpus` exits 0 on this directory. A fixture the
graph's own validator refuses is a claim rather than a fixture.

Relations are body `[[wikilinks]]` only, never typed edges — `tests/test_relations.py` pins that
every edge this corpus parses to is a plain citation, and that pin is what keeps the compatibility
claim about untyped links honest. Ids are disjoint from `knowledge/` for the reason
`tests/test_seed_corpus.py` states: the gold fixture and the seed graph must not merge.
