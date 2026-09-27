"""ECFP4 fingerprints — the molecule capability core (plan step 3.1).

Pure, GPU-free, model-free: a SMILES becomes an ECFP4 (Morgan radius 2, 2048-bit)
fingerprint via RDKit, stored as a fixed-width bitstring so it maps directly onto a
Postgres `bit(2048)` column. Radius and width come from config, so the fingerprint
definition is a versioned choice, not a magic number. Ranking (`tanimoto`) and the store
are the domain-neutral `chemclaw.science.fingerprints.store`; this module is only the
molecule-specific "SMILES → bits" step and holds no judgment (G6).
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

from chemclaw.core.chem import STANDARDIZATION_VERSION, standardize
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintInputError

# **Stereochemistry is part of the bits, and RDKit's default is that it is not.**
# `GetMorganGenerator` leaves `includeChirality` at False, so the Morgan invariant of a stereocentre
# is the invariant of the flat atom. Measured on this tree, that made every claim `core/chem.py`
# makes about stereo false of this fingerprint: (S)/(R)-naproxen, L/D-alanine, R/S-thalidomide and
# every E/Z pair produced *byte-identical* rows, tying at Tanimoto 1.0000. That is worse than a
# merged record, because the two are not merged everywhere — `standardize` keeps them apart, so
# each enantiomer holds its own `compound_id`, its own note and its own row, and
# `MoleculeHit.for_molecule` derives the citation from the stored structure. A search for
# (R)-naproxen therefore returned the (S) row at 1.0000, ranked *above* the query's own exact match
# by the label tiebreak, citing the other enantiomer's note. A score that says "identical" over a
# citation that says "a different compound" is a contradiction a reader cannot detect.
#
# **Not a setting**, unlike radius and width. Those are genuine deployment trades between resolution
# and storage; this one decides whether the index agrees with the identity function feeding it, and
# a deployment that turned it off would be re-introducing the defect rather than tuning anything.
# It is named in `molecule_definition` instead, which is where a thing that decides the bits belongs
# (`reaction_definition`'s `agents-excluded` token is the same argument on the reaction side).
#
# **Isotopes are deliberately still invisible**, and this does not change that: ECFP4's atom
# invariant is connectivity plus element, charge, degree and H count, so `CCO` and `[13CH3]CO` tie
# at 1.0000 with the flag on as well. That is what ECFP4 *is* rather than a defect here — an
# isotopologue is the same connectivity — and `compound_id` already tells the two apart, which is
# the half that keeps a labelled tracer from being filed as its unlabelled parent.
_INCLUDE_CHIRALITY = True


@lru_cache(maxsize=8)
def _generator(
    radius: int, n_bits: int, include_chirality: bool
) -> rdFingerprintGenerator.FingerprintGenerator64:
    """Cache the Morgan generator per (radius, bits, chirality) — constructing it is not free."""
    return rdFingerprintGenerator.GetMorganGenerator(
        radius=radius, fpSize=n_bits, includeChirality=include_chirality
    )


def _parse(smiles: str) -> Chem.Mol:
    """Parse a SMILES into an RDKit molecule, raising `FingerprintInputError` on failure.

    RDKit parses the empty string to a zero-atom Mol rather than failing; that would
    fingerprint to all zeros — a meaningless query/index entry — so it is rejected here
    too, mirroring rxnfp's empty-fingerprint guard (G4).
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise FingerprintInputError(f"unparseable SMILES: {smiles!r}")
    if mol.GetNumAtoms() == 0:
        raise FingerprintInputError(f"empty SMILES (no atoms): {smiles!r}")
    return mol


def ecfp_bitstring(smiles: str) -> str:
    """Return the ECFP4 fingerprint of `smiles` as a `settings.ecfp_bits`-long bitstring.

    The bitstring is the storage form (one char per bit, '0'/'1'), sized to the
    configured width so it inserts straight into the `bit(ecfp_bits)` column.

    **Fingerprinted after standardization**, so similarity answers the "same compound" question the
    index is asked. Without it a hydrochloride and its free base fingerprinted differently and each
    held its own row, so a search for one would rank the other as merely similar — and a chemist
    reading that result cannot tell it from two genuinely different molecules.
    """
    mol = standardize(_parse(smiles))
    generator = _generator(settings.ecfp_radius, settings.ecfp_bits, _INCLUDE_CHIRALITY)
    return str(generator.GetFingerprint(mol).ToBitString())


def molecule_definition() -> str:
    """The current ECFP definition signature stored on each molecule row.

    Two ECFP fingerprints of equal width but different radius are the same length yet
    incomparable, so the store records this signature per row and refuses to rank across
    signatures — changing `ecfp_radius`/`ecfp_bits` and re-indexing can't silently mix them.

    The **standardization version** is part of the signature for the same reason, and it is the
    less obvious half: a row indexed before molecule standardization was keyed to the molecule as
    written, so a hydrochloride and its free base held separate rows. Ranking those against rows
    built after standardization would answer a similarity question using two different notions of
    what a molecule *is*. Bumping the definition retires them until a re-index rebuilds them, which
    is the same failure-safe behaviour a changed radius already gets.

    **`chiral` is the third such token**, and it is derived from `_INCLUDE_CHIRALITY` rather than
    written as a literal beside it so the two cannot drift: a stereo-blind row and a stereo-aware
    one are the same width and are not comparable, so flipping the flag has to retire the rows by
    construction. Before it existed, every enantiomer pair in `tests/test_stereo_identity.py` held
    byte-identical bits while `core/chem.py` said in the present tense that stereo was folded into
    them (`D-2026-09-09-a-map-number-is-not-a-molecule`).
    """
    chirality = "chiral" if _INCLUDE_CHIRALITY else "flat"
    return (
        f"ecfp:r{settings.ecfp_radius}:b{settings.ecfp_bits}:{chirality}:{STANDARDIZATION_VERSION}"
    )
