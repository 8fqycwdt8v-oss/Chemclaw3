"""Settings for molecule and reaction fingerprint search.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class FingerprintSettings(BaseSettings):
    """Fingerprint definitions and the bounds on the search paths they feed.

    The definition (and so the stored column width) is a deliberate, versioned choice.
    """

    # ECFP4 = Morgan radius 2, 2048 bits. The similarity threshold is the Tanimoto floor for a
    # structural neighbor; the `reaction-search` skill decides how to use it.
    ecfp_radius: int = Field(default=2, ge=0)
    ecfp_bits: int = Field(default=2048, gt=0)
    # DRFP reaction fingerprint width, independent of `ecfp_bits` (both match their bit(N) columns).
    # `top_k`/threshold are shared search knobs.
    drfp_bits: int = Field(default=2048, gt=0)
    fingerprint_top_k: int = Field(default=10, ge=1)
    fingerprint_similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    # Whether similarity search must be right or only fast. `exact` scans every fingerprint, so the
    # top-k is true and an empty result is evidence of no analog. `approximate` uses the HNSW index
    # and re-ranks: much faster on large corpora, but it can miss neighbours (Tanimoto ties are
    # broken across the whole table), so "no precedent" stops being proof. The answer reports the
    # arm (`FingerprintSearch.approximate`). Default `exact`.
    fingerprint_search_exactness: Literal["exact", "approximate"] = "exact"
    # Candidates per returned hit the approximate arm pulls before re-ranking; more buys agreement
    # with `exact`. Ignored under `exact`.
    fingerprint_approximate_overfetch: int = Field(default=10, ge=1)
    # `hnsw.ef_search` for the approximate arm; pgvector's default 40 is too narrow for a 10x
    # over-fetched page, and 1000 is its hard ceiling. Ignored under `exact`.
    fingerprint_approximate_ef_search: int = Field(default=200, ge=1, le=1000)
    # Upper bound on the model-supplied `top_k` that lands in a `LIMIT`; values are clamped.
    fingerprint_max_top_k: int = Field(default=100, ge=1)
    # How much deeper a filtered structural search looks than the page it returns: metadata filters
    # apply only after neighbours come back. Still capped by `fingerprint_max_top_k`.
    retrieval_filter_overfetch: int = Field(default=5, ge=1)
    # Records one substructure scan materializes (no similarity prefilter; each is RDKit-matched).
    # Hitting the cap logs a warning. Raising it costs time against
    # `substructure_index_build_timeout_seconds` (index skipped) and
    # `substructure_match_timeout_seconds` (search fails), so raise those with it.
    substructure_scan_max_records: int = Field(default=5000, gt=0)
    # Maximum length of a model-supplied substructure query; SMARTS matching is worst-case
    # exponential and runs in-process. Real patterns are at most a few hundred characters.
    substructure_query_max_length: int = Field(default=500, gt=0)
    # Bounds on any molecule string reaching `core/chem.require_molecule`. RDKit's SMILES writer and
    # tautomer canonicalizer recurse per atom and segfault the whole process on very large
    # molecules, which in ingest would also wedge a source's sync permanently. Far below that cliff,
    # far above any real reagent.
    molecule_max_smiles_length: int = Field(default=4000, gt=0)
    molecule_max_atoms: int = Field(default=1000, gt=0)
    # Wall-clock bound (seconds) on one scan's matching, run in a worker thread so the async caller
    # is released with an error. It cannot kill the RDKit thread (no interruption hook), so one CPU
    # stays busy until the pattern finishes.
    substructure_match_timeout_seconds: float = Field(default=5.0, gt=0.0)
    # How long building the substructure index may take before the scan matches record by record
    # (`substructure_index.index_for` returns None). Separate from the match bound so a slow build
    # costs speed, never the answer. It covers `substructure_scan_max_records` with headroom; the
    # build projects from its own rate and gives up early on a corpus far over it.
    substructure_index_build_timeout_seconds: float = Field(default=3.0, gt=0.0)
    # How long one chunk of a scan runs before the deadline is checked again. `GetMatches` cannot be
    # interrupted and per-molecule cost spans orders of magnitude, so chunks are sized in time, not
    # records. A twentieth of `substructure_match_timeout_seconds`.
    substructure_scan_deadline_slice_seconds: float = Field(default=0.25, gt=0.0)
    # Built substructure indexes held in memory, keyed by a digest of their source labels. 2 so
    # queries in flight against the previous generation do not each rebuild it; memory is this times
    # `substructure_scan_max_records` times ~1 kB per molecule.
    substructure_index_cache_entries: int = Field(default=2, ge=1)
