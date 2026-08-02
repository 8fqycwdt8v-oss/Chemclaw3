"""The `bo` connector's MCP tool surface: one-shot Bayesian-optimization experiment design.

Exposes BoFire's ask step to the conversation agent so a "which experiment should I run next?"
question is answered from data the agent has already gathered: the agent assembles the decision
space and the runs so far (from ELN history via the research tools) and asks for the next
point(s) to try. Like the fast calculators, a single ask is inline and sub-second — the GP fit
runs off the event loop; the durable `BoCampaignWorkflow` remains the path for an *automated*
closed loop that evaluates its own objective over many rounds. This tool is the one-shot
human-in-the-loop suggestion.

`resume_campaign` is the read side of that: every suggestion is recorded against the campaign its
decision space defines, and until it existed nothing could read one back, so the `campaign_id` the
suggestion tool tells the agent to quote was a handle onto a store with no reader and the
ask→observe→ask loop could not cross a session.

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
from pydantic import BaseModel, Field

from chemclaw.connectors.caller import caller_provenance
from chemclaw.science.bo.campaign_record import (
    CampaignThread,
    read_campaign_thread,
    record_suggestion,
)
from chemclaw.science.bo.engine import factorial_design, initial_candidates, propose_candidates
from chemclaw.science.bo.featurize import featurize_problem
from chemclaw.science.bo.problem import Candidate, Observation, OptimizationProblem, ScreeningDesign
from chemclaw.science.calc.postgres_store import default_store

server = FastMCP("bo")


class ExperimentSuggestion(BaseModel):
    """Proposed experiments, the campaign they belong to, and the calculations behind the space.

    A richer return than the bare `list[Candidate]` it replaces, because the candidates alone were
    the part that was never the problem: they were computed, returned, and — with nothing else
    carried out of the call — the framing that produced them was discarded every turn. The
    `campaign_id` is what a later turn quotes to add observations to the same optimization, and
    `calc_refs` is what an `experiment-proposal` note cites so a stale calculation can be traced to
    the experiment it suggested.
    """

    campaign_id: str = Field(min_length=1)
    candidates: list[Candidate] = Field(default_factory=list)
    calc_refs: list[str] = Field(default_factory=list)


@server.tool()
async def suggest_next_experiment(
    problem: OptimizationProblem,
    observations: list[Observation] | str | None = None,
    count: int = 1,
) -> ExperimentSuggestion:
    """Suggest the next experiment(s) to run for an optimization problem (Bayesian optimization).

    Answers "what should I try next?" Give the decision space (which conditions may vary and
    their ranges/choices) and the runs done so far (their conditions and the measured
    objective); it returns the point(s) a surrogate model expects to be most informative. With
    no observations yet it returns space-filling seed points instead (a model needs data first).
    These are *proposals a human runs* — surface them, do not treat them as results.

    **One objective, no constraints.** `problem` carries exactly one `objective`, and there is no
    field for a constraint of any kind — they are not partially supported, they are
    unrepresentable. If the chemist named several objectives (yield *and* selectivity, cost *and*
    throughput), pick the one they led with, say which one you optimized, and never present the
    result as a trade-off or a Pareto front: nothing here computed one. Speak to the other
    objectives, if at all, as a separate qualitative reading of the evidence you cited. A limit the
    chemist stated ("keep the temperature under 80 °C", "no more than 2 equivalents") has to be
    built into the parameter bounds or the category list, because the optimizer will not honour it
    otherwise.

    **Continuing an earlier campaign?** Call `resume_campaign(campaign_id)` first to recover the
    decision space and the runs it already has, then add the new results and call this tool.

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

    The suggestion is **recorded** against the campaign this problem defines, and the returned
    `campaign_id` is the handle for it. Quote that id back to the chemist: asking again about the
    same decision space accumulates onto the same campaign, so the sequence of proposals — and the
    evidence each rested on — becomes the campaign's history instead of being discarded with the
    turn. `calc_refs` names the calculations behind the decision space's descriptors; pass them to
    `propose_knowledge_note` if you draft an `experiment-proposal` note from this, so a stale
    calculation can be traced to the experiment it suggested.

    Returns:
        The proposed candidate point(s), the campaign they belong to, and the calculation keys the
        decision space was built from.
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
        candidates = await asyncio.to_thread(propose_candidates, featurized.problem, history, count)
    else:
        candidates = await asyncio.to_thread(initial_candidates, featurized.problem, count)
    # Recorded after the candidates exist and never at the cost of them: `record_suggestion`
    # swallows its own failures, because the chemist asked for a suggestion and a database blip
    # must not turn one into an error. The campaign id is a pure function of the problem, so it is
    # the right handle to return even on the turn where the write did not land.
    campaign_id = await record_suggestion(
        problem=featurized.problem,
        candidates=candidates,
        observations=history,
        calc_refs=featurized.calc_refs,
        provenance=caller_provenance(),
    )
    return ExperimentSuggestion(
        campaign_id=campaign_id, candidates=candidates, calc_refs=featurized.calc_refs
    )


@server.tool()
async def resume_campaign(campaign_id: str) -> CampaignThread:
    """Pick up a Bayesian-optimization campaign started in an earlier turn or session.

    Use this whenever a chemist refers back to an optimization already under way — "here is the
    result of the run you suggested", "where did we get to on the amination screen" — and you have
    the `campaign_id` from a previous `suggest_next_experiment` answer. It returns the decision
    space as it was framed, the runs the last suggestion rested on, and the candidates that
    suggestion proposed, so the next ask builds on the campaign's real history instead of on
    whatever survived in the conversation.

    The normal follow-up is: resume, append the chemist's new result to `observations`, then call
    `suggest_next_experiment` with the returned `problem` and the extended observation list.

    A campaign id is a **hash of the decision space**, not a serial number. So an id that does not
    resolve almost always means the space has changed since — a widened bound, an added or swapped
    option — and the new space is a different campaign with no history yet. This tool raises in
    that case rather than quietly answering from nothing; treat the error as "this is a new
    campaign", say so, and ask for a fresh suggestion.

    Args:
        campaign_id: The id quoted in an earlier `suggest_next_experiment` answer.

    Returns:
        The campaign's objective and direction, its decision space, the observations behind its
        latest suggestion, and the candidates that suggestion proposed.
    """
    return await read_campaign_thread(campaign_id)


@server.tool()
async def generate_screening_design(
    problem: OptimizationProblem, n_generators: int = 0
) -> ScreeningDesign:
    """Generate a factorial screening design over categorical conditions — full grid or reduced.

    Use this for the *other* classical DoE question — "run every combination of these discrete
    choices" — e.g. every catalyst x solvent x base combination before narrowing to a BO campaign,
    or a robustness matrix of discrete method parameters. This is a complete, up-front design a
    human runs as a batch; it does not adapt to results the way `suggest_next_experiment` does.

    **When the full grid does not fit the plate, reduce it.** `n_generators` halves the run count
    per generator: seven two-level factors are 128 runs at 0, then 64, 32, 16 — so a screen that
    could not be run at all becomes one that fits 96 wells. Reduce only when the chemist's budget
    demands it, and never quietly: the returned design carries a `resolution` and a `summary`
    saying exactly which combinations were given up and which effects are confounded as a result.
    **Repeat that summary to the chemist.** A fractional design presented as if it were the whole
    screen is the failure this field exists to prevent. Every factor must have exactly two levels
    for a reduced design; a three-level factor is refused rather than crossed in full.

    Only categorical parameters are supported: a continuous parameter (temperature, equivalents)
    raises rather than being silently ignored from the design. Discretize it into levels first
    (e.g. temperature as "low"/"high") if it belongs in the screen, or use
    `suggest_next_experiment` for a continuous decision space.

    Args:
        problem: The decision variables (categorical only) and the objective (its direction is
            not used by a screening design, but the same `OptimizationProblem` shape is reused so
            observations from the screen can seed a follow-up `suggest_next_experiment` campaign).
        n_generators: 0 (default) for every combination. Each step above 0 halves the design and
            requires every factor to have exactly two levels.

    Returns:
        The runs to perform, plus `resolution` and a `summary` stating whether the design is
        exhaustive or a stated fraction of the grid.
    """
    return await asyncio.to_thread(
        factorial_design, OptimizationProblem.model_validate(problem), n_generators
    )
