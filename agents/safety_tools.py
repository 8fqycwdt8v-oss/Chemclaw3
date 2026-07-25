"""Agent tool for structural hazard screening (D-080).

Exposes the deterministic screen (`safety.screen`) to the model so a proposed route, condition set,
or procedure can be checked before it is written down. The tool returns *flags*, never a verdict on
whether an experiment may run — the judgment of what to do with a flag lives in the
`safety-screening` skill, and the decision lives with a human.
"""

from agents.tool_registry import tool
from safety.screen import ScreenResult, screen_reaction, screen_structure


@tool
async def screen_hazards(smiles: list[str]) -> ScreenResult:
    """Screen molecules or a reaction for known structural hazard motifs before proposing them.

    Matches each structure against a curated, literature-cited rule table (energetic and
    shock-sensitive motifs such as azides, diazo compounds, peroxides, nitrate esters and
    polynitroaromatics; reactive motifs such as hydrazines and N-halamines) and, when several
    species are given, checks for dangerous *combinations* between them (e.g. a strong oxidizer
    together with a strong reducing agent).

    Call this before recommending a synthesis, a reagent, or a set of conditions, and report every
    flag with its explanation to the chemist.

    **An empty result means no rule in the table matched — it does NOT mean the chemistry is
    safe.** Nothing here assesses toxicity, exposure, thermal stability, scale, or the process
    around the reaction. Never present this tool's output as a safety clearance or as permission
    to run an experiment; it is one input to a human's assessment, which also needs the SDS and,
    for anything energetic, a process-safety review.

    Args:
        smiles: One SMILES per species. Pass a single molecule to screen it alone, or every
            component of a reaction (reactants, reagents, solvents, products) to also check for
            incompatible combinations.

    Returns:
        The matched hazard flags, most serious first, each with a rule id, severity, an
        explanation of the hazard, and the literature citation it rests on.
    """
    if len(smiles) == 1:
        return screen_structure(smiles[0])
    return screen_reaction(smiles)
