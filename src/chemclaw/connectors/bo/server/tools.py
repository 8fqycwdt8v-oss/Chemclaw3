"""The `bo` connector's MCP tool surface: one-shot Bayesian-optimization experiment design.

Exposes BoFire's ask step to the conversation agent so a "which experiment should I run next?"
question is answered from data the agent has already gathered: the agent assembles the decision
space and the runs so far (from ELN history via the research tools) and asks for the next
point(s) to try. Like the fast calculators, a single ask is inline and sub-second — the GP fit
runs off the event loop; the durable `BoCampaignWorkflow` remains the path for an *automated*
closed loop that evaluates its own objective over many rounds. This tool is the one-shot
human-in-the-loop suggestion.

Layer discipline (G6): this is read-only *capability*. The judgment — how to turn a vague
"optimize the reaction" into a concrete problem, which historic runs are comparable enough to
seed it, and how to present a suggestion a human must still run — lives in the bundled
`experiment-design` skill.

BoFire lives on this side of the connector boundary now: only the neutral
`chemclaw.science.bo.problem` types
cross it, as the model-facing schema of this tool and of the campaign job. That is what keeps
`bofire` and `botorch` out of the chat service's image while the agent can still ask both
questions.
"""

import asyncio
import json

from mcp.server.fastmcp import FastMCP

from chemclaw.science.bo.engine import factorial_design, initial_candidates, propose_candidates
from chemclaw.science.bo.featurize import featurize_problem
from chemclaw.science.bo.problem import Candidate, Observation, OptimizationProblem, ScreeningDesign
from chemclaw.science.calc.postgres_store import default_store

server = FastMCP("bo")


@server.tool()
async def suggest_next_experiment(
    problem: OptimizationProblem,
    observations: list[Observation] | str | None = None,
    count: int = 1,
) -> list[Candidate]:
    """Suggest the next experiment(s) to run for an optimization problem (Bayesian optimization).

    Answers "what should I try next?" Give the decision space (which conditions may vary and
    their ranges/choices) and the runs done so far (their conditions and the measured
    objective); it returns the point(s) a surrogate model expects to be most informative. With
    no observations yet it returns space-filling seed points instead (a model needs data first).
    These are *proposals a human runs* — surface them, do not treat them as results.

    Build `problem` and `observations` from evidence you have gathered (e.g. past runs of the
    transformation via similar_reactions / an optimization-campaign note), so the suggestion
    rests on real history. Mark each observation's `provenance` "measured" for lab data or
    "predicted" if it came from a model, keeping the campaign honest.

    **When a categorical choice is a molecule** — a ligand, base, solvent, or catalyst — give
    its `structures` (a mapping from each category label to its SMILES). Each option is then
    described by computed electronic descriptors instead of being an opaque label, so the
    model can reason about an option nobody has run yet rather than only about the ones with
    data. This costs one fast calculation per option and is cached thereafter.

    Args:
        problem: The decision variables (continuous/categorical) and the single objective
            (name + minimize/maximize). Set a categorical's `structures` when its options are
            molecules.
        observations: Runs already done, each mapping the parameter values to the objective
            value. Omit or pass an empty list to get seed points for a fresh campaign.
        count: How many candidates to propose (a batch).

    Returns:
        The proposed candidate point(s), each a mapping of parameter name to value.
    """
    # A tool call arrives as JSON, so the framework hands this function plain dicts and lists —
    # never `OptimizationProblem`/`Observation` instances, because the wire format has no model
    # concept. This is the one tool here with a nested-model parameter, so it is the one boundary
    # that has to bridge back into typed objects. Re-validating an already-correct instance is a
    # no-op (`model_validate` short-circuits on an exact-type match), so every direct and test
    # caller that passes real models is unaffected.
    problem = OptimizationProblem.model_validate(problem)
    # On a large batch the model occasionally emits the observations array JSON-*encoded* as a
    # single string rather than as an array — a live e2e finding on a six-parameter problem.
    # Schema validation would otherwise reject the whole call before this body runs, with no
    # detail reaching the model to self-correct from. Accepting the string is strictly more
    # permissive (a real list is untouched), so it is robustness, not a behaviour change.
    if isinstance(observations, str):
        observations = json.loads(observations)
    history = [Observation.model_validate(item) for item in observations] if observations else []
    # Featurize before the engine sees the problem: descriptors change how the surrogate
    # models the categorical space, so this must happen for the seeding path too — otherwise
    # a problem that declares structures would silently fall back to an opaque category.
    # Runs *after* the coercion above, because it needs a real `OptimizationProblem`.
    featurized = await featurize_problem(default_store(), problem)
    if history:
        return await asyncio.to_thread(propose_candidates, featurized, history, count)
    return await asyncio.to_thread(initial_candidates, featurized, count)


@server.tool()
async def generate_screening_design(problem: OptimizationProblem) -> ScreeningDesign:
    """Generate a full-factorial screening design over categorical conditions.

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
    return await asyncio.to_thread(factorial_design, OptimizationProblem.model_validate(problem))
