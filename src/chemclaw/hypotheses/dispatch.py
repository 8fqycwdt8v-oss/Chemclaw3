"""Deciding whether a discriminating check can be run without inventing anything.

**Nothing a model writes becomes a tool argument.** A fabricated argument produces a real number a
chemist reads as computed, so the model may only **select**:

- a **tool**, which must pass `refuse_unless_dispatchable`; and
- a **subject**, a note id that must resolve in this deployment's corpus to a `compound` note
  whose `compound_smiles` parses.

The structure is read off the resolved note, and the note comes from the deployment's knowledge
tree, never from anything travelling with the check.

Which keys a tool accepts comes from the caller as `agent/template_surface.ToolArguments`' terms
(`accepted`, `required`, `takes_any_key`); this module adds only the structure argument's declared
type (e.g. `screen_hazards` takes a list of SMILES). Signature checks are not sufficient: a tool
whose default is wrong for the molecule (e.g. `compute_thermochemistry`'s `symmetry_number=1`)
yields a silently wrong number, so `_UNSAFE_DEFAULTS` lists those, held to real tools by
`tests/test_hypothesis_dispatch.py`. An unreadable contract refuses: there is no later gate, and a
calculator given unexpected input computes something plausible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from chemclaw.kg.note import Note

#: The one argument name a dispatchable tool may require. A whitelist of one: a tool naming its
#: structure argument differently refuses.
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

    `accepted` / `required` / `takes_any_key` are `agent/template_surface.ToolArguments`' terms,
    built by the caller from a running session's schema, so this module imports neither the
    connector nor the agent layer. `structure_type` is the JSON type declared for
    `STRUCTURE_ARGUMENT`, empty when unstated.
    """

    required: frozenset[str] = frozenset()
    accepted: frozenset[str] = frozenset()
    structure_type: str = ""
    readable: bool = True


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a check could not be run, in a form a chemist can act on.

    `code` is a closed vocabulary for counting and alerting; `detail` names what failed. Both go
    into `CheckOutcome.detail`, so a computable check that was not computed is never silently
    reported as prose.
    """

    code: str
    detail: str

    def __str__(self) -> str:
        """The refusal as one line, for a note body or a summary."""
        return f"{self.detail} ({self.code})"


@dataclass(frozen=True, slots=True)
class Dispatch:
    """A call this module is willing to make: the tool, the structure, and what was left defaulted.

    `defaulted` is disclosed (e.g. `predict_logd`'s `ph`) so a reader knows which knobs the number
    assumed.
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

    Judged from the tool's own contract, so a tool that later gains a required argument drops out on
    its own.
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
    # Ask whether it is a structure tool before what else it requires, so the message makes sense.
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

    A pair rather than a raise: a model naming a missing note costs that check, not the ranking.
    Validated by `core.chem.require_canonical_smiles`, the gate the calculation cache keys on, which
    rejects what a bare RDKit parse accepts (embedded whitespace, empty, non-ASCII).
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

    `accepts` is an `agent/template_surface.ToolArguments`, typed `Any` to avoid importing the agent
    layer. The schema supplies only the structure argument's declared type.
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
# A durable job declares a `params_model` and a `precondition`, so both the call's shape and its
# values' vocabulary are checkable before anything runs (e.g. `require_supported_solvents` refuses
# an unmodelled solvent before the job starts).
#
# A sweep is allowed where a silent default is not: a swept axis is reported beside every result, so
# nothing is assumed invisibly. The rule: vary nothing you cannot name in the output, and draw
# values from a vocabulary something else validates.

#: Params fields that carry a structure, and how many. Curated because the schema cannot tell a
#: molecule list from another `list[str]`, but fail-closed: an unclassified required field makes the
#: job undispatchable. `tests/test_hypothesis_dispatch.py` classifies every required field of every
#: shipped calc job.
STRUCTURE_FIELDS: Mapping[str, str] = {
    "smiles": "one",
    "smiles_a": "one",
    "smiles_b": "one",
    "reactants": "many",
    "products": "many",
    "species": "many",
}

#: Params fields that may be swept, mapped to the vocabulary that validates them. The job's own
#: `precondition` does the validating at launch; the name here lets a refusal say which vocabulary
#: failed.
SWEEPABLE_FIELDS: Mapping[str, str] = {
    "solvents": "the solvents this calculator supports",
}


@dataclass(frozen=True, slots=True)
class Sweep:
    """An axis a check varies, and the values it varies over.

    Reported with the result. `values` is what the model proposed; legality is the job's
    `precondition`'s answer.
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

    `fields` is the params model reduced to `name -> (required, default)`; `structures` maps
    resolved note ids to SMILES (resolution is `structure_of`'s); `max_sweep_values` is the caller's
    budget. Built by the caller so this module imports no connector code or settings.

    Fails closed: a required field that is not a structure, not the swept axis and has no default
    would need a model-supplied value (e.g. atom indices for `scan_coordinate`). Every produced key
    must be one the job declares, since these models drop unknown keys silently and the reported
    call must be the launched one. A job with no subject field is refused: a check must be about a
    molecule in the record.
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
# A template is the third target: it can ask about structures not in the corpus (tautomers,
# protonation states, bond cleavages) by passing an enumerator's output by value into a calculation,
# without the model inventing structures. Its reviewed defaults also apply (e.g.
# `tautomer-resolution` pins `level: thorough`), which a hand-rolled chain here would miss. The
# model still supplies only a name and a subject note.

#: The one declared input a dispatchable template may require, as for `STRUCTURE_ARGUMENT`; any
#: other required input would have to be invented.
TEMPLATE_STRUCTURE_INPUT = STRUCTURE_ARGUMENT


def ground_template_inputs(
    declared: Mapping[str, bool],
    structure: str,
    writes: bool,
    computes: bool,
) -> tuple[dict[str, Any], None] | tuple[None, Refusal]:
    """The inputs for a template run, or a `Refusal` naming what it would have needed invented.

    `declared` is the template's `inputs` reduced to `name -> required`, built by the caller;
    `writes` says whether any `agent` step declares `write_tools`. Only the structure is supplied,
    so the template's reviewed defaults apply (`TemplateWorkflow` seeds omitted inputs with `None`,
    e.g. gas phase).

    Fails closed three ways: a required input other than the structure; a template that writes (a
    check is an observation); and a template that runs no durable job, whose output would be model
    prose read as a computed result.
    """
    if not computes:
        return None, Refusal(
            "template-computes-nothing",
            "this template runs no calculation, so it would answer the check with prose rather "
            "than a number",
        )
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

    The template counterpart of `defaulted_arguments`: an omitted `solvent` is a gas-phase answer,
    invisible in the number.
    """
    return tuple(sorted(name for name in declared if name != TEMPLATE_STRUCTURE_INPUT))
