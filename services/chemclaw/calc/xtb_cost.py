"""Predicted cost of an xTB request — the routing signal for inline vs. durable (X3/X4).

The single question this module answers: *will this block a conversation?* Below the
inline budget an xTB task runs in the agent's turn, where the calculation store already
makes a repeat free. Above it, the task belongs in Temporal — a job id now and a
push-back when it finishes — because the alternative is a chat that stops responding
for minutes.

The estimate is a **router, not a promise**. It is a power law fitted to measurements on
this stack (GFN2 via tblite, one core), and it is used only to compare against a
threshold, so being wrong by a factor of two moves a borderline job to the other side of
the line and nothing else.

**It is fitted on drug-sized molecules, because that is the workload.** The first
version was fitted on 3-14 atom test molecules and gave an exponent of 1.7 — which
under-predicted a 76-atom substrate by nearly **seven times**, because at that size the
fixed overhead that dominates a small molecule is irrelevant and the real scaling (SCF
diagonalization, and a step count that itself grows with size) takes over. Measured:

| molecule                    | atoms | optimize | steps | + Hessian | total   |
|-----------------------------|-------|----------|-------|-----------|---------|
| water                       |     3 |   ~0.2 s |     4 |    ~0.1 s |  0.26 s |
| ethanol                     |     9 |   0.33 s |    14 |    0.13 s |  0.46 s |
| naproxen (MW 230)           |    31 |    6.3 s |    44 |     8.3 s | 14.6 s  |
| ibuprofen (MW 206)          |    33 |   11.6 s |    71 |     7.5 s | 19.0 s  |
| sildenafil (MW 475)         |    63 |   66.0 s |   154 |   435.1 s |  501 s  |
| atorvastatin core (MW 559)  |    76 |   96.6 s |   177 |   218.3 s |  315 s  |
| erythromycin (MW 734)       |   118 |  552.6 s |   232 |  1007.1 s | 1560 s  |

The fitted exponent is ~3. Practically: **everything in the 200-800 Da range runs as a
durable job**, which is the correct answer rather than a limitation.

**Atom count is not the whole story, and the table says so.** Sildenafil at 63 atoms costs
more than the atorvastatin core at 76 — its Hessian alone is twice as expensive — because
a heteroatom-dense, conjugated system carries more basis functions per atom and converges
its SCF harder. That scatter is irreducible from composition, so this model carries a
factor of ~2 either way in the drug range and the estimate reported to a user should be
read as an order of magnitude ("about five minutes"), never as a countdown.

The coefficients are set to err high rather than low: an over-estimate sends a borderline
request to a worker, an under-estimate stalls a chat. All three are config, so a faster
machine (or a future backend) is re-tuned rather than re-coded.
"""

from calc.xtb_engine import parse_molecule
from chemclaw.config import settings


def species_seconds(atoms: int, hessian: bool) -> float:
    """Predicted seconds to optimize one species, and take its Hessian if asked.

    A Hessian is 6N gradient evaluations against an optimization's few dozen, so it
    dominates as soon as it is switched on — which is exactly why `level="quick"` is
    worth having.
    """
    scale = settings.xtb_cost_hessian_scale if hessian else settings.xtb_cost_optimize_scale
    return float(scale * float(atoms) ** settings.xtb_cost_exponent)


def atom_count(smiles: str) -> int:
    """Number of atoms including hydrogens — what every xTB cost scales in."""
    return int(parse_molecule(smiles).GetNumAtoms())


def reaction_seconds(
    species: list[str], hessian: bool, repeats: int = 1, ensemble: bool = False
) -> float:
    """Predicted seconds for a reaction over `species`, run `repeats` times.

    `repeats` is the solvent comparison's multiplier: the same species set is run once
    per solvent plus the gas phase, and that is where a comfortable inline request
    turns into a durable one. `ensemble` is the `thorough` level's conformer search,
    which is metadynamics plus hundreds of optimizations and therefore dominates
    everything else — measured at ~50 s for n-butane, so it is never inline.
    """
    total = repeats * sum(species_seconds(atom_count(smiles), hessian) for smiles in species)
    if ensemble:
        total += sum(
            settings.xtb_cost_ensemble_scale
            * float(atom_count(smiles)) ** settings.xtb_cost_exponent
            for smiles in species
        )
    return total


def ensemble_seconds(smiles: str) -> float:
    """Predicted seconds for a CREST ensemble search — always past the inline budget."""
    return float(
        settings.xtb_cost_ensemble_scale * float(atom_count(smiles)) ** settings.xtb_cost_exponent
    )


def scan_seconds(smiles: str, points: int) -> float:
    """Predicted seconds for a relaxed scan of `points` constrained optimizations."""
    return points * species_seconds(atom_count(smiles), hessian=False)


def exceeds_inline_budget(seconds: float) -> bool:
    """Whether a request of this predicted cost should run as a durable job instead."""
    return seconds > settings.xtb_inline_budget_seconds
