"""The `safety` connector's MCP tool surface: three cited tables, three questions, three tools.

The capabilities themselves stay in `science/`; this module is only what the agent sees, and it is
where each tool's *description* lives, because the description is the safety-critical part. It is
the sentence that decides whether the model treats an empty result as "no rule matched" or as
"safe" — the `screen_hazards` wording moved here verbatim from `agents/safety_tools.py`,
deliberately unedited.

**Three tools rather than one, and the split is the point.** They answer three questions a chemist
asks separately, and the previous single answer was what let one get reported as another:

- `screen_hazards` (`science/safety/screen.py`) — "is this safe to run today": energetic and
  reactive motifs, and dangerous combinations between a reaction's components.
- `screen_genotoxic_alerts` (`science/safety/genotox.py`) — "will this need a control strategy":
  DNA-reactive structural alerts, which are *not* an ICH M7 classification.
- `ich_impurity_limit` (`science/safety/ich.py`) — "what is the number": the transcribed ICH Q3C
  and Q3D limits, with a citation, and an honest miss when the tables do not carry the substance.

Why the tools are defined here rather than by importing an agent-side `@tool` function: the point
of the connector seam is that a capability's process holds the capability. Importing the agent
module would drag the tool registry, the audit middleware and the whole `agent` package into a
server whose only job is to answer questions about SMARTS and lookup tables.

**The screens run in a worker thread, and their input is bounded.** SMARTS matching is CPU-bound
C++ that holds the GIL, and this server answers every connected chat turn on one event loop — the
same reasoning `Chemclaw3-mcp:servers/chem/src/chemclaw_mcp_chem/tools.py` records, which this
bundle was the last not to
follow. It matters more here than there: both screens check their pair rules as a cross-product,
so results grow with the *square* of a caller-supplied list while the request stays tiny (13 KiB
of SMILES measured at 251,000 flags and 2.48 s of blocked loop). `safety_max_components` bounds
the input; `asyncio.to_thread` keeps even a bounded screen off the loop that serves everyone else.
`ich_impurity_limit` is a dictionary lookup over two small tables and needs neither.
"""

import asyncio

from mcp.server.fastmcp import FastMCP

from chemclaw.science.safety import genotox
from chemclaw.science.safety.genotox import AlertResult
from chemclaw.science.safety.ich import ImpurityLimitLookup, impurity_limit
from chemclaw.science.safety.screen import ScreenResult, screen_reaction, screen_structure

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

    **A SMILES this cannot read in full is refused, not screened.** Pass one structure per list
    entry and nothing else in the string — no name beside it, no two structures run together, no
    units. RDKit stops at the first space, so a string like `"CCO 1-azidopropane"` would otherwise
    be screened as ethanol and come back clean. If a call is refused, fix the string and ask again;
    never report the refusal as a result.

    Args:
        smiles: One SMILES per species. Pass a single molecule to screen it alone, or every
            component of a reaction (reactants, reagents, solvents, products) to also check for
            incompatible combinations.

    Returns:
        The matched hazard flags, most serious first, each with a rule id, severity, an
        explanation of the hazard, and the literature citation it rests on, plus `screened` — the
        canonical SMILES of every structure the result actually covers, which is how a reader
        confirms the screen is about the molecules they meant.
    """
    if len(smiles) == 1:
        return await asyncio.to_thread(screen_structure, smiles[0])
    return await asyncio.to_thread(screen_reaction, smiles)


@server.tool()
async def screen_genotoxic_alerts(smiles: list[str]) -> AlertResult:
    """Flag DNA-reactive structural alerts in a molecule or a synthetic route.

    Use this for the regulatory-toxicology question — "will this compound or this route need a
    mutagenic-impurity control strategy?" — and for anything about nitrosamine risk. It is a
    *different* question from `screen_hazards`, which asks whether the chemistry is safe to run
    today; call whichever one the chemist actually asked about, and both when they asked about
    both.

    Matches a committed, cited table of alerts (N-nitroso, nitroaromatic, primary aromatic amine,
    aromatic azo, epoxide, aziridine, mono-functional alkyl halide, alkyl sulfonate/sulfate ester,
    Michael acceptor) and, across several components, the nitrosamine formation route — a
    nitrosatable amine meeting a nitrosating agent.

    **A flag is an alert, not a classification, and you must report it as one.** This system has no
    (Q)SAR pair, no Ames corpus and no expert rule base, so it cannot produce an ICH M7 class, an
    acceptable intake, a purge factor or a mutagenicity prediction — and neither can you. Never
    state one, not even as an illustration. An empty result is equally not a negative prediction:
    the table is nine alerts long, and absence means nothing in it matched.

    **A SMILES this cannot read in full is refused, not screened**, and the refusal names the
    component's position in the list — one structure per entry, with nothing else in the string.
    RDKit stops at the first space, so `"CCO O=[N+]([O-])c1ccccc1"` would otherwise be read as
    ethanol and answer "no alert matched" while ignoring the nitroarene.

    Args:
        smiles: One SMILES per species. Pass the whole route rather than one step — the formation
            alert can only see components given to it together, so a nitrosating agent introduced
            two steps later is invisible to a per-step call.

    Returns:
        The matched alerts, each with the motif it names, why it is an alert, and the published
        alert set it comes from — plus `screened` (the canonical SMILES of every structure the
        result covers) and a verdict that states what the result does not mean.
    """
    return await asyncio.to_thread(genotox.screen_genotoxic_alerts, smiles)


@server.tool()
async def ich_impurity_limit(substance: str) -> ImpurityLimitLookup:
    """Look up an ICH Q3C residual-solvent limit or an ICH Q3D elemental-impurity PDE.

    Call this **whenever a number from either guideline is about to appear in an answer** — a
    residual-solvent class or concentration limit, a permitted daily exposure for a metal
    catalyst. Do not recall one: a recalled limit that happens to be right is worse than a wrong
    one, because it teaches the reader to trust the next.

    Accepts the guideline's spelling, an element symbol, an abbreviation a chemist writes, or a
    SMILES — `Pd`, `palladium`, `THF`, `2-MeTHF` and `C1CCOC1` all resolve.

    A miss returns `limit: null` with a verdict saying so. That means these tables do not carry the
    substance, **not** that no limit exists; say exactly that and point at the guideline. Never
    substitute a value for a similar substance.

    The tables give the number a judgement needs; they are not the judgement. Whether a process
    needs a given control, what specification an intermediate should carry, and how a PDE converts
    into a limit on an API are all assessments this tool does not make.

    Args:
        substance: The solvent or element to look up.

    Returns:
        The transcribed row — class, meaning, limits with their units, and the guideline, revision
        and table the figures came from — or an explicit miss.
    """
    return impurity_limit(substance)
