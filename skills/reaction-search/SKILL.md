---
name: reaction-search
description: >-
  Judgment for finding structurally related molecules and reactions with the fingerprint
  tools: molecule similarity vs. substructure vs. reaction similarity, what Tanimoto counts
  as precedent, and how to combine with metadata filters and the knowledge graph.
---

# Reaction / structure search

Holds the *judgment* for the fingerprint capabilities: `mcp-molfp` (`similar_molecules`,
`substructure_matches`, `index_molecule`) over ECFP4 molecule fingerprints, and `mcp-rxnfp`
(`similar_reactions`, `index_reaction`) over DRFP reaction fingerprints. The tools compute
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

## What Tanimoto counts as precedent

- The tool's default floor is `fingerprint_similarity_threshold` (config, ~0.3). Treat it
  as a *screening* floor, not proof of relevance.
- Rough reading of ECFP4 Tanimoto: **≥0.7** strong analog (usually real precedent),
  **0.4–0.7** worth a look (same series or shared scaffold), **<0.4** weak — mention only
  with the caveat that it may share isolated features, not chemistry.
- These are guidance, not law: a low-Tanimoto hit sharing the *reacting* motif can matter
  more than a high-Tanimoto hit that differs only far from the reaction center. Read the
  structures, don't just trust the number.

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
