"""Standardization must not merge stereoisomers into one identity.

`standardize` feeds `compound_id`, the ECFP4 and DRFP fingerprint rows and the knowledge graph's
note ids, so anything it collapses becomes *the same substance* everywhere downstream. RDKit's
`TautomerEnumerator` defaults `removeSp3Stereo` and `removeBondStereo` to True — a defensible rule
for a molecule in solution, where a tautomerising centre is not configurationally stable, and the
wrong rule for an identity function.

Left at the default this erased every stereocentre alpha to a carbonyl, which is most chiral drug
molecules: (S)/(R)-naproxen, L/D-alanine and R/S-thalidomide each produced one `compound_id`, one
fingerprint row and one note. The failure was not merely a merged record — chain detection then
built a product→reactant edge between a run that made one enantiomer and a run that consumed the
other, asserting a chemical relationship that does not exist.

These tests are written as pairs rather than as assertions about flags, because the flag is the
mechanism and the pair is the property. A future RDKit that renames the setter should fail here.
"""

import pytest
from rdkit import Chem

from chemclaw.core.chem import STANDARDIZATION_VERSION, standardize
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import (
    _generator,
    ecfp_bitstring,
    molecule_definition,
)
from chemclaw.science.fingerprints.store import tanimoto

# (name, SMILES A, SMILES B) — genuinely different substances that a chemist must never see merged.
ENANTIOMERS = [
    # Every one of these carries its stereocentre alpha to a carbonyl, which is exactly the case
    # the tautomer transform used to erase.
    ("alanine L/D", "C[C@@H](N)C(=O)O", "C[C@H](N)C(=O)O"),
    ("naproxen S/R", "COc1ccc2cc([C@H](C)C(=O)O)ccc2c1", "COc1ccc2cc([C@@H](C)C(=O)O)ccc2c1"),
    ("ibuprofen S/R", "CC(C)Cc1ccc([C@H](C)C(=O)O)cc1", "CC(C)Cc1ccc([C@@H](C)C(=O)O)cc1"),
    (
        "thalidomide R/S",
        "O=C1CC[C@H](N2C(=O)c3ccccc3C2=O)C(=O)N1",
        "O=C1CC[C@@H](N2C(=O)c3ccccc3C2=O)C(=O)N1",
    ),
    # No enolizable centre: this pair survived even before the fix, and is kept so a regression that
    # disabled standardization altogether would not look like a pass.
    ("2-butanol S/R", "CC[C@H](C)O", "CC[C@@H](C)O"),
]

# Double-bond geometry, which the sibling `removeBondStereo` default discarded. E/Z is only lost
# when a transform actually fires, so each of these pairs an enolizable centre with a stereo bond.
CIS_TRANS = [
    ("hex-4-en-2-one E/Z", r"C/C=C/CC(=O)C", r"C/C=C\CC(=O)C"),
    ("oct-4-en-2-one E/Z", r"CC/C=C/CC(=O)CC", r"CC/C=C\CC(=O)CC"),
    ("pent-3-en-2-one E/Z", r"C/C=C/C(=O)C", r"C/C=C\C(=O)C"),
]

# The same compound written two ways. Canonicalization exists to merge these, and the fix must not
# have bought stereo fidelity by switching the stage off.
TAUTOMER_PAIRS = [
    ("acetone keto/enol", "CC(C)=O", "CC(O)=C"),
    ("acetylacetone", "CC(=O)CC(C)=O", "CC(O)=CC(C)=O"),
    ("2-pyridone / 2-hydroxypyridine", "O=c1cccc[nH]1", "Oc1ccccn1"),
    ("cytosine-like amide", "Nc1cc[nH]c(=O)n1", "Nc1ccnc(O)n1"),
]


def _standardized(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    assert mol is not None, f"fixture SMILES does not parse: {smiles}"
    return str(Chem.MolToSmiles(standardize(mol)))


@pytest.mark.parametrize(("name", "left", "right"), ENANTIOMERS, ids=[c[0] for c in ENANTIOMERS])
def test_enantiomers_keep_separate_identities(name: str, left: str, right: str) -> None:
    assert Chem.CanonSmiles(left) != Chem.CanonSmiles(right), (
        f"{name}: the fixture pair is not actually two different molecules"
    )
    assert _standardized(left) != _standardized(right), (
        f"{name}: standardization merged two enantiomers into one identity, so they would share a "
        f"compound_id, a fingerprint row and a knowledge-graph note"
    )


@pytest.mark.parametrize(("name", "left", "right"), CIS_TRANS, ids=[c[0] for c in CIS_TRANS])
def test_double_bond_geometry_survives_a_firing_transform(name: str, left: str, right: str) -> None:
    assert Chem.CanonSmiles(left) != Chem.CanonSmiles(right), (
        f"{name}: the fixture pair is not actually two different molecules"
    )
    assert _standardized(left) != _standardized(right), (
        f"{name}: standardization merged E and Z into one identity"
    )


@pytest.mark.parametrize(
    ("name", "left", "right"), TAUTOMER_PAIRS, ids=[c[0] for c in TAUTOMER_PAIRS]
)
def test_tautomers_of_one_compound_still_unify(name: str, left: str, right: str) -> None:
    """The guard against fixing stereo by disabling canonicalization."""
    assert _standardized(left) == _standardized(right), (
        f"{name}: two tautomers of one compound no longer standardize to the same string, so "
        f"canonicalization has stopped doing its job"
    )


def test_standardization_version_moved_past_the_stereo_erasing_pipeline() -> None:
    """The version is folded into both fingerprint `definition` strings.

    Rows indexed while stereocentres were being erased describe a different notion of sameness, and
    must fall out of similarity search rather than be compared against rows built after the fix.
    """
    assert STANDARDIZATION_VERSION != "std4", (
        "the pipeline's behaviour changed but the version did not, so pre-fix fingerprint rows "
        "would be compared against post-fix ones under one definition string"
    )


def test_standardization_version_moved_past_the_atom_map_and_hydride_pipeline() -> None:
    """The same guard for D-2026-09-09, and the reaction side's *only* retirement lever.

    The molecule rows carry `chiral` in their own definition, so they would be retired by that
    token alone. The DRFP rows have no such token and two things changed under them: atom maps are
    cleared, and a neutralization that would remove a hydride no longer runs. `std6` rows
    describe a different notion of sameness and must fall out rather than be ranked against `std7`.
    """
    assert STANDARDIZATION_VERSION != "std6", (
        "atom-map clearing and the protonation guard changed what `standardize` collapses, so "
        "reaction rows built before them would be compared against rows built after"
    )


# --- the fingerprint half of the same claim (D-2026-09-09) -------------------------------------
#
# Everything above proves standardization *preserves* stereo. Nothing proved the fingerprints
# *carry* it, and they did not: `GetMorganGenerator` leaves `includeChirality` at RDKit's `False`
# default, so every pair above produced bit-identical ECFP4 rows. The two halves are not
# interchangeable — `compound_id` separating the enantiomers while the bits tie them at 1.0000 is
# strictly worse than either alone, because a similarity hit cites `compound_id`: the search
# reports "identical" and hands the reader a citation to the *other* enantiomer's note.


@pytest.mark.parametrize(("name", "left", "right"), ENANTIOMERS, ids=[c[0] for c in ENANTIOMERS])
def test_enantiomers_do_not_share_a_fingerprint_row(name: str, left: str, right: str) -> None:
    """Bits, not ids: this is the assertion `test_enantiomers_keep_separate_identities` cannot make.

    A Tanimoto floor would be the wrong shape here. The failure is a *tie* at exactly 1.0000 —
    `find_similar_molecules` orders by similarity and breaks ties on the label collation, so the
    enantiomer sorted first outranks the query's own exact match — and only inequality catches it.
    """
    assert ecfp_bitstring(left) != ecfp_bitstring(right), (
        f"{name}: the two enantiomers hold identical ECFP4 bits, so a search for one returns the "
        f"other at similarity 1.0000 citing a different compound note"
    )


@pytest.mark.parametrize(("name", "left", "right"), CIS_TRANS, ids=[c[0] for c in CIS_TRANS])
def test_double_bond_geometry_reaches_the_fingerprint_too(name: str, left: str, right: str) -> None:
    """Maleic and fumaric acid are not the same compound in the index either."""
    assert ecfp_bitstring(left) != ecfp_bitstring(right), (
        f"{name}: E and Z hold identical ECFP4 bits"
    )


def test_the_exact_match_outranks_its_own_enantiomer() -> None:
    """The property the bits exist for, stated as the ranking a chemist reads.

    Not a restatement of the pair tests: those say the rows differ, this says the *ordering* a
    chemist sees is right. Measured before the fix, all three scored 1.0000 and the (S) enantiomer
    sorted above the query's own structure.
    """
    query = "COc1ccc2cc([C@H](C)C(=O)O)ccc2c1"  # (R)-naproxen
    corpus = {
        "(R)": "COc1ccc2cc([C@H](C)C(=O)O)ccc2c1",
        "(S)": "COc1ccc2cc([C@@H](C)C(=O)O)ccc2c1",
        "flat": "COc1ccc2cc(C(C)C(=O)O)ccc2c1",
    }
    scored = {
        name: tanimoto(ecfp_bitstring(query), ecfp_bitstring(smiles))
        for name, smiles in corpus.items()
    }
    assert scored["(R)"] == 1.0
    assert scored["(S)"] < scored["(R)"], scored
    assert scored["flat"] < scored["(R)"], scored


def test_a_stereo_unspecified_query_still_finds_a_stereo_specified_record() -> None:
    """What the change costs, pinned so it cannot quietly get worse.

    Telling the bits apart means a flat query no longer scores 1.0000 against a specified record,
    and for a stereo-dense molecule that fall is steep: sucrose measures 0.3061 against the default
    0.3 threshold. That is the whole cost of the decision, and it is a cost in *ranking* — the
    neighbour is still returned — where the alternative was a citation error the reader cannot see.
    A drop below the threshold would turn it back into "no precedent", so the floor is asserted
    against the shipped one.
    """
    floor = settings.fingerprint_similarity_threshold
    dense = [
        ("glucose", "OCC1OC(O)C(O)C(O)C1O", "OC[C@H]1O[C@H](O)[C@H](O)[C@@H](O)[C@@H]1O"),
        (
            "sucrose",
            "OCC1OC(CO)(OC2OC(CO)C(O)C(O)C2O)C(O)C1O",
            "OC[C@H]1O[C@@](CO)(O[C@H]2O[C@H](CO)[C@@H](O)[C@H](O)[C@H]2O)[C@@H](O)[C@@H]1O",
        ),
        (
            "cholesterol",
            "CC(C)CCCC(C)C1CCC2C3CC=C4CC(O)CCC4(C)C3CCC12C",
            "CC(C)CCC[C@@H](C)[C@H]1CC[C@H]2[C@@H]3CC=C4C[C@@H](O)CC[C@]4(C)[C@H]3CC[C@]12C",
        ),
    ]
    for name, flat, specified in dense:
        similarity = tanimoto(ecfp_bitstring(flat), ecfp_bitstring(specified))
        assert similarity > floor, (
            f"{name}: a stereo-unspecified query scores {similarity:.4f} against the specified "
            f"record, at or below the {floor} threshold — which reads as no precedent"
        )


def test_an_achiral_molecule_pays_nothing_for_the_change() -> None:
    """The bits move only where there is stereochemistry to record.

    Half the corpus is achiral; a chirality flag that perturbed those rows would be re-indexing
    every molecule for a property none of them has.
    """
    for smiles in ("CCO", "c1ccccc1", "CC(=O)Oc1ccccc1C(=O)O", "CN(C)C=O"):
        with_chirality = ecfp_bitstring(smiles)
        flat = _generator(settings.ecfp_radius, settings.ecfp_bits, False).GetFingerprint(
            standardize(Chem.MolFromSmiles(smiles))
        )
        assert with_chirality == flat.ToBitString(), smiles


def test_the_definition_names_what_decides_the_bits() -> None:
    """A stereo-blind row and a stereo-aware one are the same width and are not comparable.

    The same argument the `agents-excluded` token makes on the reaction side: the retirement
    mechanism is the definition string, so what changed the bits has to be *in* it. Riding on
    `STANDARDIZATION_VERSION` alone would retire the rows once and then let a later pipeline change
    move that token while this one silently stayed true.
    """
    assert "chiral" in molecule_definition()
