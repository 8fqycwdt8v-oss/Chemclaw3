"""Resolve the names chemists actually write to the structures every tool demands (gap TOOL-2).

Every chemistry capability in this codebase speaks SMILES — `compute_xtb_energy(smiles)`,
`predict_pka(smiles)`, `similar_molecules(smiles)`, `find_substructure_matches(query)`. Chemists
write `Pd(dppf)Cl2`, `DIPEA`, `2-MeTHF`, `TBTU`, and ELN free text writes the same. Nothing bridged
the two, and the consequences compounded rather than added:

- `find_notes` is literal substring matching, so a query by trivial name missed a SMILES-keyed
  corpus *entirely* rather than partially;
- the deferred "per-step species linking from free-text prose" is blocked on precisely this — its
  `docs/planning/DEFERRED.md` entry says linking needs "a name→SMILES tool", which did not exist;
- the conditions-vocabulary gap (KNW-4) and compound notes (KNW-7) both need one canonical identity
  to hang on.

**Deliberately a committed table, not a network call.** The reagents a process-chemistry group uses
daily are a small, stable, high-value set, and a table is deterministic, offline, reviewable in a
PR, and citable — the same reasoning that keeps the eval case-set in Git. An external resolver
(PubChem/OPSIN) belongs behind the F7 `DataSource` seam as one more source when a real need appears;
it is not a prerequisite for the common case, and making the common case depend on a network round
trip would be strictly worse.

Resolution is deliberately *conservative*: an unknown name returns no match rather than a guess.
Fabricating a structure from a name is the one failure mode that would be worse than the gap — a
wrong structure propagates silently into a calculation, a fingerprint search, and a proposed note.

**There was a density table here, and it is gone.** `density_of` and its 23-entry
`_RAW_DENSITIES` block existed for `stoichiometry_table` — the comment argued its correctness
against that caller in the present tense, at length — and `stoichiometry_table` left this
repository with the `chem` capability. Nothing in `src/` called it, and unlike `core/http.py`'s
`error_detail` (deleted for the same reason on the same day) no test covered it either, so a
transposed digit in a solvent density would have gone unnoticed indefinitely. The measurement
behind `D-2026-08-02-a-solvent-charge-is-a-volume` stands and the ADR is the record of it; the
data belongs beside whatever computes a solvent charge next, which is `Chemclaw3-mcp`, with the
self-validating test `Chemclaw3-mcp:servers/props/tests/test_dataset.py` sets as the pattern.
"""

from typing import NamedTuple

from pydantic import BaseModel

from chemclaw.core.chem import (
    InvalidSmilesError,
    require_canonical_smiles,
    require_standard_smiles,
)

# Common bench reagents, solvents, bases, and catalysts, keyed by every spelling a chemist writes.
# Entries are grouped by role for review; the lookup itself is flat and folds case/punctuation.
# Each SMILES is canonicalized at import, so a typo here fails loudly at startup rather than
# silently yielding an unparseable structure to a calculator.
_RAW_SYNONYMS: dict[str, tuple[str, str]] = {
    # --- solvents ---
    "thf": ("C1CCOC1", "tetrahydrofuran"),
    "tetrahydrofuran": ("C1CCOC1", "tetrahydrofuran"),
    "2-methf": ("CC1CCCO1", "2-methyltetrahydrofuran"),
    "2-methyltetrahydrofuran": ("CC1CCCO1", "2-methyltetrahydrofuran"),
    "dmf": ("CN(C)C=O", "N,N-dimethylformamide"),
    "n,n-dimethylformamide": ("CN(C)C=O", "N,N-dimethylformamide"),
    "dimethylformamide": ("CN(C)C=O", "N,N-dimethylformamide"),
    "dmso": ("CS(C)=O", "dimethyl sulfoxide"),
    "dimethylsulfoxide": ("CS(C)=O", "dimethyl sulfoxide"),
    "dcm": ("ClCCl", "dichloromethane"),
    "dichloromethane": ("ClCCl", "dichloromethane"),
    "methylenechloride": ("ClCCl", "dichloromethane"),
    "mecn": ("CC#N", "acetonitrile"),
    "acn": ("CC#N", "acetonitrile"),
    "acetonitrile": ("CC#N", "acetonitrile"),
    "etoac": ("CCOC(C)=O", "ethyl acetate"),
    "ethylacetate": ("CCOC(C)=O", "ethyl acetate"),
    "meoh": ("CO", "methanol"),
    "methanol": ("CO", "methanol"),
    "etoh": ("CCO", "ethanol"),
    "ethanol": ("CCO", "ethanol"),
    "ipa": ("CC(C)O", "isopropanol"),
    "isopropanol": ("CC(C)O", "isopropanol"),
    "2-propanol": ("CC(C)O", "isopropanol"),
    "toluene": ("Cc1ccccc1", "toluene"),
    "phme": ("Cc1ccccc1", "toluene"),
    "dioxane": ("C1COCCO1", "1,4-dioxane"),
    "1,4-dioxane": ("C1COCCO1", "1,4-dioxane"),
    "dme": ("COCCOC", "1,2-dimethoxyethane"),
    "nmp": ("CN1CCCC1=O", "N-methyl-2-pyrrolidone"),
    "dmac": ("CN(C)C(C)=O", "N,N-dimethylacetamide"),
    "heptane": ("CCCCCCC", "n-heptane"),
    "hexane": ("CCCCCC", "n-hexane"),
    "water": ("O", "water"),
    "diethylether": ("CCOCC", "diethyl ether"),
    "et2o": ("CCOCC", "diethyl ether"),
    "mtbe": ("COC(C)(C)C", "methyl tert-butyl ether"),
    "acetone": ("CC(C)=O", "acetone"),
    "aceticacid": ("CC(O)=O", "acetic acid"),
    "acoh": ("CC(O)=O", "acetic acid"),
    # --- amine bases ---
    "dipea": ("CCN(C(C)C)C(C)C", "N,N-diisopropylethylamine"),
    "hunigsbase": ("CCN(C(C)C)C(C)C", "N,N-diisopropylethylamine"),
    "n,n-diisopropylethylamine": ("CCN(C(C)C)C(C)C", "N,N-diisopropylethylamine"),
    "tea": ("CCN(CC)CC", "triethylamine"),
    "et3n": ("CCN(CC)CC", "triethylamine"),
    "triethylamine": ("CCN(CC)CC", "triethylamine"),
    "nmm": ("CN1CCOCC1", "N-methylmorpholine"),
    "pyridine": ("c1ccncc1", "pyridine"),
    "dmap": ("CN(C)c1ccncc1", "4-dimethylaminopyridine"),
    "dbu": ("C1CCC2=NCCCN2CC1", "1,8-diazabicyclo[5.4.0]undec-7-ene"),
    # --- inorganic bases / salts ---
    "k2co3": ("[K+].[K+].[O-]C([O-])=O", "potassium carbonate"),
    "potassiumcarbonate": ("[K+].[K+].[O-]C([O-])=O", "potassium carbonate"),
    "cs2co3": ("[Cs+].[Cs+].[O-]C([O-])=O", "cesium carbonate"),
    "na2co3": ("[Na+].[Na+].[O-]C([O-])=O", "sodium carbonate"),
    "nahco3": ("[Na+].OC([O-])=O", "sodium bicarbonate"),
    "naoh": ("[Na+].[OH-]", "sodium hydroxide"),
    "koh": ("[K+].[OH-]", "potassium hydroxide"),
    "k3po4": ("[K+].[K+].[K+].[O-]P([O-])([O-])=O", "potassium phosphate"),
    "nah": ("[Na+].[H-]", "sodium hydride"),
    "lda": ("CC(C)[N-]C(C)C.[Li+]", "lithium diisopropylamide"),
    "nabh4": ("[Na+].[BH4-]", "sodium borohydride"),
    "lialh4": ("[Li+].[AlH4-]", "lithium aluminium hydride"),
    # --- palladium catalysts / ligands ---
    "pd(oac)2": ("CC(=O)O[Pd]OC(C)=O", "palladium(II) acetate"),
    "palladiumacetate": ("CC(=O)O[Pd]OC(C)=O", "palladium(II) acetate"),
    "pd(dppf)cl2": (
        "Cl[Pd]Cl.c1ccc(cc1)P(c1ccccc1)[CH]1[CH][CH][CH][CH]1[Fe][CH]1[CH][CH][CH]"
        "[CH]1P(c1ccccc1)c1ccccc1",
        "[1,1'-bis(diphenylphosphino)ferrocene]palladium(II) dichloride",
    ),
    "pph3": ("c1ccc(cc1)P(c1ccccc1)c1ccccc1", "triphenylphosphine"),
    "xphos": ("CC(C)c1cc(C(C)C)c(c(c1)C(C)C)-c1ccccc1P(C1CCCCC1)C1CCCCC1", "XPhos"),
    # --- coupling / activating reagents ---
    "tbtu": (
        "CN(C)C(=[N+](C)C)On1nnc2ccccc21.F[B-](F)(F)F",
        "TBTU",
    ),
    "hatu": (
        "CN(C)C(=[N+](C)C)On1nnc2cccnc12.F[P-](F)(F)(F)(F)F",
        "HATU",
    ),
    "edc": ("CCN=C=NCCCN(C)C", "EDC"),
    "dcc": ("C1CCC(CC1)N=C=NC1CCCCC1", "DCC"),
    "socl2": ("O=S(Cl)Cl", "thionyl chloride"),
    "tfa": ("OC(=O)C(F)(F)F", "trifluoroacetic acid"),
    "tfaa": ("FC(F)(F)C(=O)OC(=O)C(F)(F)F", "trifluoroacetic anhydride"),
    "boc2o": ("CC(C)(C)OC(=O)OC(=O)OC(C)(C)C", "di-tert-butyl dicarbonate"),
    "mscl": ("CS(Cl)(=O)=O", "methanesulfonyl chloride"),
    "tscl": ("Cc1ccc(cc1)S(Cl)(=O)=O", "tosyl chloride"),
    # --- oxidants / peroxides ---
    "mcpba": ("OOC(=O)c1cccc(Cl)c1", "meta-chloroperoxybenzoic acid"),
    "h2o2": ("OO", "hydrogen peroxide"),
    "hydrogenperoxide": ("OO", "hydrogen peroxide"),
    "tbhp": ("CC(C)(C)OO", "tert-butyl hydroperoxide"),
    "oxone": ("[K+].[K+].OOS([O-])(=O)=O.[O-]S(=O)(=O)O", "Oxone"),
    "naio4": ("[Na+].[O-][I](=O)(=O)=O", "sodium periodate"),
    # Sodium peroxide beside hydrogen peroxide, because the two are one hazard written two ways and
    # a table holding only the liquid could not express the solid. It is also the molecule whose
    # SMILES — one-coordinate anions rather than a HO-OH — has now defeated three separate screening
    # patterns in `Chemclaw3-mcp`'s hazard table, so having it here is what lets a test drive the
    # screen with the reagent a chemist actually names.
    "na2o2": ("[Na+].[O-][O-].[Na+]", "sodium peroxide"),
    "sodiumperoxide": ("[Na+].[O-][O-].[Na+]", "sodium peroxide"),
    # --- reductants held as salts, whose free-base spelling a screen must not depend on ---
    #
    # Hydrazine is the reagent a bench chemist weighs out, and the table held none of it at all —
    # so the `hydrazine` structural rule and the hydrazine arm of `oxidizer-with-reductant`, both
    # widened twice for exactly these spellings, could not be exercised against a *named* reagent.
    # The catalogue forms are the salts, which is why the widening was needed: their nitrogen is
    # `NX4+`, and a neutral-only pattern passes them.
    "hydrazine": ("NN", "hydrazine"),
    "n2h4": ("NN", "hydrazine"),
    "hydrazinehydrate": ("NN.O", "hydrazine hydrate"),
    "hydrazinehydrochloride": ("Cl.NN", "hydrazine hydrochloride"),
    "hydrazinesulfate": ("NN.OS(=O)(=O)O", "hydrazine sulfate"),
    "udmh": ("CN(C)N", "1,1-dimethylhydrazine"),
    "1,1-dimethylhydrazine": ("CN(C)N", "1,1-dimethylhydrazine"),
    "phenylhydrazine": ("NNc1ccccc1", "phenylhydrazine"),
    # --- azides / energetic reagents ---
    "nan3": ("[Na+].[N-]=[N+]=[N-]", "sodium azide"),
    "sodiumazide": ("[Na+].[N-]=[N+]=[N-]", "sodium azide"),
    "dppa": (
        "c1ccc(cc1)OP(=O)(N=[N+]=[N-])Oc1ccccc1",
        "diphenylphosphoryl azide",
    ),
    "tmsn3": ("C[Si](C)(C)N=[N+]=[N-]", "trimethylsilyl azide"),
}


def _normalize(name: str) -> str:
    """Fold a written name to its lookup key: case, whitespace, and separator punctuation."""
    folded = name.strip().lower()
    for noise in (" ", "-", "_", "'", "’"):
        folded = folded.replace(noise, "")
    return folded


def _build_table() -> dict[str, tuple[str, str]]:
    """Canonicalize every entry once at import, so a bad table entry fails loudly and early."""
    table: dict[str, tuple[str, str]] = {}
    for key, (smiles, display) in _RAW_SYNONYMS.items():
        try:
            table[_normalize(key)] = (require_canonical_smiles(smiles), display)
        except InvalidSmilesError as exc:  # pragma: no cover - a table typo, caught at import
            raise ValueError(f"reagent table entry {key!r} has unparseable SMILES: {exc}") from exc
    return table


_TABLE = _build_table()

# Reverse map: canonical SMILES -> preferred display name, for rendering a structure back as the
# name a chemist would recognise. First spelling wins, which is why the table lists the common
# abbreviation before the systematic name for each reagent.
_BY_STRUCTURE: dict[str, str] = {}
for _key, (_smiles, _display) in _TABLE.items():
    _BY_STRUCTURE.setdefault(_smiles, _display)


class _Compound(NamedTuple):
    """One entry of the compound-level index: what to call it, and every way it is written."""

    display: str
    synonyms: list[str]


def _index_by_compound() -> dict[str, _Compound]:
    """Index the table a second time, on the standardized SMILES — the "same compound?" key.

    The canonical index above cannot answer for a reagent whose two keys differ, and seven shipped
    ones do: DMSO's S=O normalizes to a charge-separated sulfoxide, TBTU and HATU lose their
    counterion, LDA loses its lithium. Everything downstream of `compound_id` — the compound note,
    the fingerprint row, `memory.progression`'s species names — holds the *standardized* string, so
    without this index a DMSO note rendered an anonymous structure with no name on it.

    Built once at import rather than scanned per lookup, because the callers are per-note loops and
    the scan would re-standardize the whole table each time. First spelling still wins the display
    name, for the same reason it does above.
    """
    synonyms: dict[str, list[str]] = {}
    displays: dict[str, str] = {}
    for key, (smiles, display) in _TABLE.items():
        standard = require_standard_smiles(smiles)
        displays.setdefault(standard, display)
        synonyms.setdefault(standard, []).append(key)
    return {
        standard: _Compound(displays[standard], sorted(spellings))
        for standard, spellings in synonyms.items()
    }


_BY_COMPOUND = _index_by_compound()


class ResolvedCompound(BaseModel):
    """One resolved identity: the canonical structure plus the name it was recognised as."""

    query: str
    smiles: str
    name: str
    # How the identity was established, so a caller (and the agent) can weigh it: `synonym` is the
    # curated table, `smiles` means the query already was a structure.
    source: str


def resolve_compound_name(name: str) -> ResolvedCompound | None:
    """Resolve a written reagent name (or a SMILES) to a canonical structure, or `None`.

    Returns `None` rather than guessing: a fabricated structure propagates silently into a
    calculation, a similarity search, and eventually a proposed note, which is strictly worse than
    an honest miss.
    """
    lookup = _TABLE.get(_normalize(name))
    if lookup is not None:
        smiles, display = lookup
        return ResolvedCompound(query=name, smiles=smiles, name=display, source="synonym")
    # A caller may already hold a structure; accepting it here means one entry point for
    # "give me the canonical form of whatever the chemist typed". `require_` (not the lenient
    # `canonical_smiles`, which returns its input unparsed) is essential: the lenient variant
    # would resolve any unknown name to itself, turning every miss into a fabricated structure —
    # exactly the failure this module exists to prevent.
    try:
        canonical = require_canonical_smiles(name)
    except InvalidSmilesError:
        return None
    return ResolvedCompound(
        query=name,
        smiles=canonical,
        name=_BY_STRUCTURE.get(canonical, name),
        source="smiles",
    )


def known_names() -> list[str]:
    """Every recognised spelling, sorted — what a caller can offer as a suggestion on a miss."""
    return sorted(_TABLE)


def display_name(smiles: str) -> str | None:
    """The recognised name for a structure, or `None` if it is not a known reagent.

    The exact structure first, then the *compound* it standardizes to. The second lookup is what
    lets a caller holding a standardized SMILES — a compound note, a `memory.progression` species
    list — still name DMSO or TBTU, whose two keys differ. It is a lookup, never a guess: an
    unrecognised structure still returns `None` rather than a fabricated name.
    """
    try:
        canonical = require_canonical_smiles(smiles)
    except InvalidSmilesError:
        return None
    exact = _BY_STRUCTURE.get(canonical)
    if exact is not None:
        return exact
    compound = _BY_COMPOUND.get(require_standard_smiles(canonical))
    return compound.display if compound is not None else None


def synonyms_of(smiles: str) -> list[str]:
    """Every recognised spelling of this *compound*, sorted — the KNW-4 controlled vocabulary.

    The compound question, not the structure question, because the caller writing these into a
    note body already holds the standardized SMILES its id was hashed from. Empty for an
    unrecognised or unparseable structure, so a caller can write the line only when there is one.
    """
    try:
        compound = _BY_COMPOUND.get(require_standard_smiles(smiles))
    except InvalidSmilesError:
        return []
    return list(compound.synonyms) if compound is not None else []
