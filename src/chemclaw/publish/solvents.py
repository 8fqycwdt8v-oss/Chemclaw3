"""Canonical solvent identity, because a solvent has more than one accepted spelling.

tblite accepts several spellings for one solvent (`thf`/`tetrahydrofuran`, the hexane variants,
...), and the name reaches the calculation key verbatim, so "every reaction we ran in THF" would
otherwise return a confident subset. Each group below is a set of spellings tblite treats
identically; the canonical member is `SUGGESTED_SOLVENTS`'s spelling where there is one, else the
shortest unambiguous name. `tests/test_publish_solvents.py` asserts every `ALPB_SOLVENTS` entry
resolves.

`octanol` and `woctanol` are two solvents (dry and water-saturated), not two spellings. No
dielectric constants are recorded: inventing them would be fabricating data.
"""

# canonical id -> every accepted spelling, itself included. Written this way because a reader
# checks groups; the lookup's inverse is derived below.
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

    Normalized as `science/calc/solvents._normalize` does (stripped, lowercased) so capitalization
    cannot fork it. An unrecognized name passes through normalized rather than being rejected: it is
    still a fact about the run.
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
