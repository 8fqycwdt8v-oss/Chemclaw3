"""The shape of every payload this repository reads back out of the calculation store.

Stored rows are never pruned, so a payload model change can silently misread old rows (an added
optional field validates as `None`, "not assessed", where the truth is "never asked"). A digest per
model fails on the commit that changes the shape, with the two answers named.

The digest is over the JSON schema with `title`/`description` stripped (only where a schema keyword
is expected, not a field named `title`), so it moves for any field change, including nested
models, and not for reworded docs. It cannot see a fixed arithmetic error; that stays a judgement,
hence the hand-bumped `CALCULATION_EPOCH`.

The calculation server writes these payloads, so the digests guard the reader half of a
cross-repository contract. Composed results (`ConformerEnsemble`, `ScanResult`,
`InteractionResult`, `ThermochemistryResult`) are Temporal wire types, not cache rows, and are not
listed.
"""

from typing import Any

from pydantic import BaseModel

from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.models import (
    AtomicDescriptorResult,
    DescriptorProfile,
    ElectronicProperties,
    EnsemblePayload,
    HessianPayload,
    OptimizationResult,
    PkaResult,
    SiteReactivityResult,
    SolubilityResult,
    SurfacePotentialResult,
    XtbResult,
)
from chemclaw.science.calc.uncertainty import Estimate

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


# Every model this repository validates a `calculation_results` row back into. A new cached
# calculator belongs here; one that is missing is simply unguarded, which is the state the whole
# file exists to leave behind.
PAYLOAD_MODELS: tuple[type[BaseModel], ...] = (
    DescriptorProfile,
    AtomicDescriptorResult,
    ElectronicProperties,
    EnsemblePayload,
    HessianPayload,
    OptimizationResult,
    PkaResult,
    SiteReactivityResult,
    SurfacePotentialResult,
    SolubilityResult,
    XtbResult,
)

# The recorded shape of each. Updating an entry is half of the answer to a failure here; the other
# half is deciding whether rows already written are now wrong or incomplete, and bumping
# `calc.store.CALCULATION_EPOCH` if they are.
RECORDED_SHAPES: dict[str, str] = {
    "DescriptorProfile": "81370985b8bb84c0",
    "AtomicDescriptorResult": "152cad7e5280aee5",
    "SurfacePotentialResult": "4e94bc470fa52bfe",
    "ElectronicProperties": "5c549d172443ea4c",
    # `EnsembleMember.degeneracy` gained `ge=1`: recorded, not epoch-bumped, because it tightens
    # validation only and a stored degeneracy is always >= 1.
    "EnsemblePayload": "4afdce1baac44be8",
    # The wire shape a `compute_hessian` row holds. `max_gradient_hartree_per_angstrom` and
    # `ir_wavenumbers_cm` are optional and additive, so older rows still validate and mean what they
    # said (stationarity unassessed; bands paired by count): recorded, not epoch-bumped.
    "HessianPayload": "e9833906f62fa505",
    "OptimizationResult": "3d934a3b36e47f11",
    "PkaResult": "f4928a91c06fc746",
    "SiteReactivityResult": "ddeb1c374840d99f",
    # `Estimate.method` dropped a value nothing ever wrote, so no epoch bump was needed.
    "SolubilityResult": "9c81f577df57caed",
    "XtbResult": "cc278ccf4b7832db",
}


def test_every_payload_model_is_recorded() -> None:
    """The snapshot and the model list describe the same set — a stray entry guards nothing."""
    assert {model.__name__ for model in PAYLOAD_MODELS} == set(RECORDED_SHAPES)


def test_persisted_payload_shapes_have_not_changed() -> None:
    """A payload model changed shape: decide what that does to the rows already on disk.

    Can a row written before this change still be read as what it claims to be? An added optional
    field usually means no: it validates as `None`, reading as "we do not know" where the truth is
    "we never asked".
    """
    current = {model.__name__: shape_digest(model) for model in PAYLOAD_MODELS}
    changed = {
        name: digest for name, digest in current.items() if RECORDED_SHAPES.get(name) != digest
    }
    assert not changed, (
        f"persisted payload shape(s) changed: {sorted(changed)}.\n"
        "If rows already in `calculation_results` are now wrong or incomplete, bump "
        "`chemclaw.science.calc.store.CALCULATION_EPOCH` (and log the reason beside it). It "
        "reaches every key: `CalculationKey.build` folds it in, and "
        "`connectors.calc.remote.remote_key` folds it into the params hash of every key the "
        "calculation server derives.\n"
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
