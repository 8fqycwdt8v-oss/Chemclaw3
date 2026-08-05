"""Molecule/reaction fingerprint search (plan Phase 3, mcp-molfp/mcp-rxnfp).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

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
