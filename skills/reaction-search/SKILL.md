---
name: reaction-search
description: >-
  Judgment for finding structurally related molecules and reactions, and for the faceted
  precedent search over the labelled corpus: which question each tool answers, what Tanimoto
  counts as precedent, how to read a frequency roll-up, how to turn a neighbourhood into a
  readable comparison, and what a coverage sentence obliges you to say.
tools:
  - similar_molecules
  - substructure_matches
  - similar_reactions
  - substrate_precedent
  - conditions_for_similar_product
  - conditions_for_similar_reaction
  - reagent_frequency
  - reactions_making_substructure
  - workup_precedent
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
- **Facet questions are a different tool family.** Similarity answers "is there anything like
  this?". It cannot answer "as *what*", "which ligand", "under what conditions", "with a product
  carrying this group", or "how was it worked up" — those are properties of the recipe, and DRFP
  deliberately throws the recipe away (see the next bullet). Ask them of the precedent tools:
  - `substrate_precedent(smiles, role=...)` — *has this substrate been used, and as what?* `role`
    is `starting-material`, `product`, `reagent`, `solvent`, `catalyst`, `ligand`, `base` or
    `additive`. Exact on the structure: for "like this", run `similar_molecules` first and ask
    about each neighbour, so a near-miss is never presented as a match.
  - `conditions_for_similar_product(product_smiles)` — *what conditions worked for similar
    products?* Neighbours in fingerprint space, then their recorded recipes, temperatures, times
    and yields, each with a document to cite.
  - `conditions_for_similar_reaction(reaction_smiles)` — *has this **transformation** been run, and
    under what conditions?* The same two passes, with neighbours found in DRFP space instead. Prefer
    it over the product form whenever you have the whole reaction: a Buchwald and a Suzuki that make
    the same biaryl are neighbours by product and are not the same reaction. Prefer
    `similar_reactions` when you want *our own* runs — the two search different indexes (the
    literature corpus versus this organisation's ELN) and cite different things, so a hit here is a
    patent or document reference rather than a `reaction-<id>` note.
    **Query it with `reactants>>products` only.** Both reaction indexes are built with the agent
    slot excluded, so reagents in the query add features the rows do not have: measured on one
    Buchwald, naming just the solvent scores 0.85 against the same reaction indexed without it. This
    is the *inverse* of the corpus caveat below — there the risk is a corpus that encoded the recipe
    as reactants, here it is a query that does.
  - `reagent_frequency(named_reaction=..., roles=["ligand"])` — *which ligands were used for
    Buchwald couplings?* Leave `roles` out and add `product_functional_group=...` for *which
    workhorse conditions were used when the product carries this group*.
  - `reactions_making_substructure(smarts)` — *find reactions whose product matches this SMARTS.*
    Screened with a pattern fingerprint and verified exactly, so a hit truly contains the motif.
  - `workup_precedent(reagent_smiles)` — *how do we work this up?* Verbatim instructions only from
    runs that recorded one.
- If unsure, combine: substructure/similarity narrows the molecules, reaction similarity
  finds the transformations that produced or consumed them, and the precedent tools say what was
  actually in the flask.
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
- **Coverage is part of the answer, not a footnote.** Every precedent tool returns a `coverage`
  block and a `verdict` sentence saying how much of the *matching* corpus is actually labelled. On
  a partly-labelled corpus every count is a lower bound, and you must say so in the same breath as
  the number. "None found" over an unlabelled corpus means *the question was not answered* — never
  render it as "there is no precedent".
- **An unbuilt index is not an empty answer.** Every fingerprint search returns a `verdict`
  beside its `hits`. When `index_empty` is true, nothing has been indexed and the search never
  ran: report that the fingerprint index has not been populated and that an operator must build
  it. Do not say "we have no precedent for this" — that claim needs a corpus to be false about.

## Reading a frequency roll-up

`reagent_frequency` counts what the corpus *did*, which is not the same as what you should do.

- **Popularity is the field's default, not a recommendation.** XPhos leading a Buchwald ligand
  table means it was tried most, usually because it was tried first. Say "most commonly used",
  never "best".
- **Read `count` beside `share`, and both beside `reactions_in_scope`.** Three of four is not
  evidence; three hundred of four hundred is. The denominator is reactions that recorded *that
  role* — a run whose ligand nobody wrote down is not evidence that no ligand was used.
- **`median_yield_percent` is over the runs that reported a yield**, and it is `null` when none
  did. A null is not a zero and must not be rendered as one.
- **Prefer `rxno_id` to `named_reaction`.** NameRxn, Rxn-INSIGHT and RXNO are three name strings
  for one transformation. Matching the string answers from whichever fraction of the corpus used
  that spelling — silently, and looking complete.

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
