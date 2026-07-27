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
import json

from agents.tool_registry import tool
from bo.engine import factorial_design, initial_candidates, propose_candidates
from bo.problem import Candidate, Observation, OptimizationProblem, ScreeningDesign


@tool
async def suggest_next_experiment(
    problem: OptimizationProblem,
    observations: list[Observation] | str | None = None,
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
    # MAF validates a tool call's arguments against the JSON schema derived from this
    # signature, then invokes the function with that validated payload `model_dump()`-ed back
    # to plain dicts/lists — never with `OptimizationProblem`/`Observation` instances (the JSON
    # tool-call wire format has no model concept, only object/array/string/number). This is the
    # one registered tool with a nested-model parameter, so it is the one boundary that needs
    # bridging back into typed objects; re-validating an already-correct instance is a no-op
    # (`model_validate` short-circuits on an exact-type match), so this is transparent to every
    # direct/test caller that already passes real model instances.
    problem = OptimizationProblem.model_validate(problem)
    # On a large batch of observations the model occasionally emits the array JSON-encoded as a
    # single string instead of a real array (a live e2e finding on a 6-parameter problem) — MAF's
    # schema validation would otherwise reject the whole call before this function ever runs, with
    # no detail reaching the model to self-correct from. Accepting the string here and decoding it
    # is strictly more permissive than before (a real list is untouched), so this is pure
    # robustness, not a behavior change for the common case.
    if isinstance(observations, str):
        observations = json.loads(observations)
    history = [Observation.model_validate(o) for o in observations] if observations else []
    if history:
        return await asyncio.to_thread(propose_candidates, problem, history, count)
    return await asyncio.to_thread(initial_candidates, problem, count)


@tool
async def generate_screening_design(problem: OptimizationProblem) -> ScreeningDesign:
    """Generate a full-factorial screening design over categorical conditions (D-092).

    Use this for the *other* classical DoE question — "run every combination of these discrete
    choices" — e.g. every catalyst x solvent x base combination before narrowing to a BO campaign,
    or a robustness matrix of discrete method parameters. This is a complete, up-front design a
    human runs as a batch; it does not adapt to results the way `suggest_next_experiment` does.

    Only categorical parameters are supported: a continuous parameter (temperature, equivalents)
    raises rather than being silently ignored from the design. Discretize it into levels first
    (e.g. temperature as "low"/"high") if it belongs in the screen, or use
    `suggest_next_experiment` for a continuous decision space.

    Args:
        problem: The decision variables (categorical only) and the objective (its direction is
            not used by a screening design, but the same `OptimizationProblem` shape is reused so
            observations from the screen can seed a follow-up `suggest_next_experiment` campaign).

    Returns:
        Every combination of the categorical levels, one dict of parameter name to value per run.
    """
    return await asyncio.to_thread(factorial_design, problem)
