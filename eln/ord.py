"""The stable, ELN-agnostic reaction target schema (plan step 4.1).

An **ORD-inspired** pydantic subset — the canonical shape every layer above the ELN
integration knows (graph notes, fingerprint search, metrics). It is deliberately a subset
of the full Open Reaction Database proto: only the fields Chemclaw actually consumes
(structure, roles, amounts, the headline conditions and yield, provenance), so there is no
speculative schema. An ELN adapter maps its own format *into* this; nothing here knows any
ELN's quirks (G6).
"""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Role(StrEnum):
    """A component's role in the reaction (a subset of ORD's reaction roles)."""

    REACTANT = "reactant"
    REAGENT = "reagent"
    SOLVENT = "solvent"
    CATALYST = "catalyst"
    PRODUCT = "product"


class Component(BaseModel):
    """One chemical species in a reaction: its structure, role, and optional amount."""

    smiles: str = Field(min_length=1)
    role: Role
    # Amounts are optional (an ELN may omit them); mass drives the mass-balance and
    # green-chemistry checks, so it is kept in milligrams when known.
    amount_mmol: float | None = Field(default=None, ge=0.0)
    mass_mg: float | None = Field(default=None, ge=0.0)


class OrdReaction(BaseModel):
    """A canonical reaction record: inputs, outcomes, headline conditions, provenance.

    `reaction_id` is the ELN's stable entry id (carried for idempotency and provenance).
    Inputs carry every non-product species (reactant/reagent/solvent/catalyst); outcomes
    are the products. Conditions are the few an ELN reliably records; richer setup is out
    of this subset until a consumer needs it.
    """

    reaction_id: str = Field(min_length=1)
    inputs: list[Component] = Field(min_length=1)
    outcomes: list[Component] = Field(min_length=1)
    temperature_c: float | None = None
    time_h: float | None = Field(default=None, ge=0.0)
    yield_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    provenance: str = Field(min_length=1)
    # The project/campaign this experiment belongs to — the grouping key for the semantic
    # memory layer (a playbook distils patterns that recur across >=2 projects, plan 5.4).
    project: str | None = None

    @model_validator(mode="after")
    def _roles_are_consistent(self) -> "OrdReaction":
        """Inputs must not be products, and outcomes must all be products (G4)."""
        if any(c.role == Role.PRODUCT for c in self.inputs):
            raise ValueError("an input component has role 'product'")
        if any(c.role != Role.PRODUCT for c in self.outcomes):
            raise ValueError("an outcome component is not a product")
        return self

    def reaction_smiles(self) -> str:
        """Build the reaction SMILES (`inputs>>products`) for DRFP fingerprinting.

        All inputs (reactants, reagents, solvent, catalyst) go on the left, products on
        the right — the whole-reaction form DRFP expects.
        """
        left = ".".join(c.smiles for c in self.inputs)
        right = ".".join(c.smiles for c in self.outcomes)
        return f"{left}>>{right}"

    def compounds(self) -> list[Component]:
        """Every distinct component (inputs + outcomes), for per-compound indexing."""
        return [*self.inputs, *self.outcomes]
