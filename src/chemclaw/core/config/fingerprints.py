"""Molecule/reaction fingerprint search (plan Phase 3, mcp-molfp/mcp-rxnfp).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class FingerprintSettings(BaseSettings):
    """Molecule/reaction fingerprint search (plan Phase 3, mcp-molfp/mcp-rxnfp).

    Grouped because the fingerprint definition (and thus the stored column width) is a
    deliberate, versioned choice, and the search bounds guard the same SQL/RDKit paths those
    definitions feed.
    """

    # ECFP4 = Morgan radius 2, 2048 bits; both are config so the fingerprint definition is a
    # deliberate choice, not a magic number. The similarity threshold is the Tanimoto floor a
    # match must clear to count as a structural neighbor — the capability exposes it, the
    # `reaction-search` skill decides how to wield it (G6).
    ecfp_radius: int = Field(default=2, ge=0)
    ecfp_bits: int = Field(default=2048, gt=0)
    # DRFP reaction fingerprint width (plan step 3.4, mcp-rxnfp). Its own field, not shared with
    # ecfp_bits — a different fingerprint whose folded length is an independent choice, though
    # both default to 2048 (matching their bit(N) columns). top_k/threshold below are shared:
    # they are generic fingerprint-search knobs, not molecule-specific.
    drfp_bits: int = Field(default=2048, gt=0)
    fingerprint_top_k: int = Field(default=10, ge=1)
    fingerprint_similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    # **Whether a structural-similarity search must be right, or only fast.** `exact` compares the
    # query against every indexed fingerprint, so the neighbour list is the true top-k and an empty
    # one is real evidence that the corpus holds no analog. `approximate` lets the HNSW index
    # propose candidates and re-ranks them, so a true neighbour can be missed and "no precedent"
    # stops being proof — which is why this is a deployment's decision and not a tuning constant,
    # and why the answer says which arm ran (`FingerprintSearch.approximate`).
    #
    # The trade, measured on this branch against PostgreSQL 16.15 / pgvector 0.8.0 over 200,000
    # real ECFP4 `bit(2048)` rows (`tests/test_molfp_postgres.py` re-measures the agreement half):
    # the exact scan is 17.6 ms at 200k and linear at ~0.088 µs/row — ~880 ms at 10^6 and ~8.8 s at
    # 10^7, and `CLAUDE.md` names Pistachio (order 10^7 reactions) as the first live integration.
    # The index-ordered arm is ~1.25 ms and roughly flat in corpus size, a 14x at 200k that grows
    # with the corpus. What it costs is *agreement*: over 60 queries at `ef_search=200` with a 10x
    # over-fetch the returned page differed from the exact one for 22 of them — ties, not recall.
    # Tanimoto over sparse bit vectors puts many rows at identical similarity and the exact
    # `ORDER BY distance, id COLLATE "C"` breaks those ties across the *whole* table, which no
    # truncated candidate set can reproduce.
    #
    # Default `exact`, because the tool this feeds is the one a chemist asks "have we ever made
    # something like this?", and the failure mode of the other arm is the one this whole module is
    # arranged against: a silent "no precedent" for a structure we have on file.
    fingerprint_search_exactness: Literal["exact", "approximate"] = "exact"
    # How many candidates per returned hit the approximate arm pulls off the index before it
    # re-ranks and cuts. Only the top-k survives, so over-fetching buys agreement with the exact
    # answer at the cost of a wider index probe; measured, the agreement curve is steep below ~4x
    # and flat above ~10x. Ignored entirely under `exact`.
    fingerprint_approximate_overfetch: int = Field(default=10, ge=1)
    # `hnsw.ef_search` for the approximate arm — how wide pgvector's graph traversal keeps its
    # own candidate list. pgvector's default of 40 is far too narrow for a page of 100 with a 10x
    # over-fetch, so this is raised deliberately; the arm never sends less than it means to fetch,
    # and pgvector's own hard ceiling of 1000 bounds it. Ignored entirely under `exact`.
    fingerprint_approximate_ef_search: int = Field(default=200, ge=1, le=1000)
    # Upper bound on an agent-supplied `top_k` for the similarity tools (SEC-4). `top_k` reaches
    # `find_matches` from the model (the `molfp`/`rxnfp` bundles' MCP tools) and lands in a `LIMIT`,
    # so an arbitrarily large value would be an unbounded query. Clamp it to this — the
    # fingerprint-search analog of the `graph_max_hops` clamp on `expand_note`. Generous for a
    # real neighbor list.
    fingerprint_max_top_k: int = Field(default=100, ge=1)
    # How much deeper a *filtered* structural search looks than the page it returns (D-170). The
    # fingerprint index knows bits and a label, never note metadata, so a type/tag/date filter can
    # only be applied to neighbours after they come back — and applying it to the page would let
    # one unwanted neighbour cost a wanted one. Bounded by `fingerprint_max_top_k` regardless, so
    # this cannot become a way around the one cap on how much of the index a query pulls in.
    retrieval_filter_overfetch: int = Field(default=5, ge=1)
    # Bound on how many stored fingerprints one substructure scan materializes (SEC-4). The scan
    # has no similarity prefilter, so it loads records and RDKit-matches each; without a cap a
    # large corpus is a full-table load into the worker heap (the 30s statement_timeout bounds
    # DB time, not rows returned). The scan takes at most this many rows (deterministic id
    # order) and logs a warning when it hits the cap so a truncated result is never silent.
    # Raise it for a larger corpus, or add a pattern-fingerprint prefilter (deferred) when it
    # starts truncating.
    substructure_scan_max_records: int = Field(default=5000, gt=0)
    # Bound on the length of a model-supplied substructure query string (SEC-4). SMARTS matching
    # is subgraph isomorphism (worst-case exponential) run in-process over the scanned corpus
    # with no statement_timeout analog, so a pathological multi-KB pattern could pin the server.
    # Real pharmacophore/functional-group SMARTS run tens to a few hundred characters; 500
    # leaves generous headroom while rejecting degenerate input.
    substructure_query_max_length: int = Field(default=500, gt=0)
    # Bounds on any molecule string reaching `core/chem.require_molecule` — the one gate every
    # SMILES/SMARTS caller shares. RDKit's canonical-SMILES writer and the tautomer canonicalizer
    # recurse per atom and overflow the C stack on a large linear molecule: measured, `MolToSmiles`
    # / the standardizer SIGSEGV (exit 139) between 16,000 and 20,000 atoms, an *uncatchable* crash
    # that takes the whole worker process — and every concurrent session on it — down. A ~20 KB
    # SMILES clears the 1 MB body cap, so the bound has to be here. It is also an ingest poison
    # pill: one such value in an ELN row segfaults the Temporal worker and, because the sync cursor
    # is deterministic, stops that source permanently. The caps sit far below the cliff and far
    # above any real reagent (a large natural product is a few hundred atoms).
    molecule_max_smiles_length: int = Field(default=4000, gt=0)
    molecule_max_atoms: int = Field(default=1000, gt=0)
    # Wall-clock bound on one substructure scan's matching work (SEC-4, completing the guard
    # above). Length and record caps bound the *inputs*, but a short adversarial recursive
    # SMARTS can still run for minutes, and the scan is invoked from the async front door — so
    # the matching loop runs in a worker thread under this timeout and the caller is released
    # with a clear error instead of every other session's stream stalling behind it. Honest
    # limit: the timeout frees the event loop and the caller, it cannot kill the RDKit thread
    # (RDKit offers no interruption hook), so one CPU stays busy until that pattern finishes.
    # Killing the work outright would need a subprocess — over-engineering until a real abuse
    # case is measured. Seconds; normally ms.
    substructure_match_timeout_seconds: float = Field(default=5.0, gt=0.0)
