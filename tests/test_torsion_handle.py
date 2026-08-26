"""The torsion handle, and the contract between this repository and `Chemclaw3-mcp`.

`servers/chem` mints handles; this repository checks them, and neither may import the other. So the
definition is written twice, and this table is what makes a divergence *detectable*: the same
literals are asserted in `servers/chem/tests/test_torsion_handle_contract.py` there, so whichever
side moves first — an RDKit bump, a change to how the class pair is built — turns a test red
instead of quietly answering differently. It is the arrangement `require_canonical_smiles` already
has with that server, applied to the one other value that crosses between them.

**A drift here is not a broken feature, it is a wrong answer.** A handle that does not match is
refused, which is safe. A handle that matches the *wrong bond* is a profile of something else,
reported as an answer.
"""

import pytest
from rdkit import Chem

from chemclaw.core.chem import torsion_handle

# `(SMILES, bond, handle)` — minted by this repository's `torsion_handle` under rdkit 2026.03.5 and
# asserted verbatim on both sides. The first three are one compound written three ways; the last
# two are one compound's two symmetry-equivalent methyls.
CONTRACT = [
    ("CC(=O)Nc1ccccc1", (1, 3), "tor_d139107cd84f9333"),
    ("O=C(C)Nc1ccccc1", (1, 3), "tor_d139107cd84f9333"),
    ("c1ccc(NC(C)=O)cc1", (4, 5), "tor_d139107cd84f9333"),
    ("CCCC", (1, 2), "tor_6b25409b2bd410a6"),
    ("c1ccc(-c2ccccc2)cc1", (3, 4), "tor_17935ce6ec9a1219"),
    ("Cc1ccc(C)cc1", (0, 1), "tor_7b6b88fe5991e188"),
    ("Cc1ccc(C)cc1", (4, 5), "tor_7b6b88fe5991e188"),
]


@pytest.mark.parametrize(("smiles", "bond", "handle"), CONTRACT)
def test_the_handle_is_the_one_both_repositories_agree_on(
    smiles: str, bond: tuple[int, int], handle: str
) -> None:
    """Literals, not a recomputation: a test that derives the expected value proves nothing."""
    assert torsion_handle(Chem.MolFromSmiles(smiles), bond) == handle


def test_one_bond_written_three_ways_is_one_handle() -> None:
    """The property the design rests on, stated once as itself rather than as three rows."""
    amide = {handle for smiles, _, handle in CONTRACT if "N" in smiles and "c1" in smiles}
    assert len(amide) == 1


def test_the_bond_order_does_not_matter() -> None:
    """The pair is sorted before hashing, so a caller need not know which end is which."""
    mol = Chem.MolFromSmiles("CC(=O)Nc1ccccc1")
    assert torsion_handle(mol, (3, 1)) == torsion_handle(mol, (1, 3))


def test_a_different_molecule_does_not_share_a_handle() -> None:
    """The check that makes a mismatch meaningful: n-butane's bond is not n-pentane's."""
    butane = torsion_handle(Chem.MolFromSmiles("CCCC"), (1, 2))
    pentane = torsion_handle(Chem.MolFromSmiles("CCCCC"), (1, 2))
    assert butane != pentane
