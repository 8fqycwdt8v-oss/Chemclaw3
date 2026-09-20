"""Deciding whether a discriminating check can be run without inventing anything.

**The rule this module exists to enforce: nothing a model writes becomes a tool argument.** A
tournament derives a check per hypothesis, and where the check is answerable by this system's own
calculators the temptation is to let the model fill in the call. Every field it would fill —
which molecule, which solvent, which charge — is a field it fills *plausibly* when it does not
know, and a fabricated argument produces a real number that a chemist reads as computed. A missing
verdict is visible; a confidently wrong one is not. That asymmetry is the whole design.

So the model may only **select**, never write:

- a **tool**, which has to pass `refuse_unless_dispatchable`; and
- a **subject**, as a note id that has to resolve in this deployment's corpus to a `compound` note
  whose `compound_smiles` parses as a molecule.

The structure handed to the calculator is read off the resolved note. That is the same move
`agent/protocol_design_tools.require_quotes_are_verbatim` makes when it refuses a `stated` slot
whose quote is not in the chemist's own words: the model points at something real, and the pointer
is checked rather than trusted. That function's sharpest lesson is copied too — its evidence is
*ambient* rather than a parameter, because a check whose evidence the caller supplies is one the
caller can satisfy by supplying it. Here the corpus plays that role: the note is resolved from the
deployment's own knowledge tree, never from anything travelling with the check.

**The keys half of the contract is not re-derived here.** `agent/template_surface.ToolArguments`
already reduces "what does this tool accept" to `accepted` / `required` / `takes_any_key`, from
either a local signature or a running server's schema, and its own docstring says why there is one
definition: "two lanes disagreeing about what a template's arguments mean would be worse than the
gap the second one closes." A third reading of the same question would be that failure again, so
callers hand the answer in and this module only applies policy to it.

**What this adds to those three terms is a type, and only because it can.** `argument_problems`
checks keys and never values, correctly: a template's argument may be a `${...}` reference whose
type is unknown until substitution. A check's subject is not — it is a SMILES string, known now.
That matters concretely: `screen_hazards` declares its structure argument as a *list* of SMILES,
so a keys-only check would pass a call that hands it a bare string.

**The signature check is necessary and is not sufficient, which is the part that cost the most to
get right.** Arity catches a tool that would make a model invent an argument. It does not catch a
tool whose *default* is wrong for the molecule in hand, and `compute_thermochemistry` is exactly
that: `symmetry_number: int = 1` means "no symmetry", which is false for most symmetric molecules
and shifts the entropy — and so the free energy — by `R·ln(σ)` with nothing in the output saying
so. A silently wrong number is the failure this whole module exists to prevent, so a computed
allowlist alone would have shipped one. `_UNSAFE_DEFAULTS` carries the residue, each entry naming
the parameter and what goes wrong, and `tests/test_hypothesis_dispatch.py` holds every name in it
to a tool that actually exists.

**An unreadable contract refuses here, and that is the opposite of `core/connect.py`.**
`signature_mismatch` returns "nothing to say" for a callable it cannot introspect, and is right to:
a driver that refuses a keyword still refuses it at construction, so the offline check sits in front
of a real gate. There is no second gate here. A calculator handed a structure it did not expect
does not refuse — it computes something, and the number comes back looking exactly like an answer.
Where the fallback is silence, permissiveness is free; where the fallback is a plausible number, it
is the defect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from chemclaw.kg.note import Note

#: The one argument name a dispatchable tool may require. Every calculator that answers from a
#: structure alone calls it `smiles`; a tool naming it something else is not recognised and refuses,
#: which is the safe direction — this is a whitelist of one name rather than a guess at what some
#: other parameter might mean.
STRUCTURE_ARGUMENT = "smiles"

#: The note type a subject must resolve to. A `compound` note carries `compound_smiles` in its
#: frontmatter, which is corpus data written by whoever curated the note — never model prose.
SUBJECT_NOTE_TYPE = "compound"

#: Tools whose *signature* qualifies but whose defaults do not, with what goes wrong. Arity cannot
#: see these, and each one would produce a real number that is quietly wrong for the molecule.
_UNSAFE_DEFAULTS: Mapping[str, str] = {
    "compute_thermochemistry": (
        "its `symmetry_number` defaults to 1, meaning no symmetry, which is wrong for any "
        "symmetric molecule and shifts the entropy and free energy by R·ln(sigma) with nothing in "
        "the result saying so"
    ),
}


@dataclass(frozen=True, slots=True)
class ToolContract:
    """What a tool accepts, in the terms this dispatcher judges.

    `accepted` / `required` / `takes_any_key` are `agent/template_surface.ToolArguments`' three
    terms, passed in rather than re-derived so there is still one definition of them. Building this
    from a live tool is the caller's job precisely because that is where the authority lives — a
    running session's advertised schema — and this module stays free of both the connector layer
    and the agent layer.

    `structure_type` is the JSON type the schema declares for `STRUCTURE_ARGUMENT`, empty when the
    schema does not say. It is the one fact beyond keys, for the reason in the module docstring.
    """

    required: frozenset[str] = frozenset()
    accepted: frozenset[str] = frozenset()
    structure_type: str = ""
    readable: bool = True


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a check could not be run, in a form a chemist can act on.

    `code` is a closed vocabulary so refusals can be counted and alerted on; `detail` names the
    specific thing that failed. Both ride out in `CheckOutcome.detail`, because a check identified
    as computable and then not computed is a fact the reader needs — silently dropping it back to
    prose is how the first version of this feature came to tell a chemist a calculation had run.
    """

    code: str
    detail: str

    def __str__(self) -> str:
        """The refusal as one line, for a note body or a summary."""
        return f"{self.detail} ({self.code})"


@dataclass(frozen=True, slots=True)
class Dispatch:
    """A call this module is willing to make: the tool, the structure, and what was left defaulted.

    `defaulted` exists to be disclosed. `predict_logd` has a `ph`, `compute_electronic_properties`
    a `solvent`; left alone each computation is well-defined, and a reader who does not know which
    knobs were left alone cannot tell whether the number answers their question.
    """

    tool: str
    smiles: str
    subject_note_id: str
    defaulted: tuple[str, ...] = field(default_factory=tuple)

    @property
    def arguments(self) -> dict[str, str]:
        """The call, which is the structure and nothing else."""
        return {STRUCTURE_ARGUMENT: self.smiles}


def refuse_unless_dispatchable(tool: str, contract: ToolContract | None) -> Refusal | None:
    """`None` if this tool can be called from a structure alone; a `Refusal` saying why not.

    Judges the tool's own contract rather than a maintained list, so the answer tracks the tool: one
    that later gains a second required argument drops out on its own instead of a hand-written
    allowlist going quietly out of date.
    """
    if contract is None:
        return Refusal(
            "tool-unavailable", f"{tool!r} is not on this deployment's connector surface"
        )
    if not contract.readable:
        return Refusal(
            "unreadable-contract",
            f"{tool!r} advertises no readable argument schema, so its call cannot be checked",
        )
    if unsafe := _UNSAFE_DEFAULTS.get(tool):
        return Refusal(
            "unsafe-defaults", f"{tool!r} is not dispatched automatically because {unsafe}"
        )
    # Order matters for the message, not for the verdict: a tool requiring only `query` is not a
    # structure tool, and saying it "*also* requires query" reads as nonsense. Ask what it is
    # before asking what else it wants.
    if STRUCTURE_ARGUMENT not in contract.required:
        return Refusal(
            "not-a-structure-tool",
            f"{tool!r} does not require a {STRUCTURE_ARGUMENT}, so it is not a question about one "
            "compound",
        )
    if extra := sorted(contract.required - {STRUCTURE_ARGUMENT}):
        return Refusal(
            "needs-more-than-a-structure",
            f"{tool!r} also requires {extra}, which nothing in the record supplies — a model "
            "filling those in would be inventing them",
        )
    if contract.structure_type and contract.structure_type != "string":
        return Refusal(
            "structure-is-not-a-string",
            f"{tool!r} declares {STRUCTURE_ARGUMENT} as {contract.structure_type!r} rather than a "
            "single SMILES string",
        )
    return None


def structure_of(note: Note | None, note_id: str) -> tuple[str, None] | tuple[None, Refusal]:
    """The SMILES a subject note carries, or a `Refusal` naming the gate it failed.

    Returns a pair rather than raising, because every refusal here is an ordinary outcome the
    tournament reports rather than an error that should lose the run — a model naming a note that
    does not exist is precisely the case this function is for, and it must cost that check rather
    than the ranking.

    Validated by `core.chem.require_canonical_smiles`, the same gate the calculation cache keys on,
    which rejects the whole class a bare RDKit parse accepts: embedded whitespace (`"CCO junk"`
    parses as ethanol), the empty string, and non-ASCII. A note carrying an unparseable structure
    refuses here rather than reaching a calculator that would either fail obscurely or compute.
    """
    from chemclaw.core.chem import InvalidSmilesError, require_canonical_smiles

    if note is None:
        return None, Refusal(
            "subject-not-found", f"{note_id!r} is not a note in this deployment's corpus"
        )
    if note.type != SUBJECT_NOTE_TYPE:
        return None, Refusal(
            "subject-not-a-compound",
            f"{note_id!r} is a {note.type!r} note; a calculation needs a {SUBJECT_NOTE_TYPE}",
        )
    if not note.compound_smiles:
        return None, Refusal(
            "subject-has-no-structure",
            f"{note_id!r} is a compound note with no `compound_smiles` to compute on",
        )
    try:
        return require_canonical_smiles(note.compound_smiles), None
    except InvalidSmilesError as exc:
        return None, Refusal(
            "subject-structure-unparseable",
            f"{note_id!r} carries a structure RDKit will not read: {exc}",
        )


def contract_of(schema: Mapping[str, Any] | None, accepts: Any) -> ToolContract:
    """Build a contract from a tool's advertised schema and its already-reduced key terms.

    `accepts` is an `agent/template_surface.ToolArguments` — taken as `Any` so this module does not
    import the agent layer for a shape it only reads three attributes of. The schema is consulted
    for one thing the keys reduction deliberately drops, the structure argument's declared type.
    """
    if accepts is None:
        return ToolContract(readable=False)
    properties = (schema or {}).get("properties")
    declared = ""
    if isinstance(properties, Mapping):
        entry = properties.get(STRUCTURE_ARGUMENT)
        if isinstance(entry, Mapping):
            raw = entry.get("type")
            declared = raw if isinstance(raw, str) else ""
    return ToolContract(
        required=frozenset(accepts.required),
        accepted=frozenset(accepts.accepted),
        structure_type=declared,
    )


def defaulted_arguments(contract: ToolContract) -> tuple[str, ...]:
    """Every argument the tool accepts and does not require, so the caller can disclose them."""
    return tuple(sorted(contract.accepted - contract.required))


# ---------------------------------------------------------------------------------------- jobs
#
# A durable job is a *better* grounded target than an endpoint tool, which is the opposite of what
# it looks like. A tool advertises a schema only while its server is up; a job declares a
# `params_model` and a `precondition` in its manifest, so both the shape of the call and the
# vocabulary of its values are checkable before anything runs. `compare_solvents` is the case worth
# holding in mind: `require_supported_solvents` refuses a solvent the method cannot model *before
# the job starts*, so a model-proposed solvent list is a selection from a validated vocabulary
# rather than an invention.
#
# **That is why a sweep is allowed here when a silent default is not.** The harm in an invented
# argument is that the assumption is invisible — a pKa computed in an unstated solvent reads as
# "the pKa". A swept axis is the opposite: it is the most visible part of the answer, every value
# is reported beside the result it produced, and nothing is claimed beyond `f(x)` for stated `x`.
# So the rule is not "never vary a parameter", it is **"vary nothing you cannot name in the
# output, and draw the values from a vocabulary something else validates"**.

#: Params fields that carry a structure, and how many. Semantic rather than derivable: `solvents`
#: and `reactants` are both `list[str]` and only one of them is a molecule, so the schema cannot
#: say which. Curated, but **fail-closed** — a required field that is not here, not the sweep and
#: not defaulted makes its job undispatchable, so a new job does not become automatically runnable
#: by omission. `tests/test_hypothesis_dispatch.py` holds every required field of every shipped
#: calc job to a classification.
STRUCTURE_FIELDS: Mapping[str, str] = {
    "smiles": "one",
    "smiles_a": "one",
    "smiles_b": "one",
    "reactants": "many",
    "products": "many",
    "species": "many",
}

#: Params fields that may be swept, mapped to the vocabulary that validates them. The value here is
#: only the *name* of the validating authority: the validation itself is the job's own declared
#: `precondition`, which runs at launch and is the thing that actually refuses. Naming it here is
#: what lets a refusal say which vocabulary a value failed, rather than "the job rejected it".
SWEEPABLE_FIELDS: Mapping[str, str] = {
    "solvents": "the solvents this calculator supports",
}


@dataclass(frozen=True, slots=True)
class Sweep:
    """An axis a check varies, and the values it varies over.

    Reported with the result rather than assumed behind it — see the block comment above. `values`
    is what the model proposed; whether each one is legal is the job's `precondition`'s answer, not
    this type's.
    """

    parameter: str = ""
    values: tuple[str, ...] = field(default_factory=tuple)


def ground_job_params(
    fields: Mapping[str, tuple[bool, Any]],
    subjects: Mapping[str, list[str]],
    structures: Mapping[str, str],
    sweep: Sweep | None,
    max_sweep_values: int,
) -> tuple[dict[str, Any], None] | tuple[None, Refusal]:
    """Assemble a job's params from resolved structures and a swept axis, or refuse.

    `fields` is the job's declared params model reduced to `name -> (required, default)`; the
    caller builds it from `model_fields` so this module imports no connector code. `structures`
    maps an already-resolved note id to its SMILES — resolution and its refusals are
    `structure_of`'s, done before this is called. `max_sweep_values` is passed in rather than read
    here because this module holds no settings import; it is the caller's budget, and refusing a
    wider axis is what keeps a sweep inside it.

    **Fail closed on anything it cannot account for.** A required field that is not a structure,
    not the swept axis and has no default is a value only a model could supply, and supplying it is
    the whole failure this module exists to prevent. `scan_coordinate` and `profile_rotation` land
    here: both need *atom indices*, which a model will produce plausibly and wrongly, and neither
    becomes dispatchable until something grounds them.

    **And every key it produces has to be one the job declares.** These models do not set
    `extra="forbid"`, so pydantic's default drops an unknown key on validation — measured, a
    `solvents` axis handed to `sample_conformers` vanished, the gas-phase search ran, and the
    grounded call still *reported* three solvents compared. A name being sweepable in the abstract
    says nothing about this job having that field, and the same held for a structure role the job
    does not take. Checking against `fields` is what makes the reported call the launched one.

    **A job with no subject at all is not a discriminating check.** Requiring one is what keeps the
    set to calculations *about a molecule in the record*: `republish_calculations` has no required
    field, so "every required field is grounded" was vacuously true for it and a model naming that
    string would have started a corpus-wide push to an external sink. A check that names no subject
    is not narrowly wrong — it is a different kind of act.
    """
    params: dict[str, Any] = {}
    if not subjects:
        return None, Refusal(
            "no-subject",
            "this job names no structure from the record, so it is not a check about a molecule",
        )
    for name, ids in subjects.items():
        arity = STRUCTURE_FIELDS.get(name)
        if arity is None:
            return None, Refusal(
                "subject-field-unknown",
                f"{name!r} is not a field that carries a structure, so note ids cannot fill it",
            )
        if name not in fields:
            return None, Refusal(
                "subject-field-not-declared",
                f"this job declares no {name!r} field, so the structures would be dropped on "
                "validation while the answer still reported them",
            )
        resolved = [structures[note_id] for note_id in ids if note_id in structures]
        if len(resolved) != len(ids):
            missing = sorted(set(ids) - set(structures))
            return None, Refusal("subject-not-found", f"unresolved subject(s): {missing}")
        if arity == "one":
            if len(resolved) != 1:
                return None, Refusal(
                    "subject-arity",
                    f"{name!r} takes one structure, {len(resolved)} given",
                )
            params[name] = resolved[0]
        else:
            if not resolved:
                return None, Refusal("subject-arity", f"{name!r} needs at least one structure")
            params[name] = resolved

    if sweep is not None and sweep.parameter:
        if sweep.parameter not in SWEEPABLE_FIELDS:
            return None, Refusal(
                "axis-not-sweepable",
                f"{sweep.parameter!r} is not an axis with a vocabulary to check values against",
            )
        if sweep.parameter not in fields:
            return None, Refusal(
                "axis-not-declared",
                f"this job declares no {sweep.parameter!r} field, so the axis would be dropped on "
                "validation and the answer would report a comparison that never ran",
            )
        if not sweep.values:
            return None, Refusal("axis-empty", f"{sweep.parameter!r} was swept over no values")
        if len(sweep.values) > max_sweep_values:
            return None, Refusal(
                "axis-too-wide",
                f"{sweep.parameter!r} was swept over {len(sweep.values)} values and the budget is "
                f"{max_sweep_values}; a narrower axis is a choice this check has to make, because "
                "dropping values here would make the axis reported beside the answer wrong",
            )
        params[sweep.parameter] = list(sweep.values)

    ungrounded = sorted(
        name for name, (required, _default) in fields.items() if required and name not in params
    )
    if ungrounded:
        return None, Refusal(
            "field-cannot-be-grounded",
            f"this job also requires {ungrounded}, and nothing in the record supplies them — "
            "a model filling them in would be inventing them",
        )
    return params, None


# ----------------------------------------------------------------------------------- templates
#
# **The third thing a check may name, and the one that answers a question about structures nobody
# wrote down.** A tool takes one note's molecule; a job takes several. Neither can ask about a
# molecule's *tautomers*, its protonation states or its breakable bonds, because none of those is
# a note in the corpus — and a model listing them would be inventing structures, which is the
# failure this module exists to prevent, in its worst form.
#
# A `Template` closes that, and it already existed. Five of the shipped ones are exactly an
# enumerator feeding a calculation (`enumerate_tautomers` -> `rank_species`,
# `enumerate_bond_cleavages` -> `survey_bond_strengths`, and three more), with the enumeration
# passed **by value** into the next step. The sibling fleet's own `SpeciesSet.smiles` says so:
# "the field Chemclaw3's templates pass straight into `rank_species`, by value".
#
# **So this is a third target, not a second chaining mechanism**, and the difference is not
# tidiness. A template carries defaults that were measured rather than chosen —
# `tautomer-resolution` pins `level: thorough` because acetylacetone ranks 99.9% keto from one
# embedding per tautomer and is ~80% *enol* in reality, the enol being stabilised by a hydrogen
# bond that exists in one planar conformer. A chain hand-rolled here would miss that and look
# entirely reasonable, which is this module's definition of the worst kind of wrong.
#
# What the model supplies is unchanged: a name, and a subject note. Everything else is the
# template's.

#: The one declared input a dispatchable template may require — the same name and for the same
#: reason as `STRUCTURE_ARGUMENT`. Every shipped template takes `smiles` plus optional extras, so a
#: template requiring anything else is one whose remaining input a model would have to invent.
TEMPLATE_STRUCTURE_INPUT = STRUCTURE_ARGUMENT


def ground_template_inputs(
    declared: Mapping[str, bool],
    structure: str,
    writes: bool,
) -> tuple[dict[str, Any], None] | tuple[None, Refusal]:
    """The inputs for a template run, or a `Refusal` naming what it would have needed invented.

    `declared` is the template's own `inputs` reduced to `name -> required`, built by the caller so
    this module imports no template code — the same arrangement the job half uses for
    `params_model`. `writes` says whether any of its `agent` steps declares `write_tools`.

    **Only the structure is supplied, and every optional input is left unset**, which is what makes
    the template's reviewed defaults the ones that apply. `template_job.TemplateWorkflow` seeds
    every declared name with `None` before substituting, so an omitted optional input resolves to
    gas phase rather than failing — that is the behaviour this relies on and it is deliberate
    there.

    **Fail closed twice.** A required input that is not the structure is refused, because nothing
    in the record supplies it. And a template whose agent step holds a write tool is refused
    outright: a discriminating check computes a number, and a procedure that also *acts* is not
    something a tournament may start on its own. No shipped template declares one today, and that
    is exactly why the guard belongs here — omission must not make the next one runnable.
    """
    if writes:
        return None, Refusal(
            "template-writes",
            "this template has a step holding a side-effecting tool, and a check may compute but "
            "may not act",
        )
    if TEMPLATE_STRUCTURE_INPUT not in declared:
        return None, Refusal(
            "template-takes-no-structure",
            f"this template declares no {TEMPLATE_STRUCTURE_INPUT!r} input, so a subject note has "
            "nothing to fill",
        )
    invented = sorted(
        name for name, required in declared.items() if required and name != TEMPLATE_STRUCTURE_INPUT
    )
    if invented:
        return None, Refusal(
            "template-needs-more-than-a-structure",
            f"this template also requires {invented}, and nothing in the record supplies them — "
            "a model filling them in would be inventing them",
        )
    return {TEMPLATE_STRUCTURE_INPUT: structure}, None


def defaulted_inputs(declared: Mapping[str, bool]) -> tuple[str, ...]:
    """Every declared input left unset, so the run can disclose what defaulted.

    The template half's `defaulted_arguments`. A `solvent` left out is a gas-phase answer, which is
    a different question from the same calculation in water and is not visible in the number.
    """
    return tuple(sorted(name for name in declared if name != TEMPLATE_STRUCTURE_INPUT))
