"""Map a BO campaign's recommendation to a knowledge-graph note (plan step 1d.5).

A finished campaign's best point is the experiment the optimizer recommends running next; like a QM
result, it becomes an agent-authored note proposed through the **same** PR-gate (D-005) so a human
validates before it enters the graph.

This module is the *mapping only*, which is the connector split: turning a campaign result into a
note is the BO domain's knowledge, so it lives in the bundle; pushing that note through the PR-gate
is the GxP boundary, so it stays in core (`ConnectorJobWorkflow` publishes whatever note the result
envelope carries). The activity that used to do both is gone — a connector must not be able to reach
around the gate, and now it structurally cannot.

Core also stamps the run and *why it was started* onto this note on the way through
(`durable/job_record.py::note_with_run_provenance`, D-157). So this builder answers "what came out
and over what space", and never has to know the job id or the requester — which is what keeps the
mapping a pure function of the campaign.
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

    The note records the recommended conditions, the achieved objective value and whether
    it was measured or predicted (`provenance`), and how many evaluations backed the
    recommendation — the context a reviewer needs before approving a lab run.

    It also records the **space that was searched**, which the earlier version left out (D-157). A
    recommendation of "1.2 mol% Pd" means one thing when the campaign could have gone to 5 mol% and
    something else entirely when 1.2 was the ceiling, and the reader of a merged note has no other
    copy of the decision space: the spec lives in the durable job record and in Temporal's history,
    neither of which is in front of someone reviewing a markdown file.

    The id is the objective plus a hash of the recommended parameters, so re-proposing the same
    recommendation is idempotent. The *body* is not quite: core appends the run and its reason
    (D-157), so a second, differently-motivated campaign that lands on the same point proposes the
    same note id with a different footer — which is a real difference (two runs agreeing, for two
    reasons) and one a reviewer should see. The identical campaign never gets that far: it rejoins
    the first run's id and never re-executes.

    **The value comes before the conditions, and carries the surrogate's opinion of it** (F8-T1).
    Both are the same fix. A retrieval excerpt is a blind character prefix of the body
    (`retrieval.retrievers._excerpt`, 240 characters by default), and the objective value used to
    sit *after* the full conditions list — so a campaign over five or six parameters produced an
    excerpt quoting the recommended conditions with no number attached at all, which is the worst
    of the possible truncations. Leading with the number puts it, its provenance and the model's
    own uncertainty about it inside the prefix that actually gets quoted back.

    **The molecules it recommends are written as structures, not as prose** — see `_condition` and
    `_recommended_molecule`. That is what puts a `bo-candidate` in front of the hazard gate, which
    could not see one at all before.

    The note carries no `[[wikilink]]` (a dangling link would fail `chemclaw.kg.validate` on the
    very PR this opens).
    """
    best = result.best
    by_name = {parameter.name: parameter for parameter in problem.parameters}
    conditions = "\n".join(
        f"- {_condition(by_name.get(name), name, value)}"
        for name, value in sorted(best.params.items())
    )
    space = "\n".join(f"- {_parameter_range(parameter)}" for parameter in problem.parameters)
    # The "Searched over:" block describes a box. The moment a constraint reaches the durable path
    # the campaign searched a *polytope* instead, and a reviewer reading only the bounds would
    # believe a corner was available that never was — the same defect D-157 fixed one field over,
    # where the note recorded a recommendation with no decision space at all.
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

    A campaign declares "this choice is a molecule" in one of two ways and both have to be read
    here, because between them they cover every shipped objective: a featurized categorical carries
    an explicit label → SMILES map (`CategoricalParameter.structures`), while a library campaign
    (`science.bo.objectives.molecule_library_problem`) makes the SMILES *itself* the category
    label. Only the second needs RDKit, and only to answer "is this label a structure at all" —
    the same arbiter, for the same reason, that `science.safety.notes.structures_in` applies to a
    note body: a heuristic over label spellings would be a second, weaker answer to one question.

    `parameter` is optional because the caller looks it up by name from the recommended point: a
    result whose params do not line up with the problem still has to yield a readable note rather
    than a `KeyError` that loses a completed campaign.
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

    The backticks are load-bearing rather than styling. The hazard gate reads a note's structures
    out of `compound_smiles` and the body's inline code spans
    (`science/safety/notes.py::structures_in`), and this line is where a `bo-candidate` names the
    molecules it is asking a human to put in a flask. Emitted as plain prose — `- molecule:
    CCN=[N+]=[N-]`, which is what this wrote — the gate found *no* structures in any machine-minted
    candidate and passed every one of them unscreened, organic azides included. That is the exact
    inversion of the gate's purpose: `bo-candidate` is the note type proposing work nobody has run,
    so it is the one type with no chemist who has already formed a judgment about the mixture.

    A label that is not a SMILES yields nothing downstream (RDKit arbitrates), so it is left as
    plain text; a label that *is* one is backticked; and a label with a declared structure behind
    it gets that structure appended, because "L7" is not something any extractor could resolve.
    """
    smiles = _molecule_in(parameter, value)
    if smiles is None:
        return f"{name}: {value}"
    if smiles == str(value):
        return f"{name}: `{value}`"
    return f"{name}: {value} (`{smiles}`)"


def _recommended_molecule(by_name: dict[str, Parameter], best: Observation) -> str | None:
    """The molecule this note is *about*, when the recommendation names exactly one.

    `compound_smiles` is where every by-compound question starts — `kg.conflicts` groups on
    `(type, compound_smiles)`, `find_notes` searches it, and the hazard gate reads it first — and
    a `bo-candidate` carried none, so a recommendation to *make a specific molecule* was invisible
    to all three.

    Only when there is exactly one, for the reason `ingest/eln/note.py::_principal_product` gives
    about the same field: "the molecule this note is about" has no honest answer for a
    recommendation naming a ligand *and* a substrate, and picking one would file the note under a
    compound nobody chose. A wrong `compound_smiles` is worse than none — it is what a by-compound
    search returns, and it would look right. Nothing is lost for the gate either way: every
    recommended structure is in the body as a code span.
    """
    named = [
        smiles
        for name, value in sorted(best.params.items())
        if (smiles := _molecule_in(by_name.get(name), value)) is not None
    ]
    return named[0] if len(named) == 1 else None


def _surrogate_belief(best: Observation, history: list[Observation]) -> str:
    """What the model thought of this point before it was evaluated, in one clause (F8-T1).

    Two honest readings, and the distinction is the one a reviewer needs. A recorded sd means the
    surrogate proposed this point and says how sure it was of the region: small is an exploit of
    chemistry it has learned, large an excursion into chemistry it has not. No sd means no model
    was involved — the point came from the space-filling seed design — which is a different claim
    entirely and reads as an endorsement if left unsaid.

    Never phrased as the uncertainty *of* the reported value: that value came from the evaluator,
    not from the surrogate, and the sd is what the model believed beforehand.

    **The spread comes with it**, because an sd alone is not a reading. ±3 is an exploit when the
    campaign's values span 40 and an excursion when they span 4, and a reviewer deciding whether to
    book lab time needs the comparison rather than the raw number. `ExperimentSuggestion.summary`
    makes the same comparison for the inline tool; they are written together so the note and the
    tool cannot drift into two answers to one question.
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

    Categorical options are listed rather than counted — "one of 4 ligands" tells a reviewer
    nothing about whether the ligand they would have tried was even on the list — but the listing
    is **bounded**, because one shipped objective makes it unbounded: `molecule_library_problem`
    turns a screening library into one categorical whose levels are every SMILES in it, so a
    500-molecule campaign would write a single 12 KB line into a note whose job is to let a chemist
    decide on one experiment. Past the budget it says how many were left out, and the complete
    space stays one lookup away in the run's durable record (D-157), which is the column that
    exists for exactly this.

    The budget is the shared `note_excerpt_chars` — the one note-excerpt allowance the report
    harness and the memory layer already spend — so this cannot drift into a second answer to
    "how much prose belongs in a note".
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
