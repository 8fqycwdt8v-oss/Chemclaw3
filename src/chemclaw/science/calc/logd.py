"""The local half of logD: a Crippen sum and one Henderson-Hasselbalch term over a remote pKa.

logD has no cache key of its own: its expensive half is a cached remote pKa and the rest is a
sub-millisecond RDKit descriptor, so it is composed here to keep the warm path warm.

Domain: neutral O-H/S-H acids and the conjugate acid of aromatic or aryl nitrogen, inherited from
the pKa predictor (aliphatic amines raise there). `_require_a_single_equilibrium` narrows it further
— polyprotic acids and amphoterics — because one term consumes one pKa. `PkaResult.site` must be
read: the correction runs in opposite directions for an acid and a base.
"""

import math
from typing import NamedTuple

from rdkit import Chem
from rdkit.Chem import Crippen

from chemclaw.core.config import settings
from chemclaw.science.calc.models import LogdResult, PkaResult
from chemclaw.science.calc.uncertainty import CalculationDomainError

# Site patterns as SMARTS over the hydrogen-explicit molecule (`Chem.AddHs`), so `X`/`D` count
# hydrogens. An acidic site is one O-H or S-H proton, counted per hydrogen (a diol has two), which
# is what `_require_a_single_equilibrium` reads. `tests/test_logd.py` pins the partition.
_ACIDIC_SITE = Chem.MolFromSmarts("[#1;D1]-[#8,#16]")

# A basic site is a neutral, not four-connected nitrogen whose lone pair is available. Excluded:
#
# - `$([#7]#*)` — nitrile (any sp nitrogen); pKaH ~ -10.
# - `$([n;!X1;!X2])` — pyrrole-type aromatic nitrogen, whose pair is in the aromatic sextet. The
#   pyridine-type nitrogen has two connections and is basic (imidazole has one of each).
# - `$([#7]-[#6,#16]=[#8,#16])` — amide, carbamate, urea, sulfonamide: the pair is conjugated into
#   C=O/S=O and protonation occurs on oxygen. The single bond keeps aniline (aromatic bond, pKaH
#   4.6) in, and `=` never matches an aromatic bond.
_BASIC_SITE = Chem.MolFromSmarts(
    "[#7;+0;X1,X2,X3;!$([#7]#*);!$([n;!X1;!X2]);!$([#7]-[#6,#16]=[#8,#16])]"
)


class IonisableSites(NamedTuple):
    """How many acid and base sites this molecule offers a single-equilibrium model.

    `predict_pka` reports one pKa, and a `PkaResult` does not reveal whether the molecule has more
    sites. This mirrors the pKa predictor's own site enumeration (pure graph inspection, so it is
    cheap here and needs no round trip), and is exactly as good as that enumeration.
    """

    acidic: int
    basic: int

    @property
    def total(self) -> int:
        """Sites of either kind — the number a single-equilibrium model needs to be 1."""
        return self.acidic + self.basic


def ionisable_sites(smiles: str) -> IonisableSites:
    """Count the acidic O-H/S-H protons and the protonatable nitrogens of a neutral molecule.

    Structural, not energetic: what the pKa predictor would enumerate, not what ionises at a given
    pH.
    """
    parsed = Chem.MolFromSmiles(smiles)
    if parsed is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    mol = Chem.AddHs(parsed)
    # Each match is keyed by one distinct atom, so the atom count is a ceiling that cannot truncate
    # (RDKit's default is 1,000).
    cap = mol.GetNumAtoms()
    return IonisableSites(
        acidic=len(mol.GetSubstructMatches(_ACIDIC_SITE, maxMatches=cap)),
        basic=len(mol.GetSubstructMatches(_BASIC_SITE, maxMatches=cap)),
    )


def _require_a_single_equilibrium(result: PkaResult, ph: float, ionised_ratio: float) -> None:
    """Raise unless one Henderson-Hasselbalch term can describe this whole molecule.

    A second ionisable site is served correctly only when it is a spectator. Refused:

    - **Amphoteric** (an acid and a base site), at every pH: the predictor takes the acid branch and
      never evaluates the base (e.g. glycine).
    - **Polyprotic** (two or more sites of one kind) while the reported, most ionisable site is
      ionised above `settings.logd_negligible_ionised_fraction`; below it every other site is even
      less ionised and the single term holds (diols, sugars).

    A refusal rather than an out-of-domain flag: the result would be known wrong by several log
    units, and the composition's other domain limits are already refusals.
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

    Raises `CalculationDomainError` where a single Henderson-Hasselbalch term cannot describe the
    molecule at this pH (see `_require_a_single_equilibrium`); never a guessed logD. The uncertainty
    combines Crippen's RMSE in quadrature with the pKa residual scaled by the ionised fraction.
    Synchronous and sub-millisecond.
    """
    ph = settings.logd_default_ph if ph is None else ph
    # `pka_result.smiles` is already the canonical form the pKa was computed on, so this reparse
    # cannot fail — the molecule was proven parseable before any SCF ran.
    mol = Chem.MolFromSmiles(pka_result.smiles)
    if mol is None:  # pragma: no cover - guaranteed by the predictor's own validation
        raise ValueError(f"the pKa result carries an unparseable SMILES: {pka_result.smiles!r}")
    clogp = Crippen.MolLogP(mol)
    # Henderson-Hasselbalch; the sign of the exponent depends on the site:
    #   acid  HA  <-> A- + H+ : the ionized fraction *rises* with pH  -> 10**(pH - pKa)
    #   base  BH+ <-> B  + H+ : the ionized fraction *falls* with pH  -> 10**(pKa - pH)
    exponent = ph - pka_result.pka if pka_result.site == "acid" else pka_result.pka - ph
    # [ionized]/[neutral] — the same quantity the correction and the domain check both need,
    # computed once so the number that is refused on is the number that would have been used.
    ionised_ratio = 10.0**exponent
    _require_a_single_equilibrium(pka_result, ph, ionised_ratio)
    # Propagated error: `logD = clogP - log10(1 + 10**(±(pH - pKa)))`, so `dlogD/dclogP` is 1 and
    # `dlogD/dpKa` is the ionised fraction. The two terms are independent and combine in quadrature.
    ionised_fraction = ionised_ratio / (1.0 + ionised_ratio)
    return LogdResult(
        smiles=pka_result.smiles,
        ph=ph,
        clogp=clogp,
        pka=pka_result.pka,
        log_d=clogp - math.log10(1.0 + ionised_ratio),
        uncertainty=math.hypot(
            settings.crippen_logp_uncertainty, ionised_fraction * pka_result.uncertainty
        ),
    )
