"""The `safety` connector's MCP tool surface: the hazard screen, one tool.

The capability itself is unchanged and stays in `safety/screen.py` — the rule table, the SMARTS
matching, the incompatibility check. This module is only what the agent sees, and it is where
the tool's *description* lives, because that description is the safety-critical part: it is the
sentence that decides whether the model treats an empty result as "no rule matched" or as
"safe". That wording moved here verbatim from `agents/safety_tools.py`, deliberately unedited.

Why the tool is defined here rather than by importing the old `@tool` function: the point of the
connector seam is that a capability's process holds the capability. Importing the agent module
would drag the tool registry, the audit middleware and the whole `agents` package into a server
whose only job is to answer one question about SMARTS.
"""

from mcp.server.fastmcp import FastMCP

from safety.screen import ScreenResult, screen_reaction, screen_structure

server = FastMCP("safety")


@server.tool()
async def screen_hazards(smiles: list[str]) -> ScreenResult:
    """Screen molecules or a reaction for known structural hazard motifs before proposing them.

    Matches each structure against a curated, literature-cited rule table (energetic and
    shock-sensitive motifs such as azides, diazo compounds, peroxides, nitrate esters and
    polynitroaromatics; reactive motifs such as hydrazines and N-halamines) and, when several
    species are given, checks for dangerous *combinations* between them (e.g. a strong oxidizer
    together with a strong reducing agent).

    Call this before recommending a synthesis, a reagent, or a set of conditions, and report
    every flag with its explanation to the chemist.

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
