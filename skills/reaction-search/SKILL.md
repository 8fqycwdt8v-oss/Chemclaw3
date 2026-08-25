---
name: reaction-search
description: >-
  Judgment for finding structurally related molecules and reactions with the fingerprint
  tools: molecule similarity vs. substructure vs. reaction similarity, what Tanimoto counts
  as precedent, and how to combine with metadata filters and the knowledge graph.
tools:
  - similar_molecules
  - substructure_matches
  - similar_reactions
  - condense_protocols
---

# Reaction / structure search

Holds the *judgment* for the fingerprint capabilities: `mcp-molfp` (`similar_molecules`,
`substructure_matches`) over ECFP4 molecule fingerprints, and `mcp-rxnfp` (`similar_reactions`)
over DRFP reaction fingerprints. Each server also exposes an index/write tool, which is
deliberately kept off the agent's allowlist — indexing happens on ingest, not from chat. The tools compute
fingerprints and rank by Tanimoto (or match a substructure) — deterministically and without
opinion. This skill decides *when and how* to use them, so the agent doesn't just call them
correctly but uses them well (G6).

## Pick the right question

- **Molecule similarity** (`similar_molecules`) answers *"have we worked on a compound like
  this?"* — graded whole-molecule resemblance. Use it to find precedent for a new substrate.
- **Substructure** (`substructure_matches`) answers *"which of our molecules contain this
  exact motif?"* — a boolean structural filter (e.g. all molecules with a free carboxylic
  acid). Use it when a specific functional group or scaffold, not overall shape, matters. It
  is exact: a hit truly contains the fragment; a miss truly does not.
- **Reaction similarity** (`similar_reactions`) answers *"have we run a transformation like
  this?"* — DRFP captures the *difference* between reactants and products, so it finds
  reactions of the same type (same bond changes) even on different substrates. Query with a
  full reaction SMILES (`reactants>>products`); reactions have no substructure search.
- If unsure, combine: substructure/similarity narrows the molecules, reaction similarity
  finds the transformations that produced or consumed them.
- **DRFP scores the whole reaction, including reagents.** If the indexed corpus encodes full
  conditions as reactants (ligand, base, additive — common for HTE/screening data, not just
  the two core substrates), a query built from only the core substrates can score well below
  the similarity floor against a real, otherwise-identical-class precedent — a live e2e
  finding (a 2-component query scored ~0.24 against a 5-component real match of the same
  reaction class; the same query built with the matching recipe scored 1.0). A "0 hits" result
  can therefore mean "your query's recipe detail doesn't match how the corpus was indexed," not
  "no precedent exists." If a core-substrate-only query returns nothing, say that caveat, try
  widening the query with plausible reagents/conditions, or fall back to `similar_molecules` on
  the substrates before concluding there is no precedent.
- **An unbuilt index is not an empty answer.** Every fingerprint search returns a `verdict`
  beside its `hits`. When `index_empty` is true, nothing has been indexed and the search never
  ran: report that the fingerprint index has not been populated and that an operator must build
  it. Do not say "we have no precedent for this" — that claim needs a corpus to be false about.

## What Tanimoto counts as precedent

- The tool's default floor is `fingerprint_similarity_threshold` (config, ~0.3). Treat it
  as a *screening* floor, not proof of relevance.
- Rough reading of ECFP4 Tanimoto: **≥0.7** strong analog (usually real precedent),
  **0.4–0.7** worth a look (same series or shared scaffold), **<0.4** weak — mention only
  with the caveat that it may share isolated features, not chemistry.
- These are guidance, not law: a low-Tanimoto hit sharing the *reacting* motif can matter
  more than a high-Tanimoto hit that differs only far from the reaction center. Read the
  structures, don't just trust the number.

## Reading what the hits actually contain

- The fingerprint tools return ids and similarity, never chemistry. A hit is a *candidate*
  until you have read the protocol behind it, and a ranked list of ids is not evidence.
- With more than a handful of hits, pass their ids straight to `condense_protocols` rather
  than calling `expand_note` on each. It reads every protocol whole and returns one comparison
  — conditions, outcomes, and what each run changed relative to the one before — which is the
  form in which a Tanimoto neighbourhood becomes a readable answer about what has been tried.
- A high-similarity hit whose conditions turn out to be unrelated to yours is a *worse*
  precedent than a mid-similarity one that shares the reacting motif and the solvent system.
  The comparison is where that becomes visible; the score alone hides it.

## Combine with metadata and the graph

- The fingerprint tools return ids + SMILES + similarity, nothing else. To answer
  *"similar to X, but only project Y, only logP < 3"*, take the returned ids and filter
  via the knowledge graph (`knowledge-graph-query`) or the relevant calculator — the
  fingerprint search is the structural pre-filter, not the whole answer.
- When several structurally *different* molecules are all plausibly relevant, present them
  as distinct options with their similarity and provenance, not a single ranked list that
  hides the diversity — let the chemist judge which analogy holds.

## Honesty

- Similarity is not causation: a close Tanimoto neighbor is a *candidate* precedent, to be
  confirmed against the actual recorded chemistry (graph note, ELN), never asserted as an
  outcome on structure alone.
- If a query molecule fails to parse, say so — do not silently search a wrong structure.
