"""Canonical solvent identity, because a solvent has more than one accepted spelling.

**This module exists because of a measured defect, not an anticipated one.**
`science/calc/solvents.py` holds `ALPB_SOLVENTS`, every name tblite accepts — 42 of them — and it
accepts `thf` **and** `tetrahydrofuran`; `hexane`, `n-hexane`, `nhexane`, `n-hexan` **and**
`nhexan`; `ch2cl2`, `dichloromethane`, `dichlormethane` (sic) **and** `methylenechloride`. That
name reaches the calculation key verbatim, so two runs of the same reaction in the same solvent,
spelled differently, are two different keys.

`SUGGESTED_SOLVENTS` is a canonical list of 16 — but its own docstring says *"the aliases are what
is left out, deliberately"*, and nothing anywhere in the tree maps an alias back to it. So a
published schema storing the name as given would answer *"every reaction we ran in THF"* with a
confident subset of the truth, and nothing would raise. That is the headline question this whole
subsystem exists to make answerable, so the mapping is built here.

**The 42 names collapse to 25 solvents.** Every group below is a set of spellings tblite treats
identically; the canonical member is `SUGGESTED_SOLVENTS`'s spelling where there is one, and
otherwise the shortest unambiguous name. `tests/test_publish_solvents.py` asserts every
`ALPB_SOLVENTS` entry resolves, so a name added upstream cannot quietly become unqueryable.

**`octanol` and `woctanol` are deliberately two solvents, not two spellings of one.** Dry and
water-saturated octanol have different dielectrics and are the two halves of a partition
coefficient; merging them would silently combine incomparable calculations.

No dielectric constants are recorded here. They would be useful to query on, and inventing them
would be fabricating data — the same objection `science/calc/uncertainty.py` raises against
approximating an applicability domain nobody measured. A deployment that has them can add them to
its own `solvent` rows.
"""

# canonical id -> every accepted spelling, itself included. Written this way round because a
# group is what a reader checks ("are these the same solvent?"), while the lookup wants the
# inverse — which is derived below rather than maintained twice.
_GROUPS: dict[str, tuple[str, ...]] = {
    "water": ("water", "h2o"),
    "methanol": ("methanol",),
    "ethanol": ("ethanol",),
    "acetonitrile": ("acetonitrile", "mecn"),
    "dmso": ("dmso", "dimethylsulfoxide"),
    "dmf": ("dmf", "dimethylformamide"),
    "acetone": ("acetone",),
    "thf": ("thf", "tetrahydrofuran"),
    "dioxane": ("dioxane",),
    "ethylacetate": ("ethylacetate", "ethyl acetate"),
    "ch2cl2": ("ch2cl2", "dichloromethane", "dichlormethane", "methylenechloride"),
    "chcl3": ("chcl3", "chloroform"),
    "toluene": ("toluene",),
    "benzene": ("benzene",),
    "ether": ("ether", "diethylether"),
    "hexane": ("hexane", "n-hexane", "nhexane", "n-hexan", "nhexan"),
    "cs2": ("cs2", "carbondisulfide"),
    "furan": ("furan", "furane"),
    "aniline": ("aniline",),
    "benzaldehyde": ("benzaldehyde",),
    "hexadecane": ("hexadecane",),
    "nitromethane": ("nitromethane",),
    "octanol": ("octanol",),
    "woctanol": ("woctanol",),
    "phenol": ("phenol",),
}

# A readable name per canonical id, for the `display_name` column — so a published row reads as
# "tetrahydrofuran" while its key stays the short form every tool already writes.
DISPLAY_NAMES: dict[str, str] = {
    "thf": "tetrahydrofuran",
    "dmso": "dimethyl sulfoxide",
    "dmf": "dimethylformamide",
    "ch2cl2": "dichloromethane",
    "chcl3": "chloroform",
    "cs2": "carbon disulfide",
    "ether": "diethyl ether",
    "ethylacetate": "ethyl acetate",
    "acetonitrile": "acetonitrile",
    "woctanol": "octanol (water-saturated)",
    "octanol": "octanol (dry)",
}

# The lookup, derived from `_GROUPS` so the two can never disagree.
_ALIASES: dict[str, str] = {
    alias: canonical for canonical, aliases in _GROUPS.items() for alias in aliases
}


def canonical_solvent(name: str | None) -> str | None:
    """The canonical id for a solvent name, or None for gas phase.

    Normalized the way `science/calc/solvents._normalize` does it — stripped and lowercased —
    because tblite matches that way and a publish path that canonicalized differently would fork
    on capitalization alone.

    **An unrecognized name is passed through normalized rather than rejected.** A solvent this
    registry has not heard of is still a fact about the run, and refusing to publish a completed
    calculation because its solvent is unfamiliar would lose science to protect a lookup table. It
    lands as its own id, which reads correctly as "a solvent we have no alias group for".
    """
    if name is None:
        return None
    normalized = name.strip().lower()
    if not normalized:
        return None
    return _ALIASES.get(normalized, normalized)


def display_name(canonical: str) -> str:
    """The readable name for a canonical solvent id."""
    return DISPLAY_NAMES.get(canonical, canonical)


def known_solvents() -> dict[str, tuple[str, ...]]:
    """Every canonical solvent and its accepted spellings, for seeding the shipped tables."""
    return dict(_GROUPS)
