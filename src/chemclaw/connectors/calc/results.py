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
    ConformerEnsemble,
    InteractionResult,
    ReactionEnergyResult,
    ScanResult,
    SolventComparisonResult,
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
    ensemble: ConformerEnsemble | None = None
    interaction: InteractionResult | None = None
