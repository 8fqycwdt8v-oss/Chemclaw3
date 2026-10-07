"""Map a BO campaign's recommendation to a knowledge-graph note.

A finished campaign's best point becomes an agent-authored note, readable at once and carrying
`created_by: agent` and its own `calc_refs`; a later campaign over the same space supersedes it.

Mapping only: the single write path stays in core (`ConnectorJobWorkflow` publishes the note the
result envelope carries, and `durable/job_record.py::note_with_run_provenance` stamps the run and
its reason), so this stays a pure function of the campaign.
"""

from rdkit import Chem

from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note
from chemclaw.science.bo.problem import (
    CampaignResult,
    CategoricalParameter,
    Observation,
    OptimizationProblem,
    Parameter,
    ParamValue,
)


def note_from_campaign_result(
    objective_name: str, problem: OptimizationProblem, result: CampaignResult
) -> Note:
    """Map a campaign's best point to an agent-authored `bo-candidate` note.

    Records the recommended conditions, the achieved objective value and whether it was measured or
    predicted, the number of evaluations, and the space that was searched — a recommendation means
    little without its bounds, and the note's reader has no other copy of the decision space.

    The id is the objective plus a hash of the recommended parameters, so recording the same
    recommendation is idempotent; core's appended footer may differ for a differently motivated run.

    The value, its provenance and the surrogate's belief lead the body, because a retrieval excerpt
    is
    a character prefix of the body (`retrieval.retrievers._excerpt`). Molecules are written as
    structures (`_condition`, `_recommended_molecule`) so by-compound paths can find them. The note
    carries no `[[wikilink]]`, which `kg.record` would warn about as dangling.
    """
    best = result.best
    by_name = {parameter.name: parameter for parameter in problem.parameters}
    conditions = "\n".join(
        f"- {_condition(by_name.get(name), name, value)}"
        for name, value in sorted(best.params.items())
    )
    space = "\n".join(f"- {_parameter_range(parameter)}" for parameter in problem.parameters)
    # The "Searched over:" block describes a box; with constraints the campaign searched a polytope,
    # and
    # a reader seeing only the bounds would believe an unavailable corner was available.
    limits = ""
    if problem.constraints:
        stated = "\n".join(f"- {constraint.describe()}" for constraint in problem.constraints)
        limits = f"\nSubject to:\n{stated}\n"
    body = (
        f"Bayesian-optimization recommendation for objective `{objective_name}`, "
        f"from {len(result.history)} evaluation(s).\n\n"
        f"- objective value: {best.value:.6g} "
        f"({best.provenance}; {_surrogate_belief(best, result.history)})\n"
        f"- direction: {problem.objective.direction} `{problem.objective.name}`\n\n"
        f"Recommended conditions:\n{conditions}\n\n"
        f"Searched over:\n{space}\n{limits}"
    )
    return Note(
        id=f"bo-{objective_name}-{stable_hash(dict(best.params), chars=12)}",
        type="bo-candidate",
        created_by="agent",
        source=f"bo:{objective_name}",
        compound_smiles=_recommended_molecule(by_name, best),
        body=body,
    )


def _molecule_in(parameter: Parameter | None, value: ParamValue) -> str | None:
    """The SMILES a recommended parameter value names, or None when it names no molecule.

    A featurized categorical maps labels to SMILES (`CategoricalParameter.structures`); a
    `solubility_max` campaign makes the SMILES itself the label, which RDKit decides.

    Deliberately the lenient `Chem.MolFromSmiles`, not `core.chem.require_molecule`: a false
    positive
    costs backticks around a non-structure, while a false negative hides a structure such as a label
    `CN=[N+]=[N-] (2 equiv)`. `parameter` is optional so a result whose params do not match the
    problem still yields a note rather than a `KeyError`.
    """
    if not isinstance(parameter, CategoricalParameter):
        return None
    label = str(value)
    declared = (parameter.structures or {}).get(label)
    if declared is not None:
        return declared
    return label if Chem.MolFromSmiles(label) is not None else None


def _condition(parameter: Parameter | None, name: str, value: ParamValue) -> str:
    """One recommended parameter value, written so any molecule in it stays machine-readable.

    People and extractors find structures in `compound_smiles` and inline code spans, and a
    `bo-candidate` proposes work nobody has run, so its molecules must be legible. A non-SMILES
    label is
    plain text; a SMILES label is backticked; a label with a declared structure gets that structure
    appended.
    """
    smiles = _molecule_in(parameter, value)
    if smiles is None:
        return f"{name}: {value}"
    if smiles == str(value):
        return f"{name}: `{value}`"
    return f"{name}: {value} (`{smiles}`)"


def _recommended_molecule(by_name: dict[str, Parameter], best: Observation) -> str | None:
    """The molecule this note is *about*, when the recommendation names exactly one.

    `compound_smiles` is where `kg.conflicts` and `find_notes` start. Only set for exactly one
    molecule: a wrong `compound_smiles` is worse than none, and every structure is in the body
    anyway.
    """
    named = [
        smiles
        for name, value in sorted(best.params.items())
        if (smiles := _molecule_in(by_name.get(name), value)) is not None
    ]
    return named[0] if len(named) == 1 else None


def _surrogate_belief(best: Observation, history: list[Observation]) -> str:
    """What the model thought of this point before it was evaluated, in one clause.

    A recorded sd means the surrogate proposed the point (small: exploiting, large: exploring); no
    sd
    means it came from the seed design, which is said explicitly. The sd is the model's prior
    belief,
    never the uncertainty of the reported value, and it is compared against the campaign's value
    spread, as `ExperimentSuggestion.summary` does for the inline tool.
    """
    if best.surrogate_sd is None:
        return "a space-filling seed point, proposed before any surrogate had an opinion"
    belief = f"surrogate posterior sd ±{best.surrogate_sd:.3g} at the time it was proposed"
    values = [observation.value for observation in history]
    spread = max(values) - min(values) if len(values) > 1 else 0.0
    if spread <= 0:
        return belief
    return f"{belief}, against an observed spread of {spread:.3g} across the campaign"


def _parameter_range(parameter: Parameter) -> str:
    """One decision variable as a single line: its name and what it was allowed to be.

    Categorical options are listed, bounded by the shared `note_excerpt_chars` budget — a
    `solubility_max` campaign makes every library SMILES a level. Past the budget the line says how
    many were omitted; the full space is in the run's durable record.
    """
    if not isinstance(parameter, CategoricalParameter):
        return f"{parameter.name}: {parameter.lower:g} to {parameter.upper:g}"
    shown: list[str] = []
    budget = settings.note_excerpt_chars
    for category in parameter.categories:
        # +2 for the ", " this level costs once it is not the first.
        budget -= len(category) + 2
        if budget < 0 and shown:
            break
        shown.append(category)
    listed = ", ".join(shown)
    omitted = len(parameter.categories) - len(shown)
    if omitted:
        listed += f", … (+{omitted} more; the full set is in the run record)"
    return f"{parameter.name}: one of {listed}"
