"""Shared cheminformatics helpers: the one definition of "the same molecule".

`canonical_smiles` is the single structure-normalizing key used wherever two spellings of one
molecule must collapse to one string — compound identity in the fingerprint index (ingestion),
product↔reactant matching (chain detection), and every calculation cache / workflow-dedup key
(D-011: compute once, never twice). It lived in `chemclaw.core.chem` when only the ELN used
it; it moved here once the compute cache and the QM workflow needed the same guarantee, so the
canonicalization that decides "same molecule" exists in exactly one place (DRY).

**RDKit's canonical SMILES is not that definition, and used to be treated as one.** It normalizes
*spelling* — atom ordering, aromaticity perception, ring closures — and nothing else. So a free
base and its hydrochloride, a carboxylate and its sodium salt, and two tautomers of one compound
each produced a different string, and therefore a different `compound_id`, a different calculation
cache key and a different fingerprint row. That is the classic cheminformatics production failure:
identity fragments, the cache misses work D-011 promises never to repeat, and similarity search
answers about a molecule the chemist thinks it has already seen.

`standardize` is the missing step. The pipeline is deliberately the conventional one, in the
conventional order, because a bespoke normalization is a bespoke notion of sameness:

1. `Cleanup` — sanitize, disconnect metals, normalize functional-group spellings (nitro, N-oxide).
2. `FragmentParent` — keep the largest organic fragment, which strips counterions and solvates.
3. `Uncharger` — neutralize what can be neutralized, so a carboxylate meets its acid.
4. `TautomerEnumerator.Canonicalize` — one representative per tautomer set.

Steps 2 and 3 say **"the counterion is not part of the identity"**, and that claim holds for an
amine hydrochloride and for sodium benzoate but not for every species a chemist writes. It fails
in three directions, each of which deletes the reagent rather than normalizing it, and
`_identity_survives_stripping` is the one gate that holds them off:

- **Nothing organic is left to be the compound.** A wholly inorganic reagent has no organic parent
  to keep, so the strip discards half the formula: NaOH and KOH both became water, CsF became a
  bare caesium ion, and K2CO3, Cs2CO3, Na2CO3 and NaHCO3 became one carbonic acid.
- **The discarded fragment is the reactive centre, not a spectator.** `Cleanup` disconnects metals,
  so Pd(OAc)2 arrived at step 2 as `[Pd+2]` beside two acetates and left it as acetic acid, and
  Pd(dppf)Cl2 left it as the bare ligand — a Pd-source screen therefore reported that nothing had
  changed, exactly as the base screen did.
- **The compound is organometallic, and the discarded fragment is its solvent.** An alkyllithium or
  a Grignard is supplied and logged as a solution — "n-BuLi in hexanes", "iPrMgCl in THF" — and the
  solvent is the larger fragment, so the parent chosen was the *solvent*: n-BuLi standardized to
  hexane, MeMgBr to diethyl ether, PhLi to dibutyl ether, and AlMe3 — whose Al–C bond `Cleanup`
  does break — plain methane. A pyrophoric reagent and an alkane sharing one compound id is the
  worst instance of this defect, since a hazard screen reads that id.

Two properties separate the three from the salts that must keep collapsing, and neither is "does it
contain a metal": a **d- or f-block metal** is what the flask is for, while a group-1/2 counterion
only balances a charge (`_REACTIVE_METALS`); and a **metal–carbon bond** is the reagent itself,
while the same metals in an ionic salt have none (`_is_organometallic`). Sodium benzoate and LDA
fail both tests and still collapse. See `_is_organic` for why "organic" is a C–H/C–C test and not
"contains a carbon".

**There are two questions here, and conflating them is how this goes wrong in the other
direction.** Applying the pipeline everywhere neutralizes species a chemist meant as ions, and a
calculation submitted for acetate must not silently compute acetic acid — the test suite says so
directly, because `Structure` validates a declared charge against its SMILES. So:

- `canonical_smiles` / `require_canonical_smiles` answer **"is this the same structure?"** —
  spelling only. They key the calculation cache, the QM workflow-dedup id and the prediction
  ledger, where an anion is a different calculation from its conjugate acid and must stay one.
- `standard_smiles` / `require_standard_smiles` answer **"is this the same compound?"** — the full
  pipeline. They key `compound_id`, the fingerprint index, product↔reactant matching in
  `memory.chains` and species grouping in `memory.progression`, where a hydrochloride and its free
  base are one substance and separate rows for them are the fragmentation this fixes.

Two names rather than a flag, so a caller cannot pick the wrong one by leaving an argument out, and
each name says which question it answers.
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize

from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash

# Bumped whenever the pipeline below changes what it collapses. It is folded into the fingerprint
# `definition` strings, so rows indexed under an older notion of sameness fall out of similarity
# search rather than being silently compared against rows built under a newer one — the guard
# `science/fingerprints/store.py` already applies to a changed radius or bit width, extended to the
# other thing that decides what a row *is*.
STANDARDIZATION_VERSION = "std4"

# The d- and f-block by atomic number — Sc→Zn, Y→Cd, La→Hg (lanthanides included) and Ac onward.
# A block rather than a hand-picked element list, because the property being asserted is a block
# property: these are the metals a synthesis puts in the flask to *do* the chemistry (Pd, Cu, Ni,
# Ru, Fe, Zn, Sm), so a species containing one is a complex whose identity is the whole complex.
# Their absence is what makes a counterion a spectator — the group-1/2 metals that balance a charge
# in sodium benzoate or LDA are outside it, and keep collapsing. RDKit exposes no block predicate,
# so the ranges are spelled out here rather than derived.
_REACTIVE_METALS = frozenset((*range(21, 31), *range(39, 49), *range(57, 81), *range(89, 113)))

# Every metal, for the metal–carbon test — the block above widened by the two groups it excludes.
# The metalloids (B, Si, Ge, As, Sb, Te) are deliberately left out: a boronic acid and a silyl azide
# are organic reagents, and calling boron metallic would exempt every Suzuki boron source from a
# strip that is correct for it.
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

# One `TautomerEnumerator` for the process. Constructing it parses its transform catalogue, which
# is not free, and this runs per component per ingested reaction.
_TAUTOMERS = rdMolStandardize.TautomerEnumerator()


@lru_cache(maxsize=4096)
def _standardized(smiles: str) -> str | None:
    """The standardized canonical SMILES of `smiles`, or None when it does not parse.

    Cached because the pipeline is materially more expensive than a parse — tautomer
    canonicalization enumerates a transform set — and because the callers are loops: every
    component of every ingested reaction, and every product/reactant pair in chain detection. Pure
    in its argument, so the cache is sound; bounded, so a long-lived worker cannot grow into it.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    return str(Chem.MolToSmiles(standardize(mol)))


def _is_organic(fragment: Chem.Mol) -> bool:
    """Whether a fragment is organic: does it hold a carbon bonded to hydrogen or to carbon?

    The C–H/C–C test rather than the obvious "does it contain a carbon", because the obvious one
    calls carbonate and bicarbonate organic — and then `FragmentParent` keeps `[O-]C([O-])=O` as
    the "parent" of K2CO3 and throws the potassium away, which is precisely how K2CO3, Cs2CO3,
    Na2CO3 and NaHCO3 collapsed into one compound. Cyanide fails the test for the same reason and
    equally correctly: NaCN and KCN are two reagents, not one.

    It is the classical organic/inorganic line (carbonates, cyanides, carbides and CO/CO2 are the
    conventional carbon-containing exceptions), and it is deliberately a *structural* test rather
    than an element list, so no table has to be kept in step with the reagents chemists write.
    """
    return any(
        atom.GetAtomicNum() == 6
        and (
            atom.GetTotalNumHs(includeNeighbors=True) > 0
            or any(neighbor.GetAtomicNum() == 6 for neighbor in atom.GetNeighbors())
        )
        for atom in fragment.GetAtoms()
    )


def _is_organometallic(mol: Chem.Mol) -> bool:
    """Whether the species has a metal–carbon bond, the bond that *is* the reagent.

    n-Butyllithium, a Grignard, a cuprate and an organozinc are defined by their M–C bond, so the
    hydrocarbon left after it is broken is a different substance in every way that matters: n-BuLi
    is pyrophoric and butane is a fuel gas. An ionic salt of the same metals has no M–C bond, which
    is what lets sodium benzoate and LDA keep collapsing — the cut is the bond, not the element.
    """
    for bond in mol.GetBonds():
        ends = {bond.GetBeginAtom().GetAtomicNum(), bond.GetEndAtom().GetAtomicNum()}
        if 6 in ends and ends & _METALS:
            return True
    return False


def _identity_survives_stripping(original: Chem.Mol, cleaned: Chem.Mol) -> bool:
    """Whether keeping only the parent fragment still names the same compound.

    All three ways it can fail delete the reagent rather than normalize it (see the module
    docstring). The two molecules are not interchangeable, and each check deliberately asks the
    stage that still holds its evidence:

    - the **cleaned** one for a reactive metal, because `Cleanup` is what disconnects the metal into
      the fragment that would then be thrown away, and the check exists to see that fragment;
    - the **original** one for a metal–carbon bond, because the same `MetalDisconnector` breaks M–C
      for some metals and not others — Al–C yes, Li–C and Mg–C no — so by the time the molecule is
      cleaned the evidence has been destroyed for exactly the ones no other check catches. AlMe3 is
      the case that decides it: aluminium is outside `_REACTIVE_METALS`, so reading the cleaned
      molecule standardizes trimethylaluminium to methane.
    """
    if _is_organometallic(original):
        return False  # the M–C bond is the reagent; the hydrocarbon left without it is not
    if any(atom.GetAtomicNum() in _REACTIVE_METALS for atom in cleaned.GetAtoms()):
        return False  # a metal complex: the metal is the chemistry, not a counterion
    return any(_is_organic(fragment) for fragment in Chem.GetMolFrags(cleaned, asMols=True))


def standardize(mol: Chem.Mol) -> Chem.Mol:
    """Apply the standardization pipeline to a parsed molecule (see the module docstring).

    Separate from the SMILES helpers so a caller that already holds a molecule — and a test that
    wants to check one stage — does not have to round-trip through a string.
    """
    cleaned = rdMolStandardize.Cleanup(mol)
    if _identity_survives_stripping(mol, cleaned):
        cleaned = rdMolStandardize.FragmentParent(cleaned)
        cleaned = rdMolStandardize.Uncharger().uncharge(cleaned)
    return _TAUTOMERS.Canonicalize(cleaned)


class InvalidSmilesError(ChemclawError):
    """A SMILES string that RDKit cannot parse.

    A `ChemclawError`, so a batch boundary catches it as bad data and the Temporal
    retry policy treats it as a fast, non-retryable failure (never a retry loop).
    """


def canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, or the input unchanged if it does not parse.

    A stable, structure-normalized key: two spellings of the same molecule collapse
    to one string, so it is the natural compound id and the product↔reactant match
    key. Lenient by design — the ELN/memory callers key on whatever string they are
    given and never want ingestion to abort on one odd label. Where an unparseable
    structure must instead be rejected, use `require_canonical_smiles`.
    """
    mol = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else smiles


def require_molecule(smiles: str) -> Chem.Mol:
    """The parsed molecule, raising `InvalidSmilesError` unless RDKit reads `smiles` **whole**.

    This is the one definition of "RDKit accepts this string, all of it", and the two strict
    helpers below are both written on top of it. It is separate from them because a caller that
    needs the *molecule* rather than a key — a SMARTS matcher, say — otherwise writes its own,
    weaker acceptance test, and that is exactly what happened: the hazard screens (since moved to
    `Chemclaw3-mcp:servers/safety/src/chemclaw_mcp_safety/engine/screen.py`) parsed with a bare
    `Chem.MolFromSmiles`, so `screen_hazards("CCO junk")` returned a clean screen **of ethanol**
    and echoed `CCO` as the structure it had looked at.

    Three inputs RDKit accepts and this rejects, each measured against this build:

    - **A string with embedded whitespace.** The parser treats any whitespace as the end of the
      structure and ignores the rest, so `"CCO junk"`, `"CCO 1"` and the tab-separated form are all
      ethanol. That is the whole silent-truncation class: a malformed or concatenated string does
      not fail, it narrows to a *different, smaller molecule* than the caller submitted.
    - **The empty string**, which parses to a molecule with no atoms — a key for nothing, or a
      screen that matches nothing.
    - **A string carrying a non-ASCII character at either end.** SMILES is written in printable
      ASCII, and RDKit skips a run of non-ASCII bytes at the *edges* of the string while failing on
      one between two atoms: `"°C"` is methane, `"CC°"` and `"°CC°"` are ethane, `"C°C"` is a parse
      error. That is the whitespace truncation wearing a different character, and prose is what
      produces it: a note body's code span reading `` `80 °C` `` offers `°C` as a candidate
      structure, and a bare parse calls it methane. Tested on the string rather than on the
      parsed molecule because that is where the evidence is: once RDKit has skipped the
      character, nothing about the molecule says it was ever there.

    Surrounding whitespace is stripped rather than refused: a leading newline is a copy-paste
    artifact, not a second molecule. The message quotes the caller's own string, not the stripped
    one, so what is echoed back is what was typed.
    """
    stripped = smiles.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        raise InvalidSmilesError(f"invalid SMILES (empty or contains whitespace): {smiles!r}")
    if not stripped.isascii():
        raise InvalidSmilesError(f"invalid SMILES (non-ASCII characters): {smiles!r}")
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    return mol


def require_canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, raising `InvalidSmilesError` if it does not parse.

    Use where an unparseable molecule must not silently pass and where the key must
    not distinguish two spellings of one molecule: the calculation cache keys and
    the QM durable boundary (G4). Canonicalizing before the key means `"CCO"` and
    `"OCC"` share one cache entry / one workflow id, honoring D-011.

    Stricter than RDKit's parser — see `require_molecule`, which is where that strictness now
    lives so that a caller wanting the molecule instead of the key gets the identical gate.
    """
    return str(Chem.MolToSmiles(require_molecule(smiles)))


def standard_smiles(smiles: str) -> str:
    """The **standardized** canonical SMILES, or the input unchanged if it does not parse.

    "Is this the same compound?" — salts stripped, charges neutralized where they can be, one
    tautomer per set. Use it wherever two spellings of one *substance* must reach one record;
    use `canonical_smiles` where an anion is genuinely a different thing to compute.

    Lenient about parse failure for the same reason `canonical_smiles` is: the ELN and memory
    callers key on whatever string they are given, and one odd label must not abort ingestion.
    """
    standardized = _standardized(smiles)
    return standardized if standardized is not None else smiles


def require_standard_smiles(smiles: str) -> str:
    """The standardized canonical SMILES, raising `InvalidSmilesError` if it does not parse.

    The strict counterpart of `standard_smiles`, applying `require_molecule`'s gate — so the two
    strict helpers cannot drift on what "parses" means, which they could while each spelled the
    same four lines out.

    The molecule `require_molecule` hands back is deliberately discarded: the pipeline runs through
    `_standardized`, whose cache is keyed on the string and is what makes the loop callers (every
    component of every ingested reaction, every product/reactant pair in chain detection)
    affordable. Standardizing the molecule directly here would parse once instead of twice and
    lose that, which is the more expensive trade by a wide margin.
    """
    require_molecule(smiles)
    standardized = _standardized(smiles.strip())
    if standardized is None:  # pragma: no cover - unreachable once the parse above succeeded
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    return standardized


def substructure_pattern(query: str) -> Chem.Mol:
    """Compile a substructure query — SMARTS first, then SMILES — or raise `InvalidSmilesError`.

    SMARTS first because every SMILES is also valid SMARTS but not the other way round, and a
    chemist asking for "a carbonyl next to anything aromatic" can only say it in SMARTS. Falling
    back to SMILES is what lets a plain fragment (`"c1ccccc1"`) work without the caller knowing
    which language they typed.

    A zero-atom pattern is rejected rather than run: RDKit matches it against every molecule, so
    the answer to a query that said nothing would be "everything", which reads as a finding.

    Here rather than beside one caller because two subsystems now filter by structure — the
    fingerprint index's substructure search and the calibration ledger's outlier listing — and a
    second copy of "SMARTS or SMILES, and reject the empty one" is exactly the kind of chemistry
    rule that drifts apart unnoticed.
    """
    pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
    if pattern is None:
        raise InvalidSmilesError(f"unparseable substructure query: {query!r}")
    if pattern.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"empty substructure query (no atoms): {query!r}")
    return pattern


def compound_id(smiles: str) -> str:
    """The stable knowledge-graph note id for a molecule, derived from its structure.

    Structure-derived rather than name-derived, so two sources that spell the same molecule
    differently still reach one note — the property that makes a citation from a fingerprint
    hit meaningful at all.

    Lives here, beside the canonicalization it is built on, because the callers span layers
    that share nothing else: the ingest/kg side that *writes* the note
    (`chemclaw.ingest.eln.compound`) and the fingerprint connectors that *cite* it
    (`chemclaw.science.fingerprints.molfp.search`, `chemclaw.connectors.qm.knowledge`). A connector
    must not import the knowledge graph (D-115), and the id is a pure function of the structure — no
    graph needed to derive it, only to confirm the note has been merged.
    """
    return f"compound-{stable_hash(require_standard_smiles(smiles), chars=12)}"
