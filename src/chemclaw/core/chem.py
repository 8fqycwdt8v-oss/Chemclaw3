"""Shared cheminformatics helpers: the one definition of "the same molecule".

Two questions, two pairs of names, so a caller cannot pick the wrong one by omitting a flag:

- `canonical_smiles` / `require_canonical_smiles` answer "is this the same structure?": RDKit
  canonical SMILES, spelling only. They key the calculation cache, workflow-dedup ids and the
  prediction ledger, where an anion is a different calculation from its conjugate acid.
- `standard_smiles` / `require_standard_smiles` answer "is this the same compound?": the
  standardization pipeline. They key `compound_id`, the fingerprint index and species matching in
  `memory.chains` and `memory.progression`, where a hydrochloride and its free base are one
  substance.

`standardize` is the conventional pipeline in the conventional order:

1. `Cleanup` (sanitize, disconnect metals, normalize functional groups), with the metal
   disconnection taken first so a cyclopentadienide is re-perceived aromatic (`_cleaned`); atom
   maps are cleared, since they are reaction bookkeeping, not structure.
2. Keep the one fragment `_is_organic` names, discarding a spectator only if it is charged, a
   solvent RDKit's list knows, or an ionisable neutral acid the organic fragment could be the salt
   of (`_IONISABLE_NEUTRAL_ACIDS`, `_can_take_the_proton`).
3. `Uncharger`, so a carboxylate meets its acid, re-perceived (`_uncharged`).
4. `TautomerEnumerator.Canonicalize`, one representative per tautomer set, configured to keep sp3
   and bond stereo: RDKit's defaults erase stereocentres a transform touches, which would merge
   enantiomers and E/Z isomers into one compound.

Steps 2 and 3 assert "the counterion is not part of the identity", and `standardize` declines to
apply them where that is false: when nothing organic remains (NaOH, K2CO3), when the metal is the
chemistry (a d/f-block metal, `_REACTIVE_METALS`, or a metal-carbon bond such as n-BuLi;
`_metal_is_the_compound`), when two or more fragments are organic (a solvate or co-crystal names
no winner), and when neutralizing would remove a hydride rather than add a proton
(`_neutralization_is_protonation`). An alkali salt of an organic conjugate acid (KOtBu, NaOMe, LDA)
still collapses onto the acid, as sodium acetate does; separating those would need a pKa-shaped
rule (`D-2026-09-09-a-map-number-is-not-a-molecule`), and `tests/test_compound_identity.py` pins
the collapse.
"""

from functools import lru_cache
from hashlib import sha256

import rdkit
from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash

# Bumped whenever the pipeline below changes what it collapses. Folded into the fingerprint
# `definition` strings, so rows indexed under an older notion of sameness fall out of similarity
# search instead of being compared with newer rows.
#
# A bump retires every row under the old definition, not just the rows whose form changed, and the
# fingerprint tables are append-only (`durable/retention.py`), so it costs storage whenever taken.
# `chemclaw.cli.rekey_compounds` (`make rekey-compounds`) re-fingerprints shelved rows and writes a
# `supersedes` link from each compound note's new id to its old one, so pre-bump citations still
# resolve. `tests/test_compound_identity.py` pins what `standardize` does at this version.
STANDARDIZATION_VERSION = "std12"

# The d- and f-block by atomic number (Sc-Zn, Y-Cd, La-Hg with the lanthanides, Ac onward): the
# metals a synthesis uses to do the chemistry, so a species containing one is identified as the
# whole complex. Group-1/2 counterions are outside it and keep collapsing. RDKit has no block
# predicate, so the ranges are spelled out.
_REACTIVE_METALS = frozenset((*range(21, 31), *range(39, 49), *range(57, 81), *range(89, 113)))

# Every metal, for the metal-carbon test. Metalloids (B, Si, Ge, As, Sb, Te) are excluded: boronic
# acids and silyl reagents are organic reagents.
_METALS = _REACTIVE_METALS | frozenset(
    (
        # the s-block below helium — groups 1 and 2
        *range(3, 5),
        *range(11, 13),
        *range(19, 21),
        *range(37, 39),
        *range(55, 57),
        *range(87, 89),
        # the post-transition metals — Al, Ga, In, Sn, Tl, Pb, Bi, Po
        13,
        31,
        49,
        50,
        81,
        82,
        83,
        84,
    )
)

# RDKit's curated salt and solvent list, built once (construction parses a catalogue). Consulted
# only for neutral spectators; charged ones are recognised by their charge, since this
# pharmaceutical salt list does not know e.g. tetrafluoroborate.
_KNOWN_SPECTATORS = rdMolStandardize.FragmentRemover()

# Neutral acids that can also be written as their anion and that RDKit's catalogue omits, so a salt
# gets one `compound_id` whether written ionic or neutral. A small, closed table in place of a
# pKa-shaped rule. Acids the catalogue carries already agree from both spellings; adducts that
# cannot ionise (H2O2, BH3, I2, CO2) are not listed and stay. Matched after `Cleanup`, which
# rewrites perchloric acid into a charge-separated form.
_IONISABLE_NEUTRAL_ACIDS: dict[str, tuple[str, ...]] = {
    "perchloric acid": ("OCl(=O)(=O)=O",),
    "tetrafluoroboric acid": ("F[B-](F)(F)[FH+]",),
    "sulfamic acid": ("NS(=O)(=O)O",),
    "thiocyanic acid": ("SC#N", "N=C=S"),
    "carbonic acid": ("OC(=O)O",),
    "hypophosphorous acid": ("O[PH2]=O",),
    "boric acid": ("OB(O)O",),
}
_IONISABLE_NEUTRAL_SPECTATORS = frozenset(
    Chem.MolToSmiles(rdMolStandardize.Cleanup(Chem.MolFromSmiles(spelling)))
    for spellings in _IONISABLE_NEUTRAL_ACIDS.values()
    for spelling in spellings
)

# A neutral acid beside a fragment is read as its counterion only if the fragment has a site that
# can take the proton; otherwise the pair is a mixture (a boronic acid beside boric acid) and both
# are kept. Sites:
#
# - an aliphatic amine (not amide, carbamate, urea, sulfonamide, aniline, aromatic, N-N or N-O);
# - an amidine or guanidine sp2 nitrogen, not acylated or sulfonylated;
# - a basic aza-aromatic nitrogen (pyridine-type, imidazole N3), two-coordinate and neutral;
# - or a fragment already carrying a net positive charge.
#
# Deliberately coarse and structural: it asks whether a salt could form, not how strong it is.
_BASIC_SITES = tuple(
    Chem.MolFromSmarts(pattern)
    for pattern in (
        "[NX3;+0;!$(N~a);!$(N-[#6,#7,#15,#16]=[#7,#8,#16]);!$(N-[#7,#8]);!$(N-C#N);!$(N=*)]",
        "[NX2;+0;!$(N-[#6,#16]=[#8,#16])]=[CX3;!a]-[#7X3]",
        "[nX2;+0]",
    )
)


def _can_take_the_proton(fragment: Chem.Mol) -> bool:
    """Whether `fragment` has a site an ionisable neutral acid beside it could protonate.

    See `_BASIC_SITES`. Net charge, not any positive atom, because a nitro nitrogen carries a formal
    `+` and is no base.
    """
    if Chem.GetFormalCharge(fragment) > 0:
        return True
    return any(fragment.HasSubstructMatch(site) for site in _BASIC_SITES)


_TAUTOMERS = rdMolStandardize.TautomerEnumerator()
_TAUTOMERS.SetRemoveSp3Stereo(False)
_TAUTOMERS.SetRemoveBondStereo(False)


@lru_cache(maxsize=4096)
def _standardized(smiles: str) -> str | None:
    """The standardized canonical SMILES of `smiles`, or None when it does not parse.

    Cached because tautomer canonicalization is expensive and callers loop over every component of
    every reaction. Pure in its argument and bounded.
    """
    mol = _bounded_mol(smiles)
    if mol is None:
        return None
    return str(Chem.MolToSmiles(standardize(mol)))


def _is_organic(fragment: Chem.Mol) -> bool:
    """Whether a fragment holds a carbon bonded to hydrogen, to carbon, or to two nitrogens.

    The nitrogen clause also requires three or more heavy neighbours on that carbon.

    Not "contains a carbon": that would call carbonate and cyanide organic, collapsing K2CO3,
    Cs2CO3, Na2CO3 and NaHCO3 into one compound. The nitrogen clause makes urea, guanidine, thiourea
    and melamine organic, so their salts collapse onto the free base regardless of where a methyl is
    drawn. The coordination requirement keeps the linear family (cyanide, cyanate, thiocyanate,
    cyanamide) inorganic, so their alkali salts stay distinct. A structural test, so no reagent
    table needs maintaining.
    """
    for atom in fragment.GetAtoms():
        if atom.GetAtomicNum() != 6:
            continue
        if atom.GetTotalNumHs(includeNeighbors=True) > 0:
            return True
        neighbours = [neighbour.GetAtomicNum() for neighbour in atom.GetNeighbors()]
        if 6 in neighbours:
            return True
        if len(neighbours) >= 3 and neighbours.count(7) >= 2:
            return True
    return False


def _is_organometallic(mol: Chem.Mol) -> bool:
    """Whether the species has a metal-carbon bond, the bond that is the reagent.

    An organolithium, Grignard, cuprate or organozinc is defined by its M-C bond; the hydrocarbon
    left after breaking it is a different substance. Ionic salts of the same metals (sodium
    benzoate, LDA) have no M-C bond and keep collapsing.
    """
    for bond in mol.GetBonds():
        ends = {bond.GetBeginAtom().GetAtomicNum(), bond.GetEndAtom().GetAtomicNum()}
        if 6 in ends and ends & _METALS:
            return True
    return False


def _metal_is_the_compound(original: Chem.Mol, cleaned: Chem.Mol) -> bool:
    """Whether the species' metal is the chemistry, so neither stripping nor neutralizing applies.

    Each check reads the stage that still holds its evidence: the cleaned molecule for a reactive
    metal (after `Cleanup` has disconnected it into a separate fragment), and the original for a
    metal-carbon bond (`Cleanup` breaks some M-C bonds, e.g. Al-C, destroying the evidence). It
    gates neutralization too, because the charges on a metal complex balance its metal.
    """
    if _is_organometallic(original):
        return True  # the M–C bond is the reagent; the hydrocarbon left without it is not
    return any(atom.GetAtomicNum() in _REACTIVE_METALS for atom in cleaned.GetAtoms())


def _hydrogen_count(mol: Chem.Mol) -> int:
    """Every hydrogen in a species, implicit on a heavy atom or an atom in its own right.

    Both forms count, since `[BH4-]` carries its hydrogens on boron and `[H-]` is an atom.
    """
    return sum(a.GetTotalNumHs() + (1 if a.GetAtomicNum() == 1 else 0) for a in mol.GetAtoms())


def _neutralization_is_protonation(before: Chem.Mol, after: Chem.Mol) -> bool:
    """Whether `Uncharger` reached the neutral species by adding protons, as the strip assumes.

    For an anion, neutralization must add hydrogens; if it removed one (sodium triacetoxyborohydride
    becoming triacetoxyborane) the species is kept as written. Tested on what the transformation
    did, not on an element list, so it covers anions not yet seen.

    For a cation (a protonated amine, pyridinium) neutralization legitimately removes a proton, so
    the cation arm is exempt, but only when no fragment in the string is anionic: the net charge of
    a mixed string says nothing about the anion inside it. A string carrying both an anion and a
    cation falls to the net hydrogen count and may be kept charged; keeping a species as written
    costs a cache miss, while a per-fragment pairing could silently produce a wrong molecule.
    """
    if Chem.GetFormalCharge(before) > 0 and not any(
        Chem.GetFormalCharge(f) < 0 for f in Chem.GetMolFrags(before, asMols=True)
    ):
        return True
    return _hydrogen_count(after) >= _hydrogen_count(before)


#: The disconnector `Cleanup` runs, run ahead of it instead — see `_cleaned`.
_METAL_DISCONNECTOR = rdMolStandardize.MetalDisconnector()


def _cleaned(mol: Chem.Mol) -> Chem.Mol:
    """`Cleanup`, with the metal disconnection taken before it rather than inside it.

    `Cleanup`'s own disconnector runs after sanitizing, so a cyclopentadienide freed from a metal
    stayed flagged non-aromatic and `Reionize` moved protons across fragments, making `standardize`
    non-idempotent on ferrocenes. Disconnecting first lets `Cleanup`'s `RemoveHs` re-perceive the
    ring. `Reionize` can still oscillate on a vinyl anion written as one (`[C-]1=CCC=C1`), a
    spelling the pipeline no longer produces.
    """
    return rdMolStandardize.Cleanup(_METAL_DISCONNECTOR.Disconnect(mol))


def _uncharged(mol: Chem.Mol) -> Chem.Mol:
    """`Uncharger`'s neutral form of `mol`, with its aromaticity re-perceived.

    `Uncharger` protonates an aromatic cyclopentadienide but leaves it flagged aromatic, which
    writes an unparseable SMILES. A sanitized copy is returned, or the unsanitized result if
    sanitizing fails.
    """
    uncharged = rdMolStandardize.Uncharger().uncharge(mol)
    copy = Chem.Mol(uncharged)
    try:
        Chem.SanitizeMol(copy)
    except Chem.MolSanitizeException:  # pragma: no cover - no measured case
        return uncharged
    return copy


def standardize(mol: Chem.Mol) -> Chem.Mol:
    """Apply the standardization pipeline to a parsed molecule (see the module docstring).

    The number of organic fragments decides the strip: exactly one means a salt, solvate or adduct
    of that fragment, and each other fragment is judged on its own; two or more name no winner, so
    the species is kept whole; zero is a wholly inorganic reagent with no parent to keep.

    `Uncharger` runs whenever some fragment is organic (so a co-crystal and its ion-pair spelling
    still meet), never on a wholly inorganic species (a lone `[OH-]` must not become water), and its
    result is kept only if `_neutralization_is_protonation` agrees.
    """
    cleaned = _cleaned(mol)
    # Atom maps first and unconditionally: every exit below becomes a `compound_id`. Cleared on
    # `cleaned`, a copy this function owns, so the caller's molecule is not modified.
    for atom in cleaned.GetAtoms():
        atom.SetAtomMapNum(0)
    if _metal_is_the_compound(mol, cleaned):
        return _TAUTOMERS.Canonicalize(cleaned)
    organic_fragments = [f for f in Chem.GetMolFrags(cleaned, asMols=True) if _is_organic(f)]
    if not organic_fragments:
        return _TAUTOMERS.Canonicalize(cleaned)  # no organic parent to keep, nothing to neutralize
    if len(organic_fragments) == 1:
        # The one organic fragment is the parent, by this module's own `_is_organic`; RDKit's
        # `FragmentParent` counts hydrogens and could pick ammonium over formate.
        #
        # Each other fragment is discarded only if it is charged (an inorganic ion beside one
        # organic fragment is its counterion), a solvent RDKit's list knows, or an ionisable neutral
        # acid the organic fragment could be the salt of. Anything else neutral (H2O2, a co-former)
        # keeps the string whole. Asked per spectator, so one unrecognised neutral does not preserve
        # the others.
        survived = {
            Chem.MolToSmiles(f)
            for f in Chem.GetMolFrags(_KNOWN_SPECTATORS.remove(cleaned), asMols=True)
        }
        # One of the ionisable neutral acids is a counterion only beside a fragment that could
        # have taken its proton; beside anything else it is a second component, and stays.
        salt_former = _can_take_the_proton(organic_fragments[0])
        kept = [organic_fragments[0]] + [
            f
            for f in Chem.GetMolFrags(cleaned, asMols=True)
            if not _is_organic(f)
            and Chem.GetFormalCharge(f) == 0
            and Chem.MolToSmiles(f) in survived
            and not (salt_former and Chem.MolToSmiles(f) in _IONISABLE_NEUTRAL_SPECTATORS)
        ]
        # Rebuilt from the kept fragments as one sanitized molecule; the common case (everything
        # else discarded) is the organic fragment itself.
        cleaned = (
            organic_fragments[0]
            if len(kept) == 1
            else Chem.MolFromSmiles(".".join(Chem.MolToSmiles(f) for f in kept))
        )
    uncharged = _uncharged(cleaned)
    if not _neutralization_is_protonation(cleaned, uncharged):
        return _TAUTOMERS.Canonicalize(cleaned)  # not a conjugate acid; keep the anion as written
    return _TAUTOMERS.Canonicalize(uncharged)


class InvalidSmilesError(ChemclawError):
    """A SMILES string that RDKit cannot parse.

    A `ChemclawError`, so batch boundaries treat it as bad data and Temporal does not retry it.
    """


def _oversized(smiles: str, mol: Chem.Mol | None) -> bool:
    """Whether a string or its parsed molecule is past the size the writer can survive.

    The one size gate for strict and lenient helpers alike: RDKit's SMILES writer and tautomer
    canonicalizer recurse without bound and SIGSEGV (killing the process) on a large linear
    molecule. Length is the cheap pre-filter, atom count the real bound
    (`molecule_max_smiles_length`, `molecule_max_atoms`).
    """
    if len(smiles) > settings.molecule_max_smiles_length:
        return True
    return mol is not None and mol.GetNumAtoms() > settings.molecule_max_atoms


def _bounded_mol(smiles: str) -> Chem.Mol | None:
    """Parse `smiles`, returning None if it is unparseable or too large to write safely.

    The lenient counterpart to `require_molecule`: ELN and memory callers must not abort on one odd
    label, and must never hand an oversized molecule to the writer, so both cases pass through.
    """
    if len(smiles) > settings.molecule_max_smiles_length:
        return None
    mol = Chem.MolFromSmiles(smiles)
    if mol is None or _oversized(smiles, mol):
        return None
    return mol


def canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, or the input unchanged if it does not parse.

    Spelling-normalized ("same structure"). Lenient so ingestion never aborts on one odd label; use
    `require_canonical_smiles` where an unparseable structure must be rejected.
    """
    mol = _bounded_mol(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else smiles


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` whole.

    The one acceptance test, shared by the strict helpers and by callers that need the molecule
    itself. It rejects three inputs RDKit silently narrows to a different, smaller molecule:

    - embedded whitespace (RDKit stops parsing at it, so `"CCO junk"` is ethanol);
    - the empty string (a molecule with no atoms);
    - a non-ASCII character at either end (RDKit skips edge runs, so `"°C"` is methane), checked on
      the string because the parsed molecule carries no trace of it.

    Surrounding whitespace is stripped rather than refused. The message quotes the caller's original
    string.
    """
    stripped = smiles.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        raise InvalidSmilesError(f"invalid SMILES (empty or contains whitespace): {smiles!r}")
    if not stripped.isascii():
        raise InvalidSmilesError(f"invalid SMILES (non-ASCII characters): {smiles!r}")
    # Refuse an oversized string before RDKit sees it (see `_oversized`).
    if len(stripped) > settings.molecule_max_smiles_length:
        raise InvalidSmilesError(
            f"SMILES exceeds {settings.molecule_max_smiles_length} characters "
            f"({len(stripped)}); pass a smaller molecule"
        )
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    if _oversized(stripped, mol):
        raise InvalidSmilesError(
            f"molecule has {mol.GetNumAtoms()} atoms, over the {settings.molecule_max_atoms} "
            f"limit; RDKit canonicalization is unbounded-recursive and would crash the process"
        )
    return mol


def element_counts(smiles: str) -> dict[str, int]:
    """How many atoms of each element the molecule has, hydrogens included.

    Hydrogens are made explicit first, since RDKit's implicit-H model omits them from `GetAtoms()`.
    Uses the strict parse, for proposed reactions (`protocols.checks.atom_balance`);
    `ingest.eln.validate._elements` keeps its own lenient parse because oversized recorded molecules
    are legitimate to ingest.

    Raises:
        InvalidSmilesError: `smiles` is not a molecule RDKit reads whole (see `require_molecule`).
    """
    counts: dict[str, int] = {}
    for atom in Chem.AddHs(require_molecule(smiles)).GetAtoms():
        symbol = str(atom.GetSymbol())
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts


def require_canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, raising `InvalidSmilesError` if it does not parse.

    For keys that must reject bad input and must not distinguish spellings: the calculation cache
    and durable dedup ids, so `"CCO"` and `"OCC"` share one entry. Parses through
    `require_molecule`.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))


def standard_smiles(smiles: str) -> str:
    """The standardized canonical SMILES, or the input unchanged if it does not parse.

    "Is this the same compound?": salts stripped, charges neutralized where possible, one tautomer
    per set. Use `canonical_smiles` where an anion is genuinely a different thing to compute.
    Lenient for the same reason as `canonical_smiles`.
    """
    standardized = _standardized(smiles)
    return standardized if standardized is not None else smiles


def require_standard_smiles(smiles: str) -> str:
    """The standardized canonical SMILES, raising `InvalidSmilesError` if it does not parse.

    Validates through `require_molecule`, then discards the molecule and standardizes through the
    string-keyed `_standardized` cache, which is what makes loop callers affordable.
    """
    require_molecule(smiles)
    standardized = _standardized(smiles.strip())
    if standardized is None:  # pragma: no cover - unreachable once the parse above succeeded
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    return standardized


def substructure_pattern(query: str) -> Chem.Mol:
    """Compile a substructure query (SMARTS first, then SMILES) or raise `InvalidSmilesError`.

    SMARTS first because it is the superset language; the SMILES fallback lets a plain fragment
    work. A zero-atom pattern is rejected, since it matches everything and would read as a finding.
    Shared by the fingerprint substructure search and the calibration outlier listing.
    """
    pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
    if pattern is None:
        raise InvalidSmilesError(f"unparseable substructure query: {query!r}")
    if pattern.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"empty substructure query (no atoms): {query!r}")
    return pattern


def compound_id(smiles: str) -> str:
    """The stable knowledge-graph note id for a molecule, derived from its structure.

    Structure-derived, so differently spelled sources reach one note. Here because its callers span
    layers that share nothing else (ingest writes the note, fingerprint connectors cite it), and a
    connector may not import the knowledge graph.
    """
    return compound_id_of_standard(require_standard_smiles(smiles))


def compound_id_of_standard(standard: str) -> str:
    """`compound_id` for a SMILES that is already standardized: the hash without the RDKit pass.

    For scans over stored, already-standardized structures, where re-standardizing each is too slow.
    Standardization is idempotent on its own output (`tests/test_compound_identity.py`). Anything
    not known to be standard goes through `compound_id`.
    """
    return f"compound-{stable_hash(standard, chars=12)}"


def torsion_handle(mol: Chem.Mol, bond: tuple[int, int]) -> str:
    """A content-addressed name for one rotatable bond: the verifying half of the handle.

    `Chemclaw3-mcp`'s `servers/chem` mints these and this repository checks them; neither may import
    the other, so the function is written twice and pinned by a shared table of literal handles that
    both suites assert.

    Atom indices are not names (the same indices pick a different bond once the SMILES is
    rewritten), so the two atoms are named by canonical symmetry class. The RDKit build is part of
    the payload, so a handle minted under another build fails to resolve rather than resolving to a
    different bond.

    Args:
        mol: The molecule the bond belongs to.
        bond: The bond's two atom indices, in either order.

    Returns:
        `tor_` followed by sixteen hex characters.
    """
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    low, high = sorted((ranks[bond[0]], ranks[bond[1]]))
    payload = f"{rdkit.__version__}|{Chem.MolToSmiles(mol)}|{low}-{high}"
    return f"tor_{sha256(payload.encode()).hexdigest()[:16]}"
