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
    takes_any_key: bool = False
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
    if not tool:
        return Refusal("no-tool-named", "the check named no tool to run")
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
        takes_any_key=bool(accepts.takes_any_key),
        structure_type=declared,
    )


def defaulted_arguments(contract: ToolContract) -> tuple[str, ...]:
    """Every argument the tool accepts and does not require, so the caller can disclose them."""
    return tuple(sorted(contract.accepted - contract.required))
