"""The local half of logD: a Crippen sum and one Henderson-Hasselbalch term over a remote pKa.

`D-2026-08-16-the-physics-leaves-the-cache-stays` decomposed `predict_logd` rather than shipping
it, and this module is the half that stayed. The reasoning is the ADR's and it is arithmetic: logD
has **no cache key of its own** — it never had one, because its expensive half is a *cached* pKa and
the rest is a sub-millisecond RDKit descriptor. Shipping the composite whole would have turned every
repeat into a full recompute of the most expensive tool in the set (measured: pyridine 20.603 s cold
against 0.005 s warm). Shipping the pKa as a primitive and composing here keeps the warm path warm.

What is left is genuinely local: Crippen LogP is pure RDKit, RDKit stays in this repository, and the
Henderson-Hasselbalch correction is one exponent. The **domain check is local too**, and that is not
an accident of where the code sat — it is a statement about *this* arithmetic, not about the pKa
predictor: `predict_pka` reports one pKa and one term consumes exactly one, so a molecule ionising
at two sites is outside what this composition can express even though both halves it composes are
inside theirs.

**Domain, inherited from the pKa predictor and then narrowed once.** Neutral O-H/S-H **acids**
(carboxylic acids, phenols, alcohols, thiols) and the conjugate acid of **aromatic or aryl
nitrogen** (pyridines, azoles, anilines). Aliphatic amines and molecules with neither site raise on
the server and the error propagates unchanged (gate G4). `_require_a_single_equilibrium` adds the
narrowing: polyprotic acids, and amphoterics, which had been slipping past the aliphatic-amine
refusal because a carboxyl sends the molecule down the acid branch before the amine is ever looked
at.

That domain widened underneath this composition once, and the widening had teeth: the predictor
began returning bases where it previously raised, and the correction runs in the *opposite
direction* for one. `PkaResult.site` is what makes the two distinguishable, and `logd_from_pka`
branches on it. Depending on a collaborator's domain is fine; depending on it without reading which
half of the domain you were handed is what produced a two-log-unit error that raised nothing.
"""

import math
from typing import NamedTuple

from rdkit import Chem
from rdkit.Chem import Crippen

from chemclaw.core.config import settings
from chemclaw.science.calc.models import LogdResult, PkaResult
from chemclaw.science.calc.uncertainty import CalculationDomainError

# Heavy atoms whose O-H/S-H protons count as acidic sites.
_ACIDIC_HEAVY = (8, 16)  # O, S
# Nitrogen valence at which there is no lone pair left to protonate.
_SATURATED_NITROGEN = 4
# Sigma bonds at which an *aromatic* nitrogen's lone pair has gone into the ring's pi system instead
# of staying in an in-plane orbital: pyrrole-type rather than pyridine-type.
_PYRROLE_TYPE_SIGMA_BONDS = 3
# Atoms that drain an adjacent nitrogen's lone pair when they carry a double bond to a chalcogen:
# carbon (amide, carbamate, urea) and sulfur (sulfonamide, sulfinamide).
_ELECTRON_WITHDRAWING = (6, 16)  # C, S
# The chalcogen on the far end of that double bond.
_CHALCOGEN = (8, 16)  # O, S


class IonisableSites(NamedTuple):
    """How many acid and base sites this molecule offers a single-equilibrium model.

    `predict_pka` reports **one** pKa — the most acidic proton, or the most stable protomer —
    because that is the number a chemist means by "the pKa". The arithmetic below assumes a single
    acid/base equilibrium and needs to know when that assumption is false, and it cannot read that
    off a `PkaResult`: a diprotic acid and a monoprotic one return the same shape.

    **The enumeration is duplicated across the repository boundary, deliberately and with a
    limit.** It mirrors the site enumeration the pKa predictor itself runs before any xTB, so this
    counts what that predictor *would* evaluate. That was a shared function while both lived in one
    process; it cannot be one now, and the honest reading is that this is exactly as good as the
    predictor's enumeration and no better. It is also the cheap half — pure graph inspection, no
    SCF — so re-deriving it here costs nothing and asking the server for it would cost a round trip
    on a refusal path.
    """

    acidic: int
    basic: int

    @property
    def total(self) -> int:
        """Sites of either kind — the number a single-equilibrium model needs to be 1."""
        return self.acidic + self.basic


def _lone_pair_is_available(atom: Chem.Atom) -> bool:
    """Whether this nitrogen's lone pair can actually accept a proton in water.

    Free valence says a lone pair *exists*; it does not say the pair is available, and three common
    classes have one that is not. Each exclusion is a delocalized or unavailable lone pair, never a
    convenience.

    - **Amide, carbamate, urea, sulfonamide** — a nitrogen single-bonded to a carbon or sulfur that
      carries a double bond to O or S. The lone pair is conjugated into that C=O/S=O, and the
      consequence is not a shifted pKa but a different molecule: protonated acetamide has pKaH
      ~ -0.5 **and protonates on the oxygen**.
    - **Nitrile** — an sp nitrogen (a triple bond). pKaH ~ -10; there is no aqueous pH at which any
      of it is protonated.
    - **Pyrrole-type aromatic nitrogen** — an aromatic nitrogen with three sigma bonds, so its lone
      pair is the ring's aromatic sextet rather than an in-plane orbital. The **pyridine-type**
      nitrogen beside it in the same ring has two sigma bonds and an in-plane lone pair, and *is*
      basic — imidazole's two nitrogens are one of each, which is why counting both put imidazole
      (pKaH 6.95) outside this module's single-equilibrium domain when it has exactly one basic
      centre.

    Only a **single** bond from the nitrogen counts for the amide rule, which is what keeps aniline
    out of it: aniline's bond to the ring is aromatic, not the C=O single bond this looks for, and
    aniline is genuinely a weak base (pKaH 4.6) the calibration covers.
    """
    if any(bond.GetBondType() == Chem.BondType.TRIPLE for bond in atom.GetBonds()):
        return False
    if (
        atom.GetIsAromatic()
        and atom.GetDegree() + atom.GetTotalNumHs() >= _PYRROLE_TYPE_SIGMA_BONDS
    ):
        return False
    for bond in atom.GetBonds():
        if bond.GetBondType() != Chem.BondType.SINGLE:
            continue
        neighbor = bond.GetOtherAtom(atom)
        if neighbor.GetAtomicNum() not in _ELECTRON_WITHDRAWING:
            continue
        if any(
            other.GetBondType() == Chem.BondType.DOUBLE
            and other.GetOtherAtom(neighbor).GetAtomicNum() in _CHALCOGEN
            for other in neighbor.GetBonds()
        ):
            return False
    return True


def ionisable_sites(smiles: str) -> IonisableSites:
    """Count the acidic O-H/S-H protons and the protonatable nitrogens of a neutral molecule.

    Structural, not energetic: it reports what the pKa predictor would *enumerate*, before any xTB
    runs, so it is free to call. It does not rank the sites it keeps, so "two sites" here means two
    the predictor would evaluate, not two that ionise in any particular pH window. Deciding that is
    the caller's, and needs the pKa this function deliberately does not compute.
    """
    parsed = Chem.MolFromSmiles(smiles)
    if parsed is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    mol = Chem.AddHs(parsed)
    acidic = sum(
        1
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() == 1
        and atom.GetDegree() == 1
        and atom.GetNeighbors()[0].GetAtomicNum() in _ACIDIC_HEAVY
    )
    basic = sum(
        1
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() == 7
        and atom.GetFormalCharge() == 0
        and atom.GetTotalNumHs() + atom.GetDegree() < _SATURATED_NITROGEN
        and _lone_pair_is_available(atom)
    )
    return IonisableSites(acidic=acidic, basic=basic)


def _require_a_single_equilibrium(result: PkaResult, ph: float, ionised_ratio: float) -> None:
    """Raise unless one Henderson-Hasselbalch term can describe this whole molecule.

    `predict_pka` reports one pKa and this module applies one ionisation term, so a molecule with a
    second ionisable site is only served correctly when that site is spectator. Two situations where
    it is not, and neither is recoverable from the surface the pKa tool offers — a second pKa is
    simply not computed — so both refuse rather than return a number:

    - **Amphoteric** (an acid site *and* a base site). Refused at every pH. The predictor takes the
      acid branch whenever any O-H/S-H is present, so the base site is never even evaluated and
      nothing bounds its ionisation. Glycine at pH 7.4 is the measured case: it returned -2.81 with
      no error, silently evading the very refusal raised for piperidine, because a carboxyl kept it
      out of the aliphatic-amine branch it belonged in.
    - **Polyprotic** (two or more sites of the same kind) *while the reported site is substantially
      ionised*. The reported site is the most ionisable one, so when it is essentially neutral every
      other site is even more so and the single term is exact to within
      `settings.logd_negligible_ionised_fraction`'s bound — which is what keeps a diol or a sugar
      (O-H sites with pKa ~15) working. Above that threshold the unseen equilibrium is unbounded:
      succinic acid at pH 7.4 returned -1.48 +/- 1.6 against a true value near -5, one carboxyl
      accounted for and one ignored.

    **Refusal rather than an out-of-domain `Estimate`**, though both conventions exist in the tree
    (`science/calc/models.py`'s `SolubilityResult` flags). Two reasons. This composition has only
    ever had the first: its domain limits are `ValueError`s inherited from the pKa predictor, and
    the aliphatic-amine case this closes is *already* a refusal, so flagging would make one hazard
    visible in a field and its twin visible in an exception. And the two are not the same kind of
    claim — ESOL on a salt returns a number of unknown validity, whereas this returns one known to
    be wrong by 2-5 log units, which is a number no caller should be handed at all.
    """
    sites = ionisable_sites(result.smiles)
    if sites.acidic and sites.basic:
        raise CalculationDomainError(
            f"{result.smiles!r} is amphoteric ({sites.acidic} acidic O-H/S-H site(s) and "
            f"{sites.basic} basic nitrogen(s)): its acid and base equilibria run in opposite "
            "directions and this calculator applies one ionisation term to the single pKa "
            "predicted — which for an amphoteric molecule is always the acid site, so the base "
            "site is neither computed nor bounded. No logD rather than a plausible one"
        )
    if sites.total < 2:
        return
    ionised_fraction = ionised_ratio / (1.0 + ionised_ratio)
    if ionised_fraction > settings.logd_negligible_ionised_fraction:
        kind = "acidic O-H/S-H site(s)" if result.site == "acid" else "basic nitrogen(s)"
        raise CalculationDomainError(
            f"{result.smiles!r} has {sites.total} {kind} and is {ionised_fraction:.0%} ionised at "
            f"pH {ph:g} on the one site the pKa predictor reports (pKa {result.pka:.2f}). A second "
            "ionisation of comparable size is unaccounted for and its pKa is not computable from "
            "this predictor, so the single-equilibrium logD would be wrong by an unbounded "
            "amount (measured: succinic acid at pH 7.4 gives -1.5 against a true value near -5)"
        )


def logd_from_pka(pka_result: PkaResult, ph: float | None = None) -> LogdResult:
    """Combine a computed pKa with a local Crippen LogP into logD at `ph`.

    Raises `CalculationDomainError` where a *single* Henderson-Hasselbalch term cannot describe the
    molecule at this pH (see `_require_a_single_equilibrium`). Never a guessed logD (gate G4).

    Synchronous and sub-millisecond: the SCF is already paid for by the time this is called, and
    both RDKit calls here are descriptor work on a molecule that has already been proven parseable.
    """
    ph = settings.logd_default_ph if ph is None else ph
    # `pka_result.smiles` is already the canonical form the pKa was computed on, so this reparse
    # cannot fail — the molecule was proven parseable before any SCF ran.
    mol = Chem.MolFromSmiles(pka_result.smiles)
    if mol is None:  # pragma: no cover - guaranteed by the predictor's own validation
        raise ValueError(f"the pKa result carries an unparseable SMILES: {pka_result.smiles!r}")
    clogp = Crippen.MolLogP(mol)
    # Henderson-Hasselbalch, and the sign of this exponent is the entire content of it.
    #   acid  HA  <-> A- + H+ : the ionized fraction *rises* with pH  -> 10**(pH - pKa)
    #   base  BH+ <-> B  + H+ : the ionized fraction *falls* with pH  -> 10**(pKa - pH)
    # Written as a branch on `site` rather than one formula because getting it wrong is silent:
    # before this, a base took the acid form and pyridine at pH 7.4 came out two log units too
    # lipophobic while looking entirely ordinary.
    exponent = ph - pka_result.pka if pka_result.site == "acid" else pka_result.pka - ph
    # [ionized]/[neutral] — the same quantity the correction and the domain check both need,
    # computed once so the number that is refused on is the number that would have been used.
    ionised_ratio = 10.0**exponent
    _require_a_single_equilibrium(pka_result, ph, ionised_ratio)
    return LogdResult(
        smiles=pka_result.smiles,
        ph=ph,
        clogp=clogp,
        pka=pka_result.pka,
        log_d=clogp - math.log10(1.0 + ionised_ratio),
        uncertainty=pka_result.uncertainty,
    )
