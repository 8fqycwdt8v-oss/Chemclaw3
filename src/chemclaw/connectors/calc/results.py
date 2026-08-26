"""What an xTB job returns — the result half of this bundle's durable contract.

Separate from `connectors/calc/specs.py` because of what it used to import: a result is a rich
domain type (`ReactionEnergyResult`, `ConformerEnsemble`, …) and naming those pulled in four
`science.calc` engine modules and, through them, `tblite` — the compiled quantum-chemistry library.
That was fine here and only here, because nothing outside this bundle's own worker imports this
module, while the *request* side cannot afford it: `connector.yaml` names those models in
`params_model` and `connectors/jobs.py` resolves that name by importing it inside the chat service
(D-118).

The weight is gone — `D-2026-08-16-the-physics-leaves-the-cache-stays` took the engines out of this
repository entirely, so `science/calc/models.py` is pydantic, numpy and RDKit and nothing compiled.
The split stays anyway, and not out of caution: these five shapes are pinned by workflow histories
already in flight, so this module is a *contract* boundary rather than an import-weight one, and
`cli/validate_connectors.py` plus `tests/test_connector_isolation.py` still enforce that the request
side stays leaf.
"""

from pydantic import BaseModel, Field

from chemclaw.science.calc.models import (
    BondDissociationSurvey,
    ConformerEnsemble,
    EnsembleProperty,
    InteractionResult,
    ReactionEnergyResult,
    RefinedEnsemble,
    RotationProfile,
    ScanResult,
    SolventComparisonResult,
    SpeciesDistribution,
)


class XtbJobResult(BaseModel):
    """The outcome of a durable xTB job: exactly one of the result shapes.

    Optional fields rather than a union, because each result model is a rich domain type with no
    field in common to discriminate on, and a wrong smart-union match would be a silent data
    corruption rather than a loud error.
    """

    kind: str
    summary: str
    # The calculation keys this job reached, for the envelope to carry and a note to cite
    # (D-2026-08-21). Additive and defaulted, because this crosses the Temporal wire and histories
    # are in flight; a result decoded from an older history simply has none.
    calc_refs: list[str] = Field(default_factory=list)
    reaction: ReactionEnergyResult | None = None
    solvents: SolventComparisonResult | None = None
    scan: ScanResult | None = None
    # The rotational profile (D-2026-08-26-a-torsion-is-named-not-indexed). Additive and defaulted
    # like the four below, and for the same reason: this crosses the Temporal wire and histories are
    # in flight, so a result decoded from an older one simply has none.
    rotation: RotationProfile | None = None
    ensemble: ConformerEnsemble | None = None
    interaction: InteractionResult | None = None
    # The four multi-step results. Additive and defaulted like `calc_refs` above and for the same
    # reason: this crosses the Temporal wire and histories are in flight, so a result decoded from
    # an older one simply has none of them.
    refined: RefinedEnsemble | None = None
    averaged: EnsembleProperty | None = None
    distribution: SpeciesDistribution | None = None
    bonds: BondDissociationSurvey | None = None

    def outcome(self) -> BaseModel:
        """The one result shape this job actually produced.

        **Why this exists.** This class is bookkeeping around a domain result: `kind` and `summary`
        say which member is set and how to read it in one line, and every consumer that wants the
        science wants the member. `type(envelope).__name__` is therefore never the answer to "what
        shape is this" — it is always `XtbJobResult` — and answering it that way is what left the
        whole composite half of `chemclaw.publish` dropping its input with a debug line while every
        test passed (`D-2026-08-25-a-cache-is-not-a-record`'s headline claim). `qm` has no wrapper
        and is the one bundle that published; this is how `calc` stops being the exception.

        **Members are recognised by type, not by a name list.** A member is a `BaseModel`; the
        envelope's own three fields are a `str`, a `str` and a `list[str]`. So a tenth result shape
        is one field on this class and nothing else — no list here to forget to extend, which is
        the failure mode this whole change is about.

        Pure, and it has to be: `CalcJobWorkflow` calls it in workflow code, where a replay must
        produce byte-identical output from an activity result already in history.

        Raises:
            ValueError: if the envelope carries no member or more than one. A job that produced no
                result must not report success, and `kind` would be describing something absent.
        """
        members = [value for _, value in self if isinstance(value, BaseModel)]
        if len(members) != 1:
            raise ValueError(
                f"an xTB job envelope carries exactly one result; {self.kind!r} carried "
                f"{len(members)}. This is a dispatch bug in `run_xtb_calculation`, not bad input."
            )
        return members[0]
