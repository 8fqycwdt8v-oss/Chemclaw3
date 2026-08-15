"""The shape of every payload the calculation store holds, pinned against `CALCULATION_EPOCH`.

**The defect this exists for.** `SolubilityResult` gained an `estimate` field carrying the
applicability-domain flag, and nothing about the key moved: the ESOL arithmetic was untouched, so
`calc_version` was *correctly* unchanged. The field is optional, so every row already on disk came
back validating cleanly with `estimate=None` — and a salt that the new code calls "OUT OF DOMAIN"
was served as "not assessed" instead. `durable/retention.py` deliberately never prunes
`calculation_results`, so those rows never self-heal.

**Why a snapshot and not a rule.** The rule ("bump the epoch when a stored payload's meaning
changes") already existed in spirit for `calc_version` and was still missed, because the change
that breaks a cached payload does not look like a cache change while you are making it — it looks
like adding a field. A digest per persisted model turns that into a failing test on the very commit
that adds the field, with the two available answers named in the failure message.

**What the digest covers, and what it deliberately does not.** It is taken over the model's JSON
schema with `title`/`description` stripped, so it moves for a field added, removed, renamed or
retyped — anywhere in the model, including inside a nested model such as `Estimate` — and does not
move for a reworded docstring. Prose is stripped only where a JSON-Schema keyword is expected: the
keys inside `properties`/`$defs` are the model's own field names, and a model with a field
literally named `title` must not be fingerprinted as one without it.

The one thing a digest cannot see is our arithmetic being wrong and then fixed — the payload's
shape does not move when a corrected linear-rotor term changes every entropy in it. That half stays
a judgement, which is why `CALCULATION_EPOCH` is a hand-bumped constant rather than a derived one.
"""

from typing import Any

from pydantic import BaseModel

from chemclaw.connectors.qm.specs import QMJobResult
from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.complexes import InteractionResult
from chemclaw.science.calc.conformers import ConformerEnsemble
from chemclaw.science.calc.descriptors import DescriptorProfile
from chemclaw.science.calc.geometry import BestGeometry
from chemclaw.science.calc.pka import PkaResult
from chemclaw.science.calc.solubility import SolubilityResult
from chemclaw.science.calc.uncertainty import Estimate
from chemclaw.science.calc.xtb import XtbResult
from chemclaw.science.calc.xtb_hessian import HessianResult
from chemclaw.science.calc.xtb_opt import OptimizationResult
from chemclaw.science.calc.xtb_props import ElectronicProperties, SiteReactivityResult
from chemclaw.science.calc.xtb_scan import ScanResult
from chemclaw.science.calc.xtb_thermo import ThermochemistryResult

# JSON-Schema keywords whose *keys* are names the model chose rather than schema vocabulary. The
# prose filter must not be applied inside them — see the module docstring.
_NAME_MAPS = frozenset({"properties", "$defs", "patternProperties"})
# Prose, not structure. Rewording a docstring must never invalidate a cache.
_PROSE = frozenset({"title", "description"})


def _shape_only(node: Any, *, in_name_map: bool = False) -> Any:
    """`node` with every prose annotation removed, so only its structure remains."""
    if isinstance(node, dict):
        return {
            key: _shape_only(value, in_name_map=not in_name_map and key in _NAME_MAPS)
            for key, value in node.items()
            if in_name_map or key not in _PROSE
        }
    if isinstance(node, list):
        return [_shape_only(item) for item in node]
    return node


def shape_digest(model: type[BaseModel]) -> str:
    """A digest of what `model` persists, stable against prose and sensitive to structure."""
    return stable_hash(_shape_only(model.model_json_schema()))


# Every model whose instances are written into `calculation_results` — the payloads a cache hit
# validates back. A new cached calculator belongs here; one that is missing is simply unguarded,
# which is the state the whole file exists to leave behind.
PAYLOAD_MODELS: tuple[type[BaseModel], ...] = (
    BestGeometry,
    ConformerEnsemble,
    DescriptorProfile,
    ElectronicProperties,
    HessianResult,
    InteractionResult,
    OptimizationResult,
    PkaResult,
    QMJobResult,
    ScanResult,
    SiteReactivityResult,
    SolubilityResult,
    ThermochemistryResult,
    XtbResult,
)

# The recorded shape of each. Updating an entry is half of the answer to a failure here; the other
# half is deciding whether rows already written are now wrong or incomplete, and bumping
# `calc.store.CALCULATION_EPOCH` if they are.
RECORDED_SHAPES: dict[str, str] = {
    "BestGeometry": "36145271404b5c5b",
    "ConformerEnsemble": "defae7d5ca39e8d9",
    "DescriptorProfile": "81370985b8bb84c0",
    "ElectronicProperties": "61f42bbba754c322",
    "HessianResult": "a1703112a0866c95",
    "InteractionResult": "bb0523e0297f8451",
    "OptimizationResult": "3d934a3b36e47f11",
    "PkaResult": "f4928a91c06fc746",
    "QMJobResult": "fce36419000e7f0d",
    "ScanResult": "72609a59a86db985",
    "SiteReactivityResult": "0378ea844edafd37",
    # Changed when `Estimate.method` dropped its unreachable `"conformal"` member: nothing ever
    # produced that value (the function behind it had no caller and was deleted), so every row on
    # disk carries `"reported"` and still validates and still means what it said. A narrowing that
    # removes a value nothing wrote is the one shape change that needs no epoch bump.
    "SolubilityResult": "9c81f577df57caed",
    "ThermochemistryResult": "c60a948c32b35abd",
    "XtbResult": "cc278ccf4b7832db",
}


def test_every_payload_model_is_recorded() -> None:
    """The snapshot and the model list describe the same set — a stray entry guards nothing."""
    assert {model.__name__ for model in PAYLOAD_MODELS} == set(RECORDED_SHAPES)


def test_persisted_payload_shapes_have_not_changed() -> None:
    """A payload model changed shape: decide what that does to the rows already on disk.

    This is not a request to keep the models still. It is the moment to answer one question — can a
    row written before this change still be read as what it claims to be? An added optional field
    usually means no: it validates back as `None`, which reads as "we do not know" when the truth is
    "we never asked", exactly as `SolubilityResult.estimate` did.
    """
    current = {model.__name__: shape_digest(model) for model in PAYLOAD_MODELS}
    changed = {
        name: digest for name, digest in current.items() if RECORDED_SHAPES.get(name) != digest
    }
    assert not changed, (
        f"persisted payload shape(s) changed: {sorted(changed)}.\n"
        "If rows already in `calculation_results` are now wrong or incomplete, bump "
        "`chemclaw.science.calc.store.CALCULATION_EPOCH` (and log the reason beside it).\n"
        "Then record the new digest(s) in RECORDED_SHAPES: "
        + ", ".join(f'"{name}": "{digest}"' for name, digest in sorted(changed.items()))
    )


def test_the_digest_notices_an_added_optional_field() -> None:
    """The exact change that slipped through: an optional field, appended, defaulting to None."""

    class Before(BaseModel):
        log_s_mol_per_l: float

    class After(BaseModel):
        log_s_mol_per_l: float
        estimate: Estimate | None = None

    assert shape_digest(Before) != shape_digest(After)


def test_the_digest_notices_a_field_added_to_a_nested_model() -> None:
    """Nesting is not a hiding place: `Estimate` is where the domain flag actually lives."""

    class Inner(BaseModel):
        value: float

    class WiderInner(BaseModel):
        value: float
        in_domain: bool | None = None

    class Outer(BaseModel):
        estimate: Inner

    class WiderOuter(BaseModel):
        estimate: WiderInner

    assert shape_digest(Outer) != shape_digest(WiderOuter)


def test_the_digest_ignores_reworded_prose() -> None:
    """A docstring rewrite must not invalidate a cache — the digest tracks shape, not wording."""

    class Terse(BaseModel):
        """A number."""

        value: float

    class Verbose(BaseModel):
        """A number, at considerable length, with every nuance of its meaning spelled out."""

        value: float

    assert shape_digest(Terse) == shape_digest(Verbose)


def test_a_field_named_title_is_not_mistaken_for_prose() -> None:
    """The prose filter stops at field names, or a model could hide a field by naming it `title`."""

    class Plain(BaseModel):
        value: float

    class Titled(BaseModel):
        value: float
        title: str = ""

    assert shape_digest(Plain) != shape_digest(Titled)
