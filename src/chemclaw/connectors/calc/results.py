"""What an xTB job returns: the result half of this bundle's durable contract.

Kept apart from `connectors/calc/specs.py` so the request side, which the chat service imports to
resolve `params_model` (D-118), stays a leaf. These shapes are pinned by workflow histories in
flight; `cli/validate_connectors.py` and `tests/test_connector_isolation.py` enforce the split.
"""

from pydantic import BaseModel, Field

from chemclaw.science.calc.models import (
    BondDissociationSurvey,
    ConformerEnsemble,
    EnsembleProperty,
    InteractionResult,
    MicrostatePka,
    ReactionEnergyResult,
    RefinedEnsemble,
    RotationProfile,
    ScanResult,
    SolventComparisonResult,
    SpeciesDistribution,
    SpeciesSolventComparison,
)


class XtbJobResult(BaseModel):
    """The outcome of a durable xTB job: exactly one of the result shapes.

    Optional fields rather than a union, because each result model is a rich domain type with no
    field in common to discriminate on, and a wrong smart-union match would be a silent data
    corruption rather than a loud error.
    """

    kind: str
    summary: str
    # The calculation keys this job reached, for the envelope to carry and a note to cite. Additive
    # and defaulted (as are the members below): this crosses the Temporal wire, so a result decoded
    # from an older history simply has none.
    calc_refs: list[str] = Field(default_factory=list)
    reaction: ReactionEnergyResult | None = None
    solvents: SolventComparisonResult | None = None
    scan: ScanResult | None = None
    # The rotational profile.
    rotation: RotationProfile | None = None
    ensemble: ConformerEnsemble | None = None
    interaction: InteractionResult | None = None
    pka: MicrostatePka | None = None
    # The multi-step results.
    refined: RefinedEnsemble | None = None
    averaged: EnsembleProperty | None = None
    distribution: SpeciesDistribution | None = None
    # The distribution fanned out over media.
    species_solvents: SpeciesSolventComparison | None = None
    bonds: BondDissociationSurvey | None = None

    def outcome(self) -> BaseModel:
        """The one result shape this job actually produced.

        The envelope's class name is always `XtbJobResult`, so consumers that want the science (e.g.
        `chemclaw.publish`) must ask this instead. Members are recognised by type (a `BaseModel`;
        the envelope's own fields are not), so a new result shape is one field and nothing else.
        Pure, because `CalcJobWorkflow` calls it in workflow code that must replay
        deterministically.

        Raises:
            ValueError: if the envelope carries no member or more than one.
        """
        members = [value for _, value in self if isinstance(value, BaseModel)]
        if len(members) != 1:
            raise ValueError(
                f"an xTB job envelope carries exactly one result; {self.kind!r} carried "
                f"{len(members)}. This is a dispatch bug in `run_xtb_calculation`, not bad input."
            )
        return members[0]
