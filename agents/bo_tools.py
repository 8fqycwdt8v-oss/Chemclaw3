"""Agent tool for Bayesian-optimization experiment design (plan Phase 1d, agent surface).

Exposes BoFire's ask step to the conversation agent so a "which experiment should I run
next?" question is answered from data the agent has already gathered: the agent assembles the
decision space and the runs so far (from ELN history via the research tools) and asks for the
next point(s) to try. Like the fast calculators, a single ask is inline and sub-second — the
GP fit runs off the event loop; the durable `BoCampaignWorkflow` remains the path for an
*automated* closed loop that evaluates its own objective over many rounds. This tool is the
one-shot human-in-the-loop suggestion.

Layer discipline (G6): this is read-only *capability*. The judgment — how to turn a vague
"optimize the reaction" into a concrete problem, which historic runs are comparable enough to
seed it, and how to present a suggestion a human must still run — lives in the
`experiment-design` skill. BoFire is never imported here; only the neutral `bo.problem` types
cross this boundary.
"""

import asyncio

from agents.tool_registry import tool
from bo.engine import initial_candidates, propose_candidates
from bo.problem import Candidate, Observation, OptimizationProblem


@tool
async def suggest_next_experiment(
    problem: OptimizationProblem,
    observations: list[Observation] | None = None,
    count: int = 1,
) -> list[Candidate]:
    """Suggest the next experiment(s) to run for an optimization problem (Bayesian optimization).

    Answers "what should I try next?" Give the decision space (which conditions may vary and
    their ranges/choices) and the runs done so far (their conditions and the measured
    objective); it returns the point(s) a surrogate model expects to be most informative. With
    no observations yet it returns space-filling seed points instead (a model needs data
    first). These are *proposals a human runs* — surface them, do not treat them as results.

    Build `problem` and `observations` from evidence you have gathered (e.g. past runs of the
    transformation via similar_reactions / an optimization-campaign note), so the
    suggestion rests on real history. Mark each observation's `provenance` "measured" for lab
    data or "predicted" if it came from a model, keeping the campaign honest.

    Args:
        problem: The decision variables (continuous/categorical) and the single objective
            (name + minimize/maximize).
        observations: Runs already done, each mapping the parameter values to the objective
            value. Omit or pass an empty list to get seed points for a fresh campaign.
        count: How many candidates to propose (a batch).

    Returns:
        The proposed candidate point(s), each a mapping of parameter name to value.
    """
    history = observations or []
    if history:
        return await asyncio.to_thread(propose_candidates, problem, history, count)
    return await asyncio.to_thread(initial_candidates, problem, count)
