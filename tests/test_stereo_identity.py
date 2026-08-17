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
