"""What an xTB job returns — the result half of this bundle's durable contract.

Separate from `connectors/calc/specs.py` because of what it imports. A result is a rich domain
type (`ReactionEnergyResult`, `ConformerEnsemble`, …) and naming those pulls in `calc.reaction`,
`calc.conformers`, `calc.complexes` and `calc.xtb_scan` — and through them `tblite`, the compiled
quantum-chemistry library.

That is fine here and only here: nothing outside this bundle's own worker imports this module. The
*request* side cannot afford it, because `connector.yaml` names those models in `params_model` and
`connectors/jobs.py` resolves that name by importing it inside the chat service (D-118).
"""

from pydantic import BaseModel

from calc.complexes import InteractionResult
from calc.conformers import ConformerEnsemble
from calc.reaction import ReactionEnergyResult, SolventComparisonResult
from calc.xtb_scan import ScanResult


class XtbJobResult(BaseModel):
    """The outcome of a durable xTB job: exactly one of the result shapes.

    Optional fields rather than a union, because each result model is a rich domain type with no
    field in common to discriminate on, and a wrong smart-union match would be a silent data
    corruption rather than a loud error.
    """

    kind: str
    summary: str
    reaction: ReactionEnergyResult | None = None
    solvents: SolventComparisonResult | None = None
    scan: ScanResult | None = None
    ensemble: ConformerEnsemble | None = None
    interaction: InteractionResult | None = None
