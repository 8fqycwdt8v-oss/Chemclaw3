"""What an xTB job may be asked to do — the request half of this bundle's durable contract.

**A leaf module, and that is the whole point.** These models are named by `connector.yaml`'s
`params_model`, and `connectors/jobs.py` resolves that name by *importing* it — on every
`build_langgraph_agent`, in the chat service's own process, and again in `make connector-validate`.
So whatever this module imports, the chat service imports too.

They used to sit beside the *result* types, which named four science modules and, through them,
`tblite` — the compiled quantum-chemistry library. Measured on `main` at the time, building the
enabled job tools pulled **`tblite` and fifteen science modules** into the agent's process: the
entire quantum-chemistry closure this bundle exists to keep out of it (D-114), let back in through
the one manifest field that resolves an import. Nothing failed; the chat pod simply carried a
compiled QM library it never called.

So the split here is not stylistic. Requests live in this module and import pydantic and config
only; results live in `connectors/calc/results.py`. That weight is gone —
`D-2026-08-16-the-physics-leaves-the-cache-stays` took the engines out of this repository entirely
— and the split stays anyway, because these shapes are pinned by workflow histories in flight and
because a boundary that only holds while nothing heavy exists is not a boundary.
`cli/validate_connectors.py` enforces it rather than trusting it, and
`tests/test_connector_isolation.py` asserts it in a fresh interpreter (D-118).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# What a caller has to be told about `symmetry_numbers` to supply it correctly, written once
# because the two reaction-shaped specs advertise the identical contract. It is the *only* field
# here carrying a description, and deliberately so: every other one is self-evident from its name
# and type, while this one is a physical quantity whose omission silently costs the free energy.
# A plain `dict[str, int]` keeps this module leaf — no `chemclaw.science.*` import is needed to
# state it (D-118).
_SYMMETRY_NUMBERS_DESCRIPTION = (
    "Rotational symmetry number per species, keyed by the exact SMILES string given in "
    "reactants/products: 1 for a molecule with no rotational symmetry, 2 for H2/N2/O2/CO2/water, "
    "3 for ammonia, 6 for ethane, 12 for benzene. Above level='quick', a species left out of "
    "this map has its entropy computed at sigma=1 and the job reports no free energy at all "
    "rather than one too high by R*ln(sigma); the electronic energy and enthalpy do not depend "
    "on it and are reported either way. Stating 1 is a real statement and does yield a free "
    "energy — 'no rotational symmetry' and 'not considered' are different claims."
)


class ReactionJobSpec(BaseModel):
    """A durable reaction-energy request (xTB plan X4)."""

    kind: Literal["reaction"] = "reaction"
    reactants: list[str] = Field(min_length=1)
    products: list[str] = Field(min_length=1)
    solvent: str | None = None
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"
    symmetry_numbers: dict[str, int] | None = Field(
        default=None, description=_SYMMETRY_NUMBERS_DESCRIPTION
    )


class SolventScreenJobSpec(BaseModel):
    """A durable solvent-comparison request over one reaction (xTB plan X4)."""

    kind: Literal["solvents"] = "solvents"
    reactants: list[str] = Field(min_length=1)
    products: list[str] = Field(min_length=1)
    solvents: list[str] = Field(min_length=1)
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"
    # The same species appear in every solvent, so one map covers the whole screen.
    symmetry_numbers: dict[str, int] | None = Field(
        default=None, description=_SYMMETRY_NUMBERS_DESCRIPTION
    )


class ScanJobSpec(BaseModel):
    """A durable relaxed-scan request along one internal coordinate (xTB plan X3)."""

    kind: Literal["scan"] = "scan"
    smiles: str = Field(min_length=1)
    atoms: list[int] = Field(min_length=2, max_length=4)
    values: list[float] = Field(min_length=2)
    solvent: str | None = None


class EnsembleJobSpec(BaseModel):
    """A durable conformer/tautomer/protomer search request (xTB plan X6)."""

    kind: Literal["ensemble"] = "ensemble"
    smiles: str = Field(min_length=1)
    search: Literal["conformers", "tautomers", "protomers", "deprotomers"] = "conformers"
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"


class ComplexJobSpec(BaseModel):
    """A durable non-covalent complex search over two molecules (xTB plan X11)."""

    kind: Literal["complex"] = "complex"
    smiles_a: str = Field(min_length=1)
    smiles_b: str = Field(min_length=1)
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"


# What an xTB job may be asked to do, discriminated on `kind`. A closed, typed union
# rather than a free-form request is the same boundary rule the proposal sets for the
# expert escape hatch: a model-authored payload can select among calculations we
# defined, and can never describe one we did not.
XtbJobSpec = Annotated[
    ReactionJobSpec | SolventScreenJobSpec | ScanJobSpec | EnsembleJobSpec | ComplexJobSpec,
    Field(discriminator="kind"),
]
